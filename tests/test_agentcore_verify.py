"""``agent/verify.py``: deterministic evaluator, one bounded rewrite on mismatch."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import FakeLLM
from strikt.agent.loop import TurnDeps
from strikt.agent.numbers import ClaimedTotals
from strikt.agent.tools import Registry
from strikt.agent.verify import (
    STATE_CHANGING_TOOLS,
    VERIFY_TOOLS,
    compare_totals,
    verify_reply,
    wants_recalculation,
)
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.core.types import FoodItemIn, Incoming, Macros
from strikt.db import repo
from strikt.db.models import User
from strikt.memory.daystate import DayStateBuilder
from tests.conftest import CHAT_ID, NOW

TODAY = datetime(2026, 9, 3, tzinfo=UTC).date()


@pytest.fixture
async def seeded(session: AsyncSession, user: User) -> User:
    await repo.add_meal_with_items(
        session,
        user.id,
        day_date=TODAY,
        items=[
            FoodItemIn(name="chicken", macros=Macros(kcal=800, protein_g=90, carbs_g=10, fat_g=30)),
            FoodItemIn(
                name="lentils",
                macros=Macros(kcal=440, protein_g=28, carbs_g=50, fat_g=15, fiber_g=10),
            ),
        ],
        slot="lunch",
        logged_at=NOW,
    )
    await session.commit()
    return user


def deps_for(
    session: AsyncSession, user: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> TurnDeps:
    return TurnDeps(
        session=session,
        user=user,
        llm=fake_llm,
        registry=Registry(),
        clock=clock,
        settings=settings,
        state_provider=DayStateBuilder(clock, settings),
    )


def incoming(user: User, text: str) -> Incoming:
    return Incoming(user_id=user.id, chat_id=CHAT_ID, message_id=1, text=text, received_at=NOW)


async def test_mismatch_triggers_one_rewrite(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    draft = "Записал.\nИтого: 1500 ккал | Б 118 | У 60 | Ж 45\nОсталось: 500 ккал"
    fake_llm.queue(
        FakeLLM.text("Записал.\nИтого: 1240 ккал | Б 118 | У 60 | Ж 45\nОсталось: 760 ккал")
    )
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        draft,
        ["log_meal"],
        incoming(seeded, "творог"),
    )
    assert "Итого: 1240 ккал" in text
    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["purpose"] == "verify"
    assert call["cache_tail"] is False
    assert "Verify" in call["system"]
    prompt = call["messages"][0]["content"][0]["text"]
    assert "<draft>" in prompt and "1500" in prompt
    assert "kcal: your text says 1500, the log says 1240" in prompt
    assert "recalculation_requested: no" in prompt
    assert "totals: 1240 kcal" in prompt


async def test_matching_draft_is_untouched_without_a_call(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    draft = "Итого: 1 240 ккал | Б 118 | У 60 | Ж 45 | клетчатка 10"
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        draft,
        ["log_meal"],
        incoming(seeded, "x"),
    )
    assert text == draft
    assert fake_llm.calls == []


async def test_no_state_tool_and_no_recalc_request_skips_the_check(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    draft = "Total 9999 kcal"  # wrong, but nothing changed and nobody asked
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        draft,
        ["search_food"],
        incoming(seeded, "hi"),
    )
    assert text == draft
    assert fake_llm.calls == []


async def test_recalculation_request_triggers_the_check_without_tools(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    fake_llm.queue(FakeLLM.text("chicken 800 + lentils 440 = 1240 kcal. Total 1240 kcal | P 118"))
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        "Total 1300 kcal",
        [],
        incoming(seeded, "пересчитай день"),
    )
    assert "1240" in text
    prompt = fake_llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "recalculation_requested: yes" in prompt


async def test_refused_rewrite_falls_back_to_the_database_line(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    fake_llm.queue(FakeLLM.refusal())
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        "Итого: 1500 ккал",
        ["log_meal"],
        incoming(seeded, "x"),
    )
    assert text.startswith("Итого: 1500 ккал")
    assert "Итого по базе: 1240 ккал | Б 118 | У 60 | Ж 45 | клетчатка 10." in text


async def test_still_wrong_rewrite_gets_the_truth_appended_not_a_second_trial(
    session: AsyncSession, seeded: User, fake_llm: FakeLLM, clock: FakeClock, settings: Settings
) -> None:
    fake_llm.queue(FakeLLM.text("Total 1400 kcal"), FakeLLM.text("never used"))
    text = await verify_reply(
        deps_for(session, seeded, fake_llm, clock, settings),
        seeded,
        "Total 1500 kcal",
        ["update_meal"],
        incoming(seeded, "x"),
    )
    assert text.startswith("Total 1400 kcal")
    assert "Итого по базе: 1240" in text
    assert len(fake_llm.calls) == 1


async def test_tolerances(
    session: AsyncSession, seeded: User, clock: FakeClock, settings: Settings
) -> None:
    state = await DayStateBuilder(clock, settings).day_state(session, seeded, TODAY)
    assert compare_totals(ClaimedTotals(kcal=1244, protein_g=119), state) == []
    assert compare_totals(ClaimedTotals(kcal=1260), state) == []  # within 2 %
    bad = compare_totals(ClaimedTotals(kcal=1300, fat_g=50, fiber_g=10), state)
    assert [m.field for m in bad] == ["kcal", "fat_g"]
    assert bad[0].line() == "kcal: your text says 1300, the log says 1240"


def test_wants_recalculation_keywords() -> None:
    assert wants_recalculation("Пересчитай, у меня не сходится")
    assert wants_recalculation("recalculate the day please")
    assert wants_recalculation("this doesn't add up")
    assert not wants_recalculation("200 г творога")
    assert not wants_recalculation(None)


def test_tool_sets() -> None:
    assert {"log_meal", "update_meal", "delete_meal", "get_day_state"} <= VERIFY_TOOLS
    assert {"log_meal", "log_workout", "close_day", "log_measurement"} <= STATE_CHANGING_TOOLS
    assert "get_day_state" not in STATE_CHANGING_TOOLS
    assert "search_food" not in STATE_CHANGING_TOOLS
