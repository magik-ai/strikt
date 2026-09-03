"""``memory.daystate``: totals/targets/remaining, views, dues, render_context, yesterday line."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.core.types import DayState, DayTotals, Macros, MealItemView, MealView, Remaining
from strikt.db import repo
from strikt.db.models import DayFlag, MealSlot, MeasurementType, User, UserStatus
from strikt.memory import daystate
from strikt.memory.daystate import DEFAULT_TARGETS, DayStateBuilder, render_context
from strikt.telegram.render import render_day_card
from tests.test_memory_helpers import (
    TODAY,
    TZ,
    at_local,
    item,
    seed_meal,
    seed_measurement,
    seed_recovery,
    seed_sleep,
    seed_workout,
)


async def test_day_state_totals_targets_remaining_and_views(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    breakfast = await seed_meal(
        session,
        user.id,
        TODAY,
        "09:10",
        [item("eggs", 140, 12, 1, 10), item("toast", 160, 5, 30, 2, fiber=3)],
        slot=MealSlot.breakfast,
        item_flags={0: ["kcal_mismatch"]},
    )
    await seed_meal(
        session,
        user.id,
        TODAY,
        "13:30",
        [item("ramen", 780, 35, 90, 25, fiber=4, countable=False)],
        slot=MealSlot.lunch,
        note="Kinoya",
    )
    deleted = await seed_meal(session, user.id, TODAY, "15:00", [item("cake", 500)])
    await repo.soft_delete_meal(session, user.id, deleted.id, now=clock.now())
    await seed_meal(session, user.id, TODAY - timedelta(days=1), "20:00", [item("steak", 600, 50)])
    await seed_workout(session, user.id, TODAY, "07:00")
    await seed_sleep(session, user.id, TODAY, "00:40", "07:10")
    await seed_sleep(session, user.id, TODAY - timedelta(days=1), "01:00", "07:30")
    await seed_recovery(session, user.id, TODAY, score=61)
    await repo.set_day_flag(session, user.id, TODAY, DayFlag.salty, True, now=clock.now())
    await repo.set_day_plan(session, user.id, TODAY, {"lunch": "ramen 13:00"}, now=clock.now())
    await session.commit()

    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    assert state.date == TODAY and not state.closed
    assert state.totals.meals == 2 and state.totals.items == 3
    assert state.totals.macros.kcal == 1080 and state.totals.macros.protein_g == 52
    assert state.totals.macros.fiber_g == 7
    assert state.targets.kcal == 2000 and state.targets.protein_g == 210
    assert state.remaining.kcal == 920 and state.remaining.protein_g == 158
    assert state.remaining.carbs_g == 75 - 121

    first = state.meals[0]
    assert first.id == breakfast.id and first.slot == "breakfast"
    assert first.items[0].flags == ["kcal_mismatch"] and first.items[1].flags == []
    assert first.macros.kcal == 300 and first.logged_at.tzinfo is not None
    assert state.meals[1].note == "Kinoya" and not state.meals[1].items[0].countable

    assert [w.sport for w in state.workouts] == ["run"]
    assert state.workouts[0].zones_min == {"z0": 10.0, "z1": 20.0, "z2": 15.0}
    assert state.sleep is not None and state.sleep.asleep_min == 370
    assert state.sleep.ended_at == at_local(TODAY, "07:10")
    assert state.recovery is not None and state.recovery.score == 61
    assert state.flags == ["salty"] and state.plan == {"lunch": "ramen 13:00"}
    assert state.verdict is None


async def test_closed_day_verdict_and_empty_day(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await repo.close_day(
        session, user.id, TODAY, verdict="Closed at 1910. Bed by 00:30.", now=clock.now()
    )
    await session.commit()
    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    assert state.closed and state.verdict == "Closed at 1910. Bed by 00:30."
    assert state.totals.meals == 0 and state.meals == [] and state.sleep is None
    assert state.remaining.kcal == 2000


async def test_targets_fallback_without_protocol(
    session: AsyncSession, clock: FakeClock, settings: object
) -> None:
    fresh, _ = await repo.get_or_create_user(
        session, telegram_id=5, chat_id=5, now=clock.now(), status=UserStatus.onboarding
    )
    await session.commit()
    state = await DayStateBuilder(clock, settings).day_state(session, fresh, TODAY)  # type: ignore[arg-type]
    assert state.targets == DEFAULT_TARGETS
    assert state.measurements_due == []  # no profile → nothing due
    strict = await DayStateBuilder(clock, fallback_targets=False).day_state(session, fresh, TODAY)
    assert strict.targets.kcal == 0
    assert "no protocol" in render_day_card(strict, "en")


async def test_measurements_due_by_cadence(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    # profile: waist every 14 days, weight every 7; KPI is waist → listed first
    await seed_measurement(
        session, user.id, TODAY - timedelta(days=16), "07:30", type="waist", value=103, unit="cm"
    )
    await seed_measurement(
        session, user.id, TODAY - timedelta(days=2), "07:30", type="weight", value=104.2
    )
    await session.commit()
    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    assert state.measurements_due == ["waist"]

    await seed_measurement(
        session, user.id, TODAY, "07:00", type=MeasurementType.waist, value=102, unit="cm"
    )
    await session.commit()
    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    assert state.measurements_due == []

    # a past day only sees measurements before its end; both overdue then
    past = TODAY - timedelta(days=20)
    state = await DayStateBuilder(clock).day_state(session, user, past)
    assert state.measurements_due == ["waist", "weight"]  # never measured before → due


async def test_render_context_compact_and_under_budget(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await seed_meal(
        session,
        user.id,
        TODAY,
        "09:10",
        [item("eggs", 140, 12, 1, 10), item("toast", 160, 5, 30, 2, fiber=3)],
        slot=MealSlot.breakfast,
        item_flags={1: ["loose_under_report"]},
    )
    await seed_workout(session, user.id, TODAY, "07:00")
    await seed_sleep(session, user.id, TODAY, "00:40", "07:10")
    await seed_recovery(session, user.id, TODAY)
    await repo.set_day_flag(session, user.id, TODAY, "alcohol", True, now=clock.now())
    await session.commit()
    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    text = render_context(state, "ru", tz=TZ)
    lines = text.splitlines()
    assert lines[0] == "day 2026-09-03 (чт) открыт"
    assert lines[1].startswith("totals: 300 kcal | P 17 | C 31 | F 12 | fiber 3 | 1 meals, 2 items")
    assert lines[2] == "targets: 2000 kcal | P 210 | C 75 | F 105 | fiber 30"
    assert lines[3].startswith("remaining: 1700 kcal | P 193 | C 44 | F 93 | fiber 27")
    assert "meals (1):" in lines
    meal_line = next(line for line in lines if line.startswith("- 09:10 breakfast #"))
    assert "eggs 140 kcal (12P/1C/10F)" in meal_line and "[loose_under_report]" in meal_line
    assert meal_line.endswith("= 300 kcal, P 17")
    assert any(
        line.startswith("training: run 07:00 · 45 min · strain 12.3 · 406 kcal") for line in lines
    )
    assert "sleep: 00:40→07:10 · asleep 6h10 · in bed 6h30 · 78%" in lines
    assert "recovery: 61% · rhr 52 · hrv 48 ms" in lines
    assert "flags: alcohol" in lines
    assert "<" not in text and len(text) < daystate.CONTEXT_MAX_CHARS
    assert render_context(state, "en", tz=TZ).splitlines()[0] == "day 2026-09-03 (Thu) open"


def _big_state(meals: int, items_per_meal: int) -> DayState:
    macros = Macros(kcal=120, protein_g=10, carbs_g=10, fat_g=5, fiber_g=1)
    views = [
        MealView(
            id=i,
            slot="snack",
            logged_at=at_local(TODAY, "10:00") + timedelta(minutes=i),
            items=[
                MealItemView(
                    id=i * 100 + j, name=f"a rather long item name number {j}", macros=macros
                )
                for j in range(items_per_meal)
            ],
            macros=macros.scaled(items_per_meal),
        )
        for i in range(meals)
    ]
    total = macros.scaled(meals * items_per_meal)
    return DayState(
        date=TODAY,
        totals=DayTotals(macros=total, items=meals * items_per_meal, meals=meals),
        targets=Macros(kcal=2000, protein_g=200, carbs_g=100, fat_g=80, fiber_g=30),
        remaining=Remaining.from_targets(
            Macros(kcal=2000, protein_g=200, carbs_g=100, fat_g=80, fiber_g=30), total
        ),
        meals=views,
        plan={"dinner": "x" * 500},
        verdict="y" * 500,
    )


def test_render_context_degrades_to_fit_budget() -> None:
    text = render_context(_big_state(meals=14, items_per_meal=6), "en", tz=TZ)
    assert len(text) <= daystate.CONTEXT_MAX_CHARS
    assert "more meals" in text
    assert "meals (14):" in text
    # a normal day keeps item macros
    small = render_context(_big_state(meals=2, items_per_meal=2), "en", tz=TZ)
    assert "120 kcal (10P/10C/5F/1fib)" in small
    empty = render_context(_big_state(meals=0, items_per_meal=0), "en", tz=TZ)
    assert "meals: none logged" in empty


async def test_yesterday_close_line(session: AsyncSession, user: User, clock: FakeClock) -> None:
    assert await daystate.yesterday_close_line(session, user, TODAY) is None
    yday = TODAY - timedelta(days=1)
    await seed_meal(session, user.id, yday, "13:00", [item("chicken", 900, 150, 20, 30, fiber=10)])
    await seed_meal(session, user.id, yday, "20:00", [item("yogurt", 1010, 48, 60, 20, fiber=20)])
    await session.commit()
    line = await daystate.yesterday_close_line(session, user, TODAY)
    assert line == "Вчера (2026-09-02, ср): 1910 kcal / P 198 / fiber 30 / 2 meals, не закрыт"

    await repo.set_day_flag(session, user.id, yday, DayFlag.salty, True, now=clock.now())
    await repo.close_day(
        session, user.id, yday, verdict="Best structure this month.", now=clock.now()
    )
    await session.commit()
    line = await daystate.yesterday_close_line(session, user, TODAY)
    assert line == (
        "Вчера (2026-09-02, ср): 1910 kcal / P 198 / fiber 30 / 2 meals, закрыт, флаги: salty."
        " Best structure this month."
    )

    user.language = "en"
    await repo.get_or_open_day(session, user.id, TODAY, now=clock.now())
    await session.commit()
    line = await daystate.yesterday_close_line(session, user, TODAY + timedelta(days=1))
    assert line == "Yesterday (2026-09-03, Thu): nothing logged, not closed"


async def test_sick_day_pauses_targets_and_the_card_says_so(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    """Brief §3.6: protocol paused, no calorie targets on a sick day."""
    await seed_meal(session, user.id, TODAY, "09:10", [item("broth", 120, 8, 6, 5)])
    await repo.set_day_flag(session, user.id, TODAY, DayFlag.sick, True, now=clock.now())
    await session.commit()
    state = await DayStateBuilder(clock).day_state(session, user, TODAY)
    assert "sick" in state.flags
    assert state.targets == Macros.zero()
    assert state.totals.macros.kcal == 120  # what was eaten is still counted
    card = render_day_card(state, "en", "Asia/Dubai")
    assert "targets paused" in card and "Left:" not in card and "no protocol" not in card
    assert "на паузе" in render_day_card(state, "ru", "Asia/Dubai")
