"""Clock, events, config, crypto, LLM result helpers, queue and keyboards."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest

from strikt.agent.client import FakeLLM, LLMResult
from strikt.agent.usage import LLMUsage, compute_cost
from strikt.config import ModelPrice, Settings
from strikt.core.clock import FakeClock, in_quiet_hours, local_date, local_day_bounds, week_start
from strikt.db.crypto import TokenCipher, generate_key
from strikt.events import DayStateChanged, Event, EventBus, WorkoutEvent
from strikt.telegram.copy import t
from strikt.telegram.keyboards import forget_confirm, meal_actions, parse_callback, yes_no
from strikt.telegram.messenger import FakeMessenger
from strikt.telegram.queue import PerChatQueue


def test_clock_helpers() -> None:
    clock = FakeClock(datetime(2026, 9, 3, 22, 30, tzinfo=UTC))
    assert local_date(clock, "Asia/Dubai") == date(2026, 9, 4)
    start, end = local_day_bounds(date(2026, 9, 4), "Asia/Dubai")
    assert start == datetime(2026, 9, 3, 20, 0, tzinfo=UTC) and end - start == timedelta(days=1)
    clock.advance(timedelta(hours=2))
    assert clock.now().hour == 0
    assert week_start(date(2026, 9, 3)) == date(2026, 8, 31)
    assert in_quiet_hours(datetime(2026, 9, 3, 1, 0), time(0, 0), time(7, 30))
    assert not in_quiet_hours(datetime(2026, 9, 3, 12, 0), time(0, 0), time(7, 30))
    assert in_quiet_hours(datetime(2026, 9, 3, 23, 0), time(22, 0), time(6, 0))


async def test_event_bus_dispatches_to_bases_and_survives_errors() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_any(event: Event) -> None:
        seen.append("any")

    async def on_workout(event: WorkoutEvent) -> None:
        seen.append(event.sport)

    async def broken(event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(Event, on_any)
    unsubscribe = bus.subscribe(WorkoutEvent, on_workout)
    bus.subscribe(DayStateChanged, broken)
    now = datetime.now(UTC)
    assert (
        await bus.publish(WorkoutEvent(user_id=1, occurred_at=now, sport="run", started_at=now))
        == 2
    )
    assert (
        await bus.publish(
            DayStateChanged(user_id=1, occurred_at=now, date=date.today(), reason="x")
        )
        == 2
    )
    unsubscribe()
    assert (
        await bus.publish(WorkoutEvent(user_id=1, occurred_at=now, sport="row", started_at=now))
        == 1
    )
    assert sorted(seen) == ["any", "any", "any", "run"]


def test_settings_parse_lists_and_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)  # the CI/dev shell may export one
    monkeypatch.setenv("ALLOWED_TELEGRAM_IDS", "1, 2,3")
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", "3")
    monkeypatch.setenv(
        "PRICE_TABLE",
        '{"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}',
    )
    monkeypatch.setenv("QUIET_END", "06:45")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.allowed_telegram_ids == [1, 2, 3] and s.is_admin(3) and s.is_allowed(2)
    assert s.price_for("claude-sonnet-5") == ModelPrice(
        input=3, output=15, cache_read=0.3, cache_write=3.75
    )
    assert s.price_for("unknown") is None
    assert s.quiet_end == time(6, 45)
    assert s.daily_cap_for("drill_sergeant") == 8 and s.daily_cap_for("pushy") == 5
    assert "TELEGRAM_BOT_TOKEN" in s.missing_for_runtime()


def test_cost_computation() -> None:
    price = ModelPrice(input=2.0, output=10.0, cache_read=0.20, cache_write=2.50)
    usage = LLMUsage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=100_000,
    )
    assert compute_cost(price, usage) == pytest.approx(2.0 + 1.0 + 0.4 + 0.25)
    assert compute_cost(None, usage) == 0.0
    assert usage.total_input == 3_100_000


def test_crypto_roundtrip() -> None:
    cipher = TokenCipher(generate_key())
    token = cipher.encrypt("secret")
    assert token != "secret" and cipher.decrypt(token) == "secret"
    assert cipher.encrypt_optional(None) is None
    with pytest.raises(ValueError, match="invalid"):
        TokenCipher(generate_key()).decrypt(token)


async def test_fake_llm_scripts_and_result_helpers() -> None:
    llm = FakeLLM(
        [FakeLLM.tool_use("log_meal", {"items": []}, text="logging"), FakeLLM.text("done")]
    )
    first = await llm.message(
        purpose="turn", system=None, messages=[{"role": "user", "content": "hi"}]
    )
    assert first.wants_tools and first.tool_uses[0].name == "log_meal" and first.text == "logging"
    assert first.assistant_message()["role"] == "assistant"
    second = await llm.message(purpose="verify", system="s", messages=[])
    assert second.text == "done" and not second.wants_tools and llm.calls[1]["purpose"] == "verify"
    with pytest.raises(AssertionError):
        await llm.message(purpose="turn", system=None, messages=[])
    assert FakeLLM.refusal().refused
    assert FakeLLM.json_result({"send": False}).json() == {"send": False}
    truncated = LLMResult(content=[], stop_reason="max_tokens")
    assert truncated.truncated


async def test_per_chat_queue_serialises_and_never_drops() -> None:
    queue = PerChatQueue()
    order: list[str] = []
    beats = 0

    async def beat() -> None:
        nonlocal beats
        beats += 1

    async def job(name: str, delay: float) -> str:
        order.append(f"start {name}")
        await asyncio.sleep(delay)
        order.append(f"end {name}")
        return name

    results = await asyncio.gather(
        queue.run(1, lambda: job("a", 0.05), heartbeat=beat, heartbeat_interval=0.01),
        queue.run(1, lambda: job("b", 0.01)),
        queue.run(2, lambda: job("c", 0.01)),
    )
    assert results == ["a", "b", "c"]
    assert order.index("end a") < order.index("start b")
    assert order.index("start c") < order.index("end a")
    assert beats >= 2 and queue.pending(1) == 0 and not queue.busy(1)


def test_keyboards_and_callbacks() -> None:
    assert parse_callback("s:12:lunch") is not None and parse_callback("s:12:lunch").slot == "lunch"  # type: ignore[union-attr]
    assert parse_callback("s:12:brunch") is None
    assert parse_callback("undo:x") is None
    assert parse_callback("forget:yes").answer is True  # type: ignore[union-attr]
    assert parse_callback("yn:close_day:no").action == "close_day"  # type: ignore[union-attr]
    assert parse_callback("") is None and parse_callback("recalc").kind == "recalc"  # type: ignore[union-attr]
    rows = meal_actions(7, "ru", ask_slot=True)
    assert (
        len(rows) == 3
        and rows[0][0].callback_data == "s:7:breakfast"
        and rows[2][0].text == t("ru", "btn.undo")
    )
    assert forget_confirm("en")[0][0].callback_data == "forget:yes"
    assert yes_no("close_day", "en")[0][1].callback_data == "yn:close_day:no"
    with pytest.raises(ValueError, match="action"):
        yes_no("a:b", "en")


async def test_fake_messenger_records_and_splits() -> None:
    m = FakeMessenger()
    mid = await m.send(5, "hello", keyboard=forget_confirm("en"))
    assert m.sent[-1].message_id == mid and m.sent[-1].keyboard is not None
    assert await m.edit(5, mid, "hello") is False
    assert await m.edit(5, mid, "changed") is True and m.current_text(5, mid) == "changed"
    assert await m.pin(5, mid) and await m.delete(5, mid) and not await m.delete(5, mid)
    long_id = await m.send(5, "\n".join(["x" * 100] * 60))
    assert long_id > mid and len(m.texts(5)) >= 3


def test_coaching_day_rolls_over_after_bedtime_grace() -> None:
    from strikt.core.clock import coaching_day, day_rollover

    # default profile: bed 00:30 → rollover never before 03:00
    assert day_rollover(time(0, 30)) == time(3, 0)
    assert day_rollover(time(23, 45)) == time(3, 0)  # bedtime before midnight: floor at 03:00
    assert day_rollover(time(4, 0)) == time(5, 0)  # 04:00 + 1 h
    assert day_rollover(time(5, 30)) == time(6, 0)  # capped
    assert day_rollover(None) == time(3, 0)
    assert day_rollover(time(0, 30), wake_time=time(2, 30)) == time.min  # shift worker
    ten_past_midnight = datetime(2026, 9, 3, 0, 10)
    assert coaching_day(ten_past_midnight, time(0, 30)) == date(2026, 9, 2)
    assert coaching_day(datetime(2026, 9, 3, 3, 0), time(0, 30)) == date(2026, 9, 3)
    assert coaching_day(datetime(2026, 9, 3, 12, 0), time(0, 30)) == date(2026, 9, 3)
    assert coaching_day(ten_past_midnight, time(0, 30), time(2, 0)) == date(2026, 9, 3)


def test_cost_includes_web_searches_and_one_hour_cache_writes() -> None:
    from strikt.agent.usage import usage_from_message
    from strikt.config import DEFAULT_PRICES

    price = DEFAULT_PRICES["claude-sonnet-5"]
    searches = LLMUsage(input_tokens=1500, output_tokens=600, web_search_requests=5)
    tokens_only = (1500 * 2.0 + 600 * 10.0) / 1_000_000
    assert compute_cost(price, searches) == pytest.approx(tokens_only + 5 * 10.0 / 1000)
    # 1h writes are priced at the 1h rate, the rest of the writes at the 5m rate
    writes = LLMUsage(cache_write_tokens=100_000, cache_write_1h_tokens=40_000)
    assert compute_cost(price, writes) == pytest.approx((60_000 * 2.5 + 40_000 * 4.0) / 1e6)
    old_price = ModelPrice(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5)
    assert compute_cost(old_price, writes) == pytest.approx(100_000 * 2.5 / 1e6)
    assert (searches + writes).web_search_requests == 5

    from anthropic.types import Message, Usage

    message = Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 15,
                    "ephemeral_1h_input_tokens": 25,
                },
                "server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 2},
            },
        }
    )
    assert isinstance(message.usage, Usage)
    usage = usage_from_message(message)
    assert usage == LLMUsage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_tokens=40,
        cache_write_1h_tokens=25,
        web_search_requests=3,
    )
