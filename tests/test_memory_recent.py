"""The ``<recent>`` block: two weeks of real rows, so nothing about the past is invented."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import DataSource, User
from strikt.memory.recent import RecentDay, recent_block, recent_days, render_recent

TODAY = date(2026, 9, 15)
TZ = "Asia/Dubai"


async def _meal(session: AsyncSession, user: User, day: date, kcal: float, protein: float) -> None:
    await repo.add_meal_with_items(
        session,
        user.id,
        day_date=day,
        items=[
            FoodItemIn(
                name="chicken",
                macros=Macros(kcal=kcal, protein_g=protein, carbs_g=0, fat_g=0, fiber_g=4),
            )
        ],
        logged_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC),
    )


async def test_recent_days_reads_food_training_sleep_and_closing(
    session: AsyncSession, user: User
) -> None:
    yesterday = TODAY - timedelta(days=1)
    await _meal(session, user, yesterday, 700, 60)
    await _meal(session, user, yesterday, 500, 40)
    await repo.upsert_workout_by_external(
        session,
        user.id,
        source=DataSource.manual,
        external_id="w1",
        sport="boxing",
        started_at=datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
        duration_min=62,
        now=datetime(2026, 9, 14, 15, 0, tzinfo=UTC),
    )
    await repo.upsert_sleep_by_external(
        session,
        user.id,
        source=DataSource.manual,
        external_id="s1",
        started_at=datetime(2026, 9, 13, 20, 30, tzinfo=UTC),
        ended_at=datetime(2026, 9, 14, 3, 30, tzinfo=UTC),
        asleep_min=402,
        now=datetime(2026, 9, 14, 4, 0, tzinfo=UTC),
    )
    await repo.close_day(
        session,
        user.id,
        yesterday,
        verdict="solid",
        now=datetime(2026, 9, 14, 20, 0, tzinfo=UTC),
    )

    rows = await recent_days(session, user.id, today=TODAY, tz=TZ)
    assert len(rows) == 14
    assert rows[-1].date == yesterday and rows[0].date == TODAY - timedelta(days=14)
    last = rows[-1]
    assert (last.kcal, last.protein_g, last.meals) == (1200.0, 100.0, 2)
    assert last.fiber_g == 8.0 and last.closed is True
    assert last.workouts == ["boxing 62m"] and last.sleep_h == 6.7
    assert all(row.empty for row in rows[:-1])


async def test_recent_block_is_empty_without_history(session: AsyncSession, user: User) -> None:
    assert await recent_block(session, user.id, today=TODAY, tz=TZ, lang="ru") == ""


async def test_recent_block_renders_one_line_per_day(session: AsyncSession, user: User) -> None:
    await _meal(session, user, TODAY - timedelta(days=2), 1900, 180)
    text = await recent_block(session, user.id, today=TODAY, tz=TZ, lang="ru", days=3)
    lines = text.splitlines()
    assert len(lines) == 3
    assert "1900 kcal, 180 P" in lines[1] and "no training" in lines[1]
    assert lines[0].endswith(": -") and lines[2].endswith(": -")


def test_render_recent_counts_the_workouts_it_does_not_name() -> None:
    row = RecentDay(
        date=date(2026, 9, 14),
        meals=1,
        kcal=800,
        protein_g=70,
        workouts=["gym 60m", "boxing 45m", "run 30m", "swim 20m"],
    )
    line = render_recent([row], "en")
    assert "gym 60m + boxing 45m + run 30m +1" in line
