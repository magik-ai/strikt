"""The ``<recent>`` block: the last two weeks, one line per day, for the turn context.

Why it exists: the context used to carry today's state, yesterday's close line and a handful of
prose summaries. Anything older was invisible, so the coach filled the gap from the conversation -
and told the user he had trained zero times last week when the database held four sessions. A
claim about a past day now has somewhere to come from.

One line a day, oldest first, ~40 characters each: food totals, training, sleep, closed or not.
Four aggregate queries for the whole window, none of them per-day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import func, select

from strikt.core.clock import local_day_bounds, to_local
from strikt.db.models import Day, Meal, MealItem, Sleep, Workout
from strikt.telegram.copy import weekday_name

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: Days of history the block covers (today excluded - it is in ``<day>``).
RECENT_DAYS = 14
#: Workouts named on one day's line before the rest are counted instead.
MAX_WORKOUTS_PER_DAY = 3


def _n(value: float | None) -> str:
    return "0" if not value else f"{round(value):g}"


@dataclass
class RecentDay:
    """What one past day holds. Every field is read from the database, none is inferred."""

    date: date
    kcal: float = 0.0
    protein_g: float = 0.0
    fiber_g: float = 0.0
    meals: int = 0
    workouts: list[str] = field(default_factory=list)
    sleep_h: float | None = None
    closed: bool = False

    @property
    def empty(self) -> bool:
        return not (self.meals or self.workouts or self.sleep_h)


async def recent_days(
    session: AsyncSession,
    user_id: int,
    *,
    today: date,
    tz: str,
    days: int = RECENT_DAYS,
) -> list[RecentDay]:
    """The ``days`` days before ``today``, oldest first, empty days included."""
    if days <= 0:
        return []
    first = today - timedelta(days=days)
    last = today - timedelta(days=1)
    rows: dict[date, RecentDay] = {
        first + timedelta(days=i): RecentDay(date=first + timedelta(days=i)) for i in range(days)
    }
    start_utc, _ = local_day_bounds(first, tz)
    _, end_utc = local_day_bounds(last, tz)

    totals = await session.execute(
        select(
            Meal.day_date,
            func.sum(MealItem.kcal),
            func.sum(MealItem.protein_g),
            func.sum(MealItem.fiber_g),
            func.count(sa.distinct(Meal.id)),
        )
        .join(MealItem, MealItem.meal_id == Meal.id)
        .where(
            Meal.user_id == user_id,
            Meal.deleted_at.is_(None),
            Meal.day_date >= first,
            Meal.day_date <= last,
        )
        .group_by(Meal.day_date)
    )
    for day, kcal, protein, fiber, meals in totals.all():
        row = rows.get(day)
        if row is None:
            continue
        row.kcal, row.protein_g, row.fiber_g = (
            float(kcal or 0),
            float(protein or 0),
            float(fiber or 0),
        )
        row.meals = int(meals or 0)

    workouts = await session.scalars(
        select(Workout)
        .where(
            Workout.user_id == user_id,
            Workout.started_at >= start_utc,
            Workout.started_at < end_utc,
        )
        .order_by(Workout.started_at)
    )
    for workout in workouts:
        row = rows.get(to_local(workout.started_at, tz).date())
        if row is None:
            continue
        minutes = f" {round(workout.duration_min)}m" if workout.duration_min else ""
        row.workouts.append(f"{workout.sport}{minutes}")

    nights = await session.scalars(
        select(Sleep)
        .where(Sleep.user_id == user_id, Sleep.ended_at >= start_utc, Sleep.ended_at < end_utc)
        .order_by(Sleep.ended_at)
    )
    for night in nights:
        row = rows.get(to_local(night.ended_at, tz).date())
        if row is None:
            continue
        slept = night.asleep_min or night.in_bed_min
        if slept:
            row.sleep_h = round(float(slept) / 60, 1)

    closed = await session.execute(
        select(Day.date).where(
            Day.user_id == user_id,
            Day.date >= first,
            Day.date <= last,
            Day.closed_at.is_not(None),
        )
    )
    for (day,) in closed.all():
        row = rows.get(day)
        if row is not None:
            row.closed = True

    return [rows[key] for key in sorted(rows)]


def render_recent(days: list[RecentDay], lang: str | None, *, empty_label: str = "-") -> str:
    """The block's text: ``09-14 sun: 1980 kcal, 176 P, 22 fib, 3 meals | boxing 62m | 6.7 h``."""
    lines: list[str] = []
    for row in days:
        head = f"{row.date:%m-%d} {weekday_name(lang, row.date.weekday())[:3].lower()}:"
        if row.empty:
            lines.append(f"{head} {empty_label}")
            continue
        parts: list[str] = []
        if row.meals:
            parts.append(
                f"{_n(row.kcal)} kcal, {_n(row.protein_g)} P, {_n(row.fiber_g)} fib,"
                f" {row.meals} meals"
            )
        else:
            parts.append("no food logged")
        if row.workouts:
            shown = row.workouts[:MAX_WORKOUTS_PER_DAY]
            extra = len(row.workouts) - len(shown)
            parts.append(" + ".join(shown) + (f" +{extra}" if extra else ""))
        else:
            parts.append("no training")
        if row.sleep_h:
            parts.append(f"{row.sleep_h:g} h sleep")
        lines.append(f"{head} " + " | ".join(parts))
    return "\n".join(lines)


async def recent_block(
    session: AsyncSession,
    user_id: int,
    *,
    today: date,
    tz: str,
    lang: str | None,
    days: int = RECENT_DAYS,
) -> str:
    """``recent_days`` rendered, or an empty string when there is nothing to show."""
    rows = await recent_days(session, user_id, today=today, tz=tz, days=days)
    if not any(not row.empty for row in rows):
        return ""
    return render_recent(rows, lang)
