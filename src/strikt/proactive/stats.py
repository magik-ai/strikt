"""Response-rate and streak statistics for adaptive intensity (brief §7.3, §7.5).

- ``response_rate``: share of proactive sends for one trigger that got a reply in the last N
  days (the dataset every send row contributes to).
- ``compute_streaks``: consecutive days closed within target, days with three logged meals,
  bedtime hits — counted backwards from yesterday (today is not over), or from today when
  today is already closed.
- ``week_scorecard``: the Sunday numbers (kcal, protein, fiber, sessions, bedtime adherence,
  measurements) — each a number, no stars.

Pure aggregation over ``repo`` reads; no writes, no LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import local_day_bounds, to_local
from strikt.core.types import Macros
from strikt.db import repo
from strikt.db.models import MeasurementType, User

CLEAN_KCAL_TOLERANCE = 1.05  # closed within target = at most 5 % over
BEDTIME_TOLERANCE_MIN = 30


@dataclass(frozen=True, kw_only=True)
class Streaks:
    clean_days: int = 0  # consecutive days closed within the kcal target
    three_meal_days: int = 0  # consecutive days with ≥ 3 logged meals
    bedtime_hits: int = 0  # consecutive nights asleep within 30 min of the agreed bedtime
    clean_streak_start: date | None = None

    def as_facts(self) -> dict[str, Any]:
        return {
            "clean_days": self.clean_days,
            "three_meal_days": self.three_meal_days,
            "bedtime_hits": self.bedtime_hits,
            "clean_streak_start": (
                None if self.clean_streak_start is None else self.clean_streak_start.isoformat()
            ),
        }


@dataclass(frozen=True, kw_only=True)
class WeekScorecard:
    week_start: date
    days_logged: int
    days_closed: int
    days_within_target: int
    avg_kcal: float | None
    avg_protein_g: float | None
    avg_fiber_g: float | None
    sessions: int
    bedtime_hits: int
    nights_tracked: int
    measurements_taken: int

    def as_facts(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start.isoformat(),
            "days_logged": self.days_logged,
            "days_closed": self.days_closed,
            "days_within_target": self.days_within_target,
            "avg_kcal": _round_opt(self.avg_kcal),
            "avg_protein_g": _round_opt(self.avg_protein_g),
            "avg_fiber_g": _round_opt(self.avg_fiber_g),
            "sessions": self.sessions,
            "bedtime_hits": self.bedtime_hits,
            "nights_tracked": self.nights_tracked,
            "measurements_taken": self.measurements_taken,
        }


@dataclass(frozen=True, kw_only=True)
class _DayRow:
    date: date
    kcal: float
    protein_g: float
    fiber_g: float
    meals: int
    closed: bool
    flags: tuple[str, ...]


def _round_opt(value: float | None) -> int | None:
    return None if value is None else round(value)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def minutes_after_noon(t: time) -> int:
    """Minutes since 12:00 so that bedtimes around midnight compare monotonically."""
    return ((t.hour * 60 + t.minute) - 720) % 1440


def bedtime_hit(onset_local: datetime, bed_time: time | None) -> bool | None:
    """True when sleep onset is within ``BEDTIME_TOLERANCE_MIN`` after the agreed bedtime."""
    if bed_time is None:
        return None
    return minutes_after_noon(onset_local.time()) <= minutes_after_noon(bed_time) + (
        BEDTIME_TOLERANCE_MIN
    )


def is_clean_day(row: _DayRow, kcal_target: float) -> bool:
    if row.meals == 0 or kcal_target <= 0 or "off" in row.flags:
        return False
    return row.kcal <= kcal_target * CLEAN_KCAL_TOLERANCE


async def _day_rows(
    session: AsyncSession, user_id: int, *, date_from: date, date_to: date
) -> list[_DayRow]:
    meals = await repo.list_meals_range(session, user_id, date_from, date_to)
    days = {d.date: d for d in await repo.list_days_range(session, user_id, date_from, date_to)}
    totals: dict[date, Macros] = {}
    counts: dict[date, int] = {}
    for meal in meals:
        totals[meal.day_date] = totals.get(meal.day_date, Macros.zero()) + repo.meal_macros(meal)
        counts[meal.day_date] = counts.get(meal.day_date, 0) + 1
    rows: list[_DayRow] = []
    current = date_from
    while current <= date_to:
        macros = totals.get(current, Macros.zero())
        day = days.get(current)
        rows.append(
            _DayRow(
                date=current,
                kcal=macros.kcal,
                protein_g=macros.protein_g,
                fiber_g=macros.fiber_g,
                meals=counts.get(current, 0),
                closed=bool(day is not None and day.closed_at is not None),
                flags=tuple(str(f) for f in (day.flags or [])) if day is not None else (),
            )
        )
        current += timedelta(days=1)
    return rows


def _consecutive(rows: Sequence[_DayRow], predicate: Any) -> tuple[int, date | None]:
    count = 0
    start: date | None = None
    for row in reversed(rows):
        if not predicate(row):
            break
        count += 1
        start = row.date
    return count, start


async def response_rate(
    session: AsyncSession,
    user: User,
    trigger: str | None,
    *,
    now: datetime,
    days: int = 30,
) -> float | None:
    """Share of sends for ``trigger`` (None = all) that got a reply in the last ``days`` days."""
    return await repo.response_rate(
        session, user.id, trigger=trigger, since=now - timedelta(days=days)
    )


async def compute_streaks(
    session: AsyncSession,
    user_id: int,
    *,
    today: date,
    tz: str,
    kcal_target: float,
    bed_time: time | None,
    days: int = 30,
) -> Streaks:
    """Streaks ending yesterday (or today when today is already closed)."""
    rows = await _day_rows(session, user_id, date_from=today - timedelta(days=days), date_to=today)
    if rows and rows[-1].date == today and not rows[-1].closed:
        rows = rows[:-1]
    clean, clean_start = _consecutive(rows, lambda r: is_clean_day(r, kcal_target))
    three, _ = _consecutive(rows, lambda r: r.meals >= 3)

    hits = 0
    if bed_time is not None:
        start_utc, _ = local_day_bounds(today - timedelta(days=days), tz)
        _, end_utc = local_day_bounds(today, tz)
        nights = await repo.list_sleep_range(session, user_id, start_utc, end_utc)
        for row in reversed(nights):
            if bedtime_hit(to_local(row.started_at, tz), bed_time):
                hits += 1
            else:
                break
    return Streaks(
        clean_days=clean,
        three_meal_days=three,
        bedtime_hits=hits,
        clean_streak_start=clean_start,
    )


async def week_scorecard(
    session: AsyncSession,
    user_id: int,
    *,
    week_start: date,
    tz: str,
    targets: Macros,
    bed_time: time | None,
    through: date | None = None,
) -> WeekScorecard:
    """The weekly numbers for ``[week_start, through]`` (default: the whole week)."""
    week_end = week_start + timedelta(days=6)
    last = min(week_end, through) if through is not None else week_end
    rows = await _day_rows(session, user_id, date_from=week_start, date_to=last)
    logged = [r for r in rows if r.meals > 0]
    start_utc, _ = local_day_bounds(week_start, tz)
    _, end_utc = local_day_bounds(last, tz)
    workouts = await repo.list_workouts_range(session, user_id, start_utc, end_utc)
    nights = await repo.list_sleep_range(session, user_id, start_utc, end_utc)
    hits = sum(1 for n in nights if bedtime_hit(to_local(n.started_at, tz), bed_time))
    measurements = await repo.list_measurements_range(session, user_id, start_utc, end_utc)
    body = [m for m in measurements if m.type in (MeasurementType.waist, MeasurementType.weight)]
    return WeekScorecard(
        week_start=week_start,
        days_logged=len(logged),
        days_closed=sum(1 for r in rows if r.closed),
        days_within_target=sum(1 for r in logged if is_clean_day(r, targets.kcal)),
        avg_kcal=_mean([r.kcal for r in logged]),
        avg_protein_g=_mean([r.protein_g for r in logged]),
        avg_fiber_g=_mean([r.fiber_g for r in logged]),
        sessions=len(workouts),
        bedtime_hits=hits,
        nights_tracked=len(nights),
        measurements_taken=len(body),
    )
