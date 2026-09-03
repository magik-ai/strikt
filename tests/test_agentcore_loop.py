"""``agent/loop.py`` with ``FakeLLM`` and a small test registry."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import STOP_MAX_TOKENS, FakeLLM, LLMError, LLMResult
from strikt.agent.loop import CONTINUE_TEXT, TurnDeps, run_turn, stub_media_blocks, to_telegram_html
from strikt.agent.tools import Registry, Tool, ToolContext, ToolResult
from strikt.agent.tools.schemas import ToolInput
from strikt.agent.usage import LLMUsage
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.core.types import Attachment, FoodItemIn, Incoming, Macros
from strikt.db import repo
from strikt.db.models import MealSlot, TurnRole, User
from strikt.events import DayStateChanged, Event, EventBus, UserReplied
from strikt.memory.daystate import DayStateBuilder
from strikt.telegram.copy import t
from tests.conftest import CHAT_ID, NOW


class FakeLogMeal(ToolInput):
    """Test double for log_meal: stores one item with the given macros."""

    name: str = Field(description="Item name.")
    kcal: float = Field(description="kcal.")
    protein_g: float = Field(default=0, description="P.")
    carbs_g: float = Field(default=0, description="C.")
    fat_g: float = Field(default=0, description="F.")
    slot: str | None = Field(default=None, description="Slot.")


class NoArgs(ToolInput):
    """Test double with no arguments."""


class Boom(ToolInput):
    """Test double that always fails."""


async def fake_log_meal(ctx: ToolContext, args: FakeLogMeal) -> ToolResult:
    meal = await repo.add_meal_with_items(
        ctx.session,
        ctx.user_id,
        day_date=ctx.local_date,
        items=[
            FoodItemIn(
                name=args.name,
                macros=Macros(
                    kcal=args.kcal, protein_g=args.protein_g, carbs_g=args.carbs_g, fat_g=args.fat_g
                ),
            )
        ],
        slot=args.slot or MealSlot.unknown,
        logged_at=ctx.clock.now(),
    )
    return ToolResult(content=json.dumps({"meal_id": meal.id, "kcal": args.kcal}))


async def fake_day_state(ctx: ToolContext, args: NoArgs) -> ToolResult:
    return ToolResult(content="day state ok")


async def fake_boom(ctx: ToolContext, args: Boom) -> ToolResult:
    raise RuntimeError("kaboom")


@pytest.fixture
def test_registry() -> Registry:
    reg = Registry()
    reg.register(Tool.from_model("log_meal", FakeLogMeal, fake_log_meal))
    reg.register(Tool.from_model("get_day_state", NoArgs, fake_day_state))
    reg.register(Tool.from_model("boom", Boom, fake_boom))
    return reg


class Recorder:
    def __init__(self, bus: EventBus) -> None:
        self.events: list[Event] = []
        bus.subscribe(Event, self.on_event)

    async def on_event(self, event: Event) -> None:
        self.events.append(event)


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def recorder(bus: EventBus) -> Recorder:
    return Recorder(bus)


def make_deps(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    registry: Registry,
    clock: FakeClock,
    settings: Settings,
    bus: EventBus | None = None,
) -> TurnDeps:
    return TurnDeps(
        session=session,
        user=user,
        llm=fake_llm,
        registry=registry,
        clock=clock,
        settings=settings,
        bus=bus,
        state_provider=DayStateBuilder(clock, settings),
    )


def incoming(user: User, text: str | None, attachments: list[Attachment] | None = None) -> Incoming:
    return Incoming(
        user_id=user.id,
        chat_id=CHAT_ID,
        message_id=7,
        text=text,
        attachments=attachments or [],
        received_at=NOW,
    )


def two_tool_uses() -> LLMResult:
    return LLMResult(
        content=[
            {"type": "text", "text": "Logging."},
            {
                "type": "tool_use",
                "id": "t1",
                "name": "log_meal",
                "input": {"name": "eggs", "kcal": 300, "protein_g": 20, "carbs_g": 2, "fat_g": 22},
            },
            {"type": "tool_use", "id": "t2", "name": "boom", "input": {}},
            {"type": "tool_use", "id": "t3", "name": "get_day_state", "input": {}},
        ],
        stop_reason="tool_use",
        usage=LLMUsage(input_tokens=20, output_tokens=10),
        model="fake-model",
    )


# ------------------------------------------------------------------------------------ tests


async def test_text_only_turn(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
    bus: EventBus,
    recorder: Recorder,
) -> None:
    fake_llm.queue(FakeLLM.text("Сон важнее клетчатки."))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings, bus),
        incoming(user, "а клетчатка?"),
    )
    assert result.outgoings[0].text == "Сон важнее клетчатки."
    assert result.outgoings[0].keyboard is None  # 12:00 local, nothing to correct
    assert result.tools_used == [] and result.state_changed is False
    assert result.usage.output_tokens == 5
    turns = await repo.last_n_turns(session, user.id, 10)
    assert [t.role for t in turns] == [TurnRole.user, TurnRole.assistant]
    assert turns[0].text == "а клетчатка?" and turns[0].telegram_message_id == 7
    assert turns[1].text == "Сон важнее клетчатки."
    assert result.turn_id == turns[0].id and result.assistant_turn_id == turns[1].id
    assert [type(e) for e in recorder.events] == [UserReplied]
    assert isinstance(recorder.events[0], UserReplied) and recorder.events[0].turn_id == turns[0].id
    call = fake_llm.calls[0]
    assert call["purpose"] == "turn" and call["tools"] and call["cache_tail"] is True
    assert call["system"][0]["cache_control"]["ttl"] == "1h"
    assert [m["role"] for m in call["messages"]] == ["user"]  # the current turn is not history
    content = call["messages"][0]["content"]
    assert len(content) == 2
    assert content[0]["text"].startswith("<context>")
    assert content[1] == {"type": "text", "text": "а клетчатка?"}


async def test_tool_round_trip_publishes_day_state_changed_and_picks_slot_keyboard(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
    bus: EventBus,
    recorder: Recorder,
) -> None:
    fake_llm.queue(
        FakeLLM.tool_use(
            "log_meal",
            {"name": "cottage cheese", "kcal": 620, "protein_g": 40, "carbs_g": 30, "fat_g": 20},
            id="tu1",
        ),
        FakeLLM.text("Творог 620 ккал\nИтого: 620 ккал | Б 40 | У 30 | Ж 20\nОсталось: 1380 ккал"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings, bus),
        incoming(user, "творог"),
    )
    assert result.tools_used == ["log_meal"]
    assert result.state_changed is True
    assert result.rounds == 1
    assert len(fake_llm.calls) == 2  # no verify call: the total matches the database
    second = fake_llm.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][0]["type"] == "tool_use"
    assert second[-1]["role"] == "user"
    tool_result = second[-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu1"
    assert "is_error" not in tool_result
    changed = [e for e in recorder.events if isinstance(e, DayStateChanged)]
    assert len(changed) == 1
    assert (
        changed[0].date == datetime(2026, 9, 3, tzinfo=UTC).date()
        and "log_meal" in changed[0].reason
    )
    keyboard = result.outgoings[0].keyboard
    assert keyboard is not None and len(keyboard) == 3
    assert keyboard[0][0].text == "Завтрак" and keyboard[0][0].callback_data is not None
    assert keyboard[0][0].callback_data.startswith("s:")
    assert [b.text for b in keyboard[2]] == [t("ru", "btn.undo"), t("ru", "btn.recalc")]
    assert "Итого: 620 ккал" in result.outgoings[0].text


async def test_slotted_meal_gets_actions_without_slot_picker(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        FakeLLM.tool_use("log_meal", {"name": "eggs", "kcal": 300, "slot": "breakfast"}),
        FakeLLM.text("Записал завтрак."),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings),
        incoming(user, "яйца на завтрак"),
    )
    keyboard = result.outgoings[0].keyboard
    assert keyboard is not None and len(keyboard) == 1
    assert [b.text for b in keyboard[0]] == [t("ru", "btn.undo"), t("ru", "btn.recalc")]


async def test_parallel_tool_calls_return_in_one_message_with_is_error(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(two_tool_uses(), FakeLLM.text("Done."))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "eggs")
    )
    assert result.tools_used == ["log_meal", "boom", "get_day_state"]
    results_msg = fake_llm.calls[1]["messages"][-1]
    assert results_msg["role"] == "user"
    blocks = results_msg["content"]
    assert [b["tool_use_id"] for b in blocks] == ["t1", "t2", "t3"]
    assert all(b["type"] == "tool_result" for b in blocks)
    assert blocks[1]["is_error"] is True and "kaboom" in blocks[1]["content"]
    assert "is_error" not in blocks[0] and "is_error" not in blocks[2]
    assert result.text.startswith("Logging.")  # text before the tool calls is kept


async def test_parallel_flag_runs_tools_concurrently_with_same_order(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        LLMResult(
            content=[
                {"type": "tool_use", "id": "a", "name": "get_day_state", "input": {}},
                {"type": "tool_use", "id": "b", "name": "boom", "input": {}},
            ],
            stop_reason="tool_use",
        ),
        FakeLLM.text("ok"),
    )
    deps = make_deps(session, user, fake_llm, test_registry, clock, settings)
    deps.parallel_tools = True
    await run_turn(deps, incoming(user, "x"))
    blocks = fake_llm.calls[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in blocks] == ["a", "b"]


async def test_refusal_becomes_an_honest_line(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(FakeLLM.refusal("nope"))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "…")
    )
    assert result.refused is True
    assert (
        result.outgoings[0].text
        == "С этим не помогу. Пришли следующий приём еды или спроси о другом."
    )
    turns = await repo.last_n_turns(session, user.id, 5)
    assert [t.role for t in turns] == [TurnRole.user, TurnRole.assistant]


async def test_max_tokens_gets_one_continuation(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        LLMResult(content=[{"type": "text", "text": "part one"}], stop_reason=STOP_MAX_TOKENS),
        FakeLLM.text("part two"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "long")
    )
    assert result.text == "part one\npart two"
    tail = fake_llm.calls[1]["messages"]
    assert tail[-1]["content"][0]["text"] == CONTINUE_TEXT
    assert tail[-2]["role"] == "assistant"


async def test_pause_turn_is_resent(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        LLMResult(content=[{"type": "text", "text": "searching"}], stop_reason="pause_turn"),
        FakeLLM.text("found"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "?")
    )
    assert result.text == "found"
    assert fake_llm.calls[1]["messages"][-1]["role"] == "assistant"


async def test_tool_round_cap(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    settings.max_tool_rounds = 1
    fake_llm.queue(
        FakeLLM.tool_use("get_day_state", {}, id="r1"),
        FakeLLM.tool_use("get_day_state", {}, id="r2"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "?")
    )
    assert result.tools_used == ["get_day_state"]
    assert "Слишком много шагов" in result.text
    assert len(fake_llm.calls) == 2


async def test_llm_error_yields_down_copy_and_keeps_user_turn(
    session: AsyncSession, user: User, test_registry: Registry, clock: FakeClock, settings: Settings
) -> None:
    class DownLLM(FakeLLM):
        async def message(self, **kwargs: Any) -> LLMResult:  # type: ignore[override]
            raise LLMError("boom", retryable=True, status=500)

    result = await run_turn(
        make_deps(session, user, DownLLM(), test_registry, clock, settings), incoming(user, "hi")
    )
    assert result.error == "boom"
    assert "Claude недоступен" in result.outgoings[0].text
    assert result.assistant_turn_id is None
    turns = await repo.last_n_turns(session, user.id, 5)
    assert [t.role for t in turns] == [TurnRole.user]


async def test_evening_open_day_gets_day_actions(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    clock.set(datetime(2026, 9, 3, 17, 5, tzinfo=UTC))  # 21:05 in Dubai
    fake_llm.queue(FakeLLM.text("Ужин?"))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "привет")
    )
    keyboard = result.outgoings[0].keyboard
    assert keyboard is not None
    assert [b.text for b in keyboard[0]] == ["Пересчитать", "Закрыть день"]


async def test_images_go_to_the_model_and_are_stubbed_in_history(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    photo = Attachment(kind="image", mime="image/jpeg", bytes_b64="YWJj", sha256="a" * 64)
    fake_llm.queue(FakeLLM.text("ok"))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings),
        incoming(user, None, [photo]),
    )
    sent = fake_llm.calls[0]["messages"][-1]["content"]
    assert any(b["type"] == "image" and b["source"]["data"] == "YWJj" for b in sent)
    turn = (await repo.last_n_turns(session, user.id, 5))[0]
    assert turn.id == result.turn_id
    assert turn.content == [{"type": "text", "text": f"[image: {'a' * 64}]"}]


def test_stub_media_blocks_hashes_when_no_sha_given() -> None:
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}},
        {"type": "text", "text": "hi"},
    ]
    stubbed = stub_media_blocks(blocks, [])
    assert stubbed[1] == {"type": "text", "text": "hi"}
    assert stubbed[0]["text"].startswith("[image: ba7816bf")  # sha256("abc")


def test_to_telegram_html_escapes_and_keeps_bold_and_code() -> None:
    assert (
        to_telegram_html("waist <94 cm & **bold** `x`")
        == "waist &lt;94 cm &amp; <b>bold</b> <code>x</code>"
    )
    assert to_telegram_html("## Title\ntext") == "Title\ntext"


async def test_state_changed_only_for_state_changing_tools(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
    bus: EventBus,
    recorder: Recorder,
) -> None:
    fake_llm.queue(FakeLLM.tool_use("get_day_state", {}), FakeLLM.text("1240 so far"))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings, bus),
        incoming(user, "сколько сегодня?"),
    )
    assert result.tools_used == ["get_day_state"]
    assert result.state_changed is False
    assert not any(isinstance(e, DayStateChanged) for e in recorder.events)


async def test_open_proactive_send_is_marked_answered(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    send = await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_first_meal",
        window_key="no_first_meal:2026-09-03",
        step=1,
        sent_at=NOW - timedelta(minutes=20),
        text="Nothing logged. Breakfast?",
    )
    await session.commit()
    fake_llm.queue(FakeLLM.text("ok"))
    await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings),
        incoming(user, "ate at the office"),
    )
    ctx = fake_llm.calls[0]["messages"][-1]["content"][0]["text"]
    assert "<proactive>" in ctx
    await session.refresh(send)
    assert send.responded_at is not None
