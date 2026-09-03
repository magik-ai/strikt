"""``agent/client.py``'s production wrapper with a stubbed ``AsyncAnthropic``: request shape,
stop-reason normalisation, refusal details, error mapping, usage recording (memory and DB)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from strikt.agent.client import (
    LLM,
    DbUsageRecorder,
    LLMError,
    MemoryUsageRecorder,
)
from strikt.agent.usage import LLMUsage
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db.engine import make_session_factory
from strikt.db.models import TokenUsage, User


def message_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
        },
    }
    base.update(overrides)
    return base


class StubMessages:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Message | BaseException] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class StubClient:
    def __init__(self) -> None:
        self.messages = StubMessages()


def make_llm(
    settings: Settings, recorder: MemoryUsageRecorder | DbUsageRecorder | None = None
) -> tuple[LLM, StubClient]:
    stub = StubClient()
    llm = LLM(settings, client=stub, recorder=recorder)  # type: ignore[arg-type]
    return llm, stub


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(status: int) -> anthropic.APIStatusError:
    response = httpx.Response(status, request=_request(), json={"error": {"message": "x"}})
    if status == 429:
        return anthropic.RateLimitError("rate", response=response, body=None)
    return anthropic.APIStatusError(f"status {status}", response=response, body=None)


async def test_request_shape_for_a_turn(settings: Settings) -> None:
    recorder = MemoryUsageRecorder()
    llm, stub = make_llm(settings, recorder)
    stub.messages.responses.append(Message.model_validate(message_dict()))
    system = [
        {"type": "text", "text": "coach", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    tools = [{"name": "log_meal", "input_schema": {"type": "object"}, "strict": True}]
    result = await llm.message(
        purpose="turn",
        system=system,
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        tools=tools,
        user_id=7,
    )
    kwargs = stub.messages.calls[0]
    assert kwargs["model"] == settings.model
    assert kwargs["max_tokens"] == settings.max_tokens_turn
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": settings.effort_turn}
    assert kwargs["system"] == system and kwargs["tools"] == tools
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["messages"][0]["role"] == "user"
    assert "tool_choice" not in kwargs
    assert result.text == "ok" and result.stop_reason == "end_turn" and not result.truncated
    assert result.usage == LLMUsage(
        input_tokens=100, output_tokens=20, cache_read_tokens=50, cache_write_tokens=10
    )
    assert result.cost_usd == pytest.approx((100 * 2.0 + 20 * 10.0 + 50 * 0.2 + 10 * 2.5) / 1e6)
    assert recorder.records == [
        {
            "user_id": 7,
            "model": "claude-sonnet-5",
            "purpose": "turn",
            "usage": result.usage,
            "cost_usd": result.cost_usd,
        }
    ]


async def test_structured_output_and_explicit_overrides(settings: Settings) -> None:
    llm, stub = make_llm(settings)
    stub.messages.responses.append(
        Message.model_validate(message_dict(content=[{"type": "text", "text": '{"send": false}'}]))
    )
    result = await llm.message(
        purpose="proactive",
        system="p",
        messages=[{"role": "user", "content": [{"type": "text", "text": "?"}]}],
        output_schema={"type": "object"},
        max_tokens=512,
        effort="high",
        cache_tail=False,
        tool_choice={"type": "auto"},
    )
    kwargs = stub.messages.calls[0]
    assert kwargs["system"] == "p" and kwargs["max_tokens"] == 512
    assert kwargs["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert "cache_control" not in kwargs and kwargs["tool_choice"] == {"type": "auto"}
    assert result.json() == {"send": False}


async def test_tool_use_max_tokens_and_refusal_are_normalised(settings: Settings) -> None:
    llm, stub = make_llm(settings)
    stub.messages.responses += [
        Message.model_validate(
            message_dict(
                stop_reason="tool_use",
                content=[{"type": "tool_use", "id": "t1", "name": "log_meal", "input": {"a": 1}}],
            )
        ),
        Message.model_validate(message_dict(stop_reason="max_tokens")),
        Message.model_validate(
            message_dict(
                stop_reason="refusal",
                content=[],
                stop_details={"type": "refusal", "category": "general_harms", "explanation": "no"},
            )
        ),
    ]
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    tool = await llm.message(purpose="turn", system=None, messages=msgs)
    assert tool.wants_tools and tool.tool_uses[0].name == "log_meal"
    assert tool.tool_uses[0].input == {"a": 1}
    assert tool.assistant_message()["content"][0]["type"] == "tool_use"
    cut = await llm.message(purpose="turn", system=None, messages=msgs)
    assert cut.truncated and not cut.wants_tools
    refused = await llm.message(purpose="turn", system=None, messages=msgs)
    assert refused.refused and refused.refusal is not None
    assert refused.refusal.explanation == "no" and refused.refusal.category == "general_harms"
    assert "system" not in stub.messages.calls[0]


async def test_error_mapping(settings: Settings) -> None:
    llm, stub = make_llm(settings)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    stub.messages.responses += [
        anthropic.APIConnectionError(request=_request()),
        _status_error(429),
        _status_error(529),
        _status_error(400),
    ]
    with pytest.raises(LLMError) as conn:
        await llm.message(purpose="turn", system=None, messages=msgs)
    assert conn.value.retryable and conn.value.status is None
    with pytest.raises(LLMError) as rate:
        await llm.message(purpose="turn", system=None, messages=msgs)
    assert rate.value.retryable and rate.value.status == 429
    with pytest.raises(LLMError) as overloaded:
        await llm.message(purpose="turn", system=None, messages=msgs)
    assert overloaded.value.retryable and overloaded.value.status == 529
    with pytest.raises(LLMError) as bad:
        await llm.message(purpose="turn", system=None, messages=msgs)
    assert not bad.value.retryable and bad.value.status == 400


async def test_db_usage_recorder_writes_one_row_per_user_local_day(
    engine: AsyncEngine, user: User, settings: Settings, clock: FakeClock
) -> None:
    """The brief's "token spend per user per day" is the user's day: 22:30 UTC on the 3rd is
    already the 4th in Dubai."""
    clock.set(datetime(2026, 9, 3, 22, 30, tzinfo=UTC))
    sessions = make_session_factory(engine)
    llm, stub = make_llm(settings, DbUsageRecorder(sessions, clock))
    stub.messages.responses += [
        Message.model_validate(message_dict()),
        Message.model_validate(message_dict()),
    ]
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    await llm.message(purpose="turn", system=None, messages=msgs, user_id=user.id)
    await llm.message(purpose="turn", system=None, messages=msgs, user_id=user.id)
    stub.messages.responses.append(Message.model_validate(message_dict()))
    await llm.message(purpose="verify", system=None, messages=msgs, user_id=None)  # not recorded
    async with sessions() as session:
        rows = list((await session.scalars(select(TokenUsage))).all())
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == user.id and row.date == datetime(2026, 9, 4).date()
    assert str(row.purpose) == "turn" and row.calls == 2
    assert row.input_tokens == 200 and row.cache_read_tokens == 100
    assert row.cache_write_tokens == 20 and row.output_tokens == 40
    assert row.cost_usd == pytest.approx(2 * (100 * 2.0 + 20 * 10.0 + 50 * 0.2 + 10 * 2.5) / 1e6)


async def test_recorder_failure_never_breaks_the_call(settings: Settings) -> None:
    class Broken:
        async def record(self, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    llm, stub = make_llm(settings, Broken())  # type: ignore[arg-type]
    stub.messages.responses.append(Message.model_validate(message_dict()))
    result = await llm.message(
        purpose="turn",
        system=None,
        messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
        user_id=1,
    )
    assert result.text == "ok"
