"""The daily streak: the 10% rule, the open day and the gap."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import User
from strikt.telegram.streak import count_streak, current_streak, day_in_range

TARGETS = Macros(kcal=2000, protein_g=210, fat_g=105, carbs_g=75)
TODAY = date(2026, 9, 3)
FIELDS = ("kcal", "protein_g", "fat_g", "carbs_g")


def _shift(field: str, factor: float, extra: float = 0) -> Macros:
    target = getattr(TARGETS, field)
    return TARGETS.model_copy(update={field: target * factor + extra})


@pytest.mark.parametrize("field", FIELDS)
def test_exactly_ten_percent_is_inside(field: str) -> None:
    assert day_in_range(_shift(field, 1.1), TARGETS)
    assert day_in_range(_shift(field, 0.9), TARGETS)


@pytest.mark.parametrize("field", FIELDS)
def test_just_past_ten_percent_is_outside(field: str) -> None:
    assert not day_in_range(_shift(field, 1.1, 0.01), TARGETS)
    assert not day_in_range(_shift(field, 0.9, -0.01), TARGETS)


def test_all_four_at_the_edge_at_once_is_inside() -> None:
    edge = Macros(kcal=2200, protein_g=189, fat_g=115.5, carbs_g=67.5)
    assert day_in_range(edge, TARGETS)


def test_no_targets_never_counts() -> None:
    assert not day_in_range(Macros.zero(), Macros.zero())
    assert count_streak({TODAY: Macros.zero()}, Macros.zero(), today=TODAY, today_closed=True) == 0


def _days(*offsets: int, macros: Macros = TARGETS) -> dict[date, Macros]:
    return {TODAY - timedelta(days=o): macros for o in offsets}


def test_open_today_is_skipped_either_way() -> None:
    good = _days(0, 1, 2, 3)
    assert count_streak(good, TARGETS, today=TODAY, today_closed=False) == 3
    bad_today = {**_days(1, 2, 3), TODAY: Macros(kcal=400, protein_g=30, fat_g=10, carbs_g=20)}
    assert count_streak(bad_today, TARGETS, today=TODAY, today_closed=False) == 3


def test_closed_today_counts() -> None:
    assert count_streak(_days(0, 1, 2, 3), TARGETS, today=TODAY, today_closed=True) == 4


def test_closed_today_off_target_breaks_it() -> None:
    days = {**_days(1, 2, 3), TODAY: _shift("protein_g", 0.5)}
    assert count_streak(days, TARGETS, today=TODAY, today_closed=True) == 0


def test_a_gap_breaks_the_streak() -> None:
    assert count_streak(_days(1, 2, 4, 5, 6), TARGETS, today=TODAY, today_closed=False) == 2


def test_a_day_outside_the_rule_breaks_the_streak() -> None:
    days = {**_days(1, 2, 4), TODAY - timedelta(days=3): _shift("carbs_g", 1.2)}
    assert count_streak(days, TARGETS, today=TODAY, today_closed=False) == 2


async def _log(session: AsyncSession, user: User, day: date, macros: Macros = TARGETS) -> None:
    await repo.add_meal_with_items(
        session,
        user.id,
        day_date=day,
        items=[FoodItemIn(name="x", macros=macros)],
        logged_at=datetime(2026, 9, 1, 8, 0),
    )


async def test_current_streak_reads_the_meals_and_the_closed_day(
    session: AsyncSession, user: User
) -> None:
    for ago in range(40):  # longer than the first window, so the read widens
        await _log(session, user, TODAY - timedelta(days=ago))
    await _log(session, user, TODAY - timedelta(days=41))  # after the gap on day 40
    await session.commit()
    assert await current_streak(session, user.id, today=TODAY, targets=TARGETS) == 39
    await repo.close_day(session, user.id, TODAY, verdict=None, now=datetime(2026, 9, 3, 20, 0))
    assert await current_streak(session, user.id, today=TODAY, targets=TARGETS) == 40
