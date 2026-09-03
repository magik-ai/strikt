"""``agent/loop.py`` with ``FakeLLM`` and a small test registry."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import STOP_MAX_TOKENS, FakeLLM, LLMCreditError, LLMError, LLMResult
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
    # one button, whatever the slot: the slot itself is corrected in words
    assert keyboard is not None and len(keyboard) == 1 and len(keyboard[0]) == 1
    assert keyboard[0][0].text == t("ru", "btn.undo")
    assert keyboard[0][0].callback_data == "undo:1"
    assert "Итого: 620 ккал" in result.outgoings[0].text


async def test_a_logged_meal_gets_exactly_one_button(
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
    assert [b.text for b in keyboard[0]] == [t("ru", "btn.undo")]


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
    assert result.outgoings[0].text == t("ru", "err.llm_down")
    assert result.assistant_turn_id is None
    turns = await repo.last_n_turns(session, user.id, 5)
    assert [t.role for t in turns] == [TurnRole.user]


async def test_empty_anthropic_balance_names_the_balance_not_the_model(
    session: AsyncSession, user: User, test_registry: Registry, clock: FakeClock, settings: Settings
) -> None:
    """A 400 about credit is the user's billing, not an outage: saying "Claude is down" sends
    them looking in the wrong place."""

    class BrokeLLM(FakeLLM):
        async def message(self, **kwargs: Any) -> LLMResult:  # type: ignore[override]
            raise LLMCreditError("api error 400: credit balance is too low", status=400)

    result = await run_turn(
        make_deps(session, user, BrokeLLM(), test_registry, clock, settings), incoming(user, "hi")
    )
    assert result.outgoings[0].text == t("ru", "err.llm_no_credit")
    assert result.outgoings[0].text != t("ru", "err.llm_down")
    assert result.assistant_turn_id is None


async def test_after_midnight_the_turn_stays_on_the_evening_day(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    """The day ends at the rollover, not at midnight (brief §3.3). ``log_meal`` always knew that;
    the card, the context and the events used the calendar date, so a 01:10 dinner was written to
    the 3rd and displayed on an empty 4th."""
    await repo.upsert_profile(
        session, user.id, {"bed_time": time(0, 30), "wake_time": time(8, 0)}, now=clock.now()
    )
    await session.commit()
    clock.set(datetime(2026, 9, 3, 21, 10, tzinfo=UTC))  # 01:10 on the 4th in Dubai
    bus = EventBus()
    recorder = Recorder(bus)
    fake_llm.queue(
        FakeLLM.tool_use("log_meal", {"name": "шаверма", "kcal": 620, "slot": "dinner"}),
        FakeLLM.text("Записал."),
    )

    await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings, bus),
        incoming(user, "шаверма"),
    )

    evening = date(2026, 9, 3)
    meals = await repo.list_meals_for_date(session, user.id, evening)
    assert len(meals) == 1, "log_meal dates by the coaching day"
    changed = [e for e in recorder.events if isinstance(e, DayStateChanged)]
    assert [e.date for e in changed] == [evening], "and everything else has to agree"


async def test_evening_reply_without_a_meal_carries_no_buttons(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    """Recalculate and Close day used to hang off every evening reply, which is how two buttons
    ended up under an error message. Both are ordinary sentences now."""
    clock.set(datetime(2026, 9, 3, 17, 5, tzinfo=UTC))  # 21:05 in Dubai
    fake_llm.queue(FakeLLM.text("Ужин?"))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "привет")
    )
    assert result.outgoings[0].keyboard is None


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


# ------------------------------------------------------- verify against the day the tools touched


class DayArg(ToolInput):
    """Test double for get_day_state with a date."""

    day: date | None = Field(default=None, description="Local date.")


class MealIdArg(ToolInput):
    """Test double for update_meal."""

    meal_id: int = Field(description="Meal id.")


async def fake_day_state_dated(ctx: ToolContext, args: DayArg) -> ToolResult:
    from strikt.agent.tools.common import state_numbers

    day = args.day or ctx.local_date
    state = await DayStateBuilder(ctx.clock, ctx.settings).day_state(ctx.session, ctx.user, day)
    return ToolResult(content=json.dumps({"numbers": state_numbers(state)}))


async def fake_undo_last(ctx: ToolContext, args: NoArgs) -> ToolResult:
    meal = await repo.last_meal(ctx.session, ctx.user_id)
    assert meal is not None
    await repo.soft_delete_meal(ctx.session, ctx.user_id, meal.id, now=ctx.clock.now())
    return ToolResult(
        content=json.dumps({"undone_meal_id": meal.id, "day": {"date": meal.day_date.isoformat()}})
    )


async def fake_update_meal(ctx: ToolContext, args: MealIdArg) -> ToolResult:
    meal = await repo.get_meal(ctx.session, ctx.user_id, args.meal_id)
    assert meal is not None
    return ToolResult(content=json.dumps({"meal_id": meal.id, "date": meal.day_date.isoformat()}))


@pytest.fixture
def dated_registry(test_registry: Registry) -> Registry:
    reg = Registry()
    reg.register(Tool.from_model("log_meal", FakeLogMeal, fake_log_meal))
    reg.register(Tool.from_model("get_day_state", DayArg, fake_day_state_dated))
    reg.register(Tool.from_model("undo_last", NoArgs, fake_undo_last))
    reg.register(Tool.from_model("update_meal", MealIdArg, fake_update_meal))
    return reg


async def seed_day(
    session: AsyncSession, user: User, day: date, kcal: float, protein: float
) -> int:
    meal = await repo.add_meal_with_items(
        session,
        user.id,
        day_date=day,
        items=[
            FoodItemIn(name="food", macros=Macros(kcal=kcal, protein_g=protein, carbs_g=0, fat_g=0))
        ],
        logged_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=9),
    )
    await session.commit()
    return meal.id


async def test_yesterday_totals_are_verified_against_yesterday_not_today(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    dated_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    yesterday = today - timedelta(days=1)
    await seed_day(session, user, yesterday, 1910, 198)
    await seed_day(session, user, today, 420, 30)
    fake_llm.queue(
        FakeLLM.tool_use("get_day_state", {"day": yesterday.isoformat()}),
        FakeLLM.text("Вчера: курица и рис.\nИтого за вчера: 1 910 ккал / Б 198"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, dated_registry, clock, settings),
        incoming(user, "что я ел вчера?"),
    )
    assert len(fake_llm.calls) == 2  # correct numbers about yesterday: no rewrite
    assert "1 910" in result.text and "420" not in result.text


async def test_mixed_days_in_one_reply_skip_the_check(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    dated_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    yesterday = today - timedelta(days=1)
    await seed_day(session, user, yesterday, 1910, 198)
    await seed_day(session, user, today, 420, 30)
    fake_llm.queue(
        LLMResult(
            content=[
                {
                    "type": "tool_use",
                    "id": "a",
                    "name": "get_day_state",
                    "input": {"day": yesterday.isoformat()},
                },
                {"type": "tool_use", "id": "b", "name": "get_day_state", "input": {}},
            ],
            stop_reason="tool_use",
        ),
        FakeLLM.text("Итого вчера 1910 ккал, сегодня пока 420 ккал."),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, dated_registry, clock, settings),
        incoming(user, "сравни вчера и сегодня"),
    )
    assert len(fake_llm.calls) == 2 and "1910" in result.text


async def test_dinner_logged_after_midnight_is_checked_against_its_own_day(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    clock: FakeClock,
    settings: Settings,
    bus: EventBus,
    recorder: Recorder,
) -> None:
    """The real food registry: bed 00:30, dinner at 00:10 lands on yesterday; the reply states
    yesterday's total and must not be rewritten to today's empty state."""
    from strikt.agent.tools import build_registry

    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    await seed_day(session, user, today, 1490, 110)
    clock.set(datetime(2026, 9, 3, 20, 10, tzinfo=UTC))  # 00:10 Dubai on the 4th
    fake_llm.queue(
        FakeLLM.tool_use(
            "log_meal",
            {
                "items": [
                    {"name": "творог", "kcal": 392, "protein_g": 40, "carbs_g": 12, "fat_g": 18}
                ],
                "slot": "dinner",
            },
        ),
        FakeLLM.text("Творог 392 ккал.\nИтого: 1882 ккал | Б 150"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, build_registry(), clock, settings, bus),
        incoming(user, "ужин: творог"),
    )
    assert len(fake_llm.calls) == 2, [c["purpose"] for c in fake_llm.calls]
    assert "Итого: 1882" in result.text
    changed = [e for e in recorder.events if isinstance(e, DayStateChanged)]
    assert [e.date for e in changed] == [today]  # the evening's day, not the calendar's
    assert await repo.list_meals_for_date(session, user.id, today + timedelta(days=1)) == []


async def test_verify_mismatch_rewrites_through_run_turn(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        FakeLLM.tool_use("log_meal", {"name": "творог", "kcal": 620, "protein_g": 40}),
        FakeLLM.text("Творог.\nИтого: 900 ккал | Б 40"),
        FakeLLM.text("Творог.\nИтого: 620 ккал | Б 40"),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "творог")
    )
    assert [c["purpose"] for c in fake_llm.calls] == ["turn", "turn", "verify"]
    assert "Итого: 620" in result.outgoings[0].text and "900" not in result.outgoings[0].text
    turns = await repo.last_n_turns(session, user.id, 2)
    assert "620" in turns[-1].text and "900" not in turns[-1].text
    prompt = fake_llm.calls[2]["messages"][0]["content"][0]["text"]
    assert "<day_state>" in prompt and "totals: 620 kcal" in prompt


# ------------------------------------------------------------------------- keyboard targeting


async def test_undo_button_never_targets_an_untouched_meal(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    dated_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    lunch = await seed_day(session, user, today, 700, 50)
    dinner = await seed_day(session, user, today, 500, 40)
    deps = make_deps(session, user, fake_llm, dated_registry, clock, settings)

    fake_llm.queue(FakeLLM.tool_use("undo_last", {}), FakeLLM.text("Убрал ужин."))
    result = await run_turn(deps, incoming(user, "убери ужин"))
    assert result.outgoings[0].keyboard is None  # no Undo aimed at the surviving lunch
    assert (await repo.get_meal(session, user.id, dinner)) is None
    assert (await repo.get_meal(session, user.id, lunch)) is not None

    snack = await seed_day(session, user, today, 200, 10)
    fake_llm.queue(
        FakeLLM.tool_use("update_meal", {"meal_id": lunch}), FakeLLM.text("Поправил обед.")
    )
    result = await run_turn(deps, incoming(user, "обед был 150 г"))
    keyboard = result.outgoings[0].keyboard
    assert keyboard is not None
    undo = keyboard[-1][0]
    assert undo.callback_data == f"undo:{lunch}" and undo.callback_data != f"undo:{snack}"


async def test_logged_meal_button_targets_the_meal_this_turn_created(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    dated_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    await seed_day(session, user, today, 700, 50)
    fake_llm.queue(FakeLLM.tool_use("log_meal", {"name": "eggs", "kcal": 300}), FakeLLM.text("ok"))
    result = await run_turn(
        make_deps(session, user, fake_llm, dated_registry, clock, settings), incoming(user, "яйца")
    )
    keyboard = result.outgoings[0].keyboard
    assert keyboard is not None and len(keyboard) == 1
    meals = await repo.list_meals_for_date(session, user.id, today)
    new_meal = next(m for m in meals if m.items[0].name == "eggs")
    assert keyboard[-1][0].callback_data == f"undo:{new_meal.id}"


# ---------------------------------------------------------------- truncated inside a tool call


async def test_max_tokens_inside_a_tool_call_retries_with_a_larger_cap(
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
                {"type": "text", "text": "Logging"},
                {"type": "tool_use", "id": "cut", "name": "log_meal", "input": {}},
            ],
            stop_reason=STOP_MAX_TOKENS,
        ),
        FakeLLM.tool_use("log_meal", {"name": "eggs", "kcal": 300}, id="full"),
        FakeLLM.text("Записал."),
    )
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "яйца")
    )
    assert result.tools_used == ["log_meal"] and result.text == "Записал."
    first, retry = fake_llm.calls[0], fake_llm.calls[1]
    assert first["max_tokens"] is None
    assert retry["max_tokens"] == settings.max_tokens_turn * 2
    assert retry["messages"] == first["messages"]  # the half tool_use is never re-sent
    assert all(
        b.get("type") != "tool_use" or b["id"] != "cut"
        for m in fake_llm.calls[2]["messages"]
        for b in m["content"]
    )


async def test_evening_closed_day_without_tools_gets_no_buttons(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    test_registry: Registry,
    clock: FakeClock,
    settings: Settings,
) -> None:
    today = datetime(2026, 9, 3, tzinfo=UTC).date()
    await repo.close_day(session, user.id, today, verdict="ok", now=clock.now())
    await session.commit()
    clock.set(datetime(2026, 9, 3, 17, 5, tzinfo=UTC))  # 21:05 in Dubai
    fake_llm.queue(FakeLLM.text("Спокойной ночи."))
    result = await run_turn(
        make_deps(session, user, fake_llm, test_registry, clock, settings), incoming(user, "всё")
    )
    assert result.outgoings[0].keyboard is None
