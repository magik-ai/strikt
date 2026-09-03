"""History facts for the proactive engine.

Everything the trigger preconditions need beyond today's ``DayState`` is loaded here in one
``HistoryFacts`` bundle: evening intake on days with a skipped lunch, past weekend blowups,
the last same-sport workout and the 30-day average, the last three nights of sleep, recent
weights, measurement staleness, the same-meal streak and the streak counters. Every query
filters by ``user_id``; nothing here writes.

Local-time rules used throughout (PLAN §14: store UTC, compute local with ``zoneinfo``):
- a meal's instant is ``eaten_at`` when known, else ``logged_at``;
- the *lunch window* is 11:00–16:00 local, the *evening* starts at 18:00 local;
- a day "counts" for pattern statistics only when at least one meal was logged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc, local_day_bounds, to_local, week_start
from strikt.core.types import Macros
from strikt.db import repo
from strikt.db.models import (
    ConversationTurn,
    Day,
    Meal,
    MeasurementType,
    Note,
    NoteKind,
    ProactiveSend,
    Profile,
    Protocol,
    Recovery,
    Reminder,
    Sleep,
    Summary,
    SummaryKind,
    TurnRole,
    User,
    Workout,
)
from strikt.proactive.stats import Streaks, WeekScorecard, compute_streaks, week_scorecard

LUNCH_WINDOW: tuple[time, time] = (time(11, 0), time(16, 0))
EVENING_FROM = time(18, 0)
OVER_TARGET_RATIO = 1.15  # a day is "off" when kcal exceed the target by 15 %
DEFAULT_SLEEP_TARGET_MIN = 420
MIN_SLEEP_TARGET_MIN = 360
DEFAULT_SESSIONS_PER_WEEK = 3

# ------------------------------------------------------------------------------------- facts


@dataclass(frozen=True, kw_only=True)
class DayKcal:
    """One local day's intake as the triggers see it."""

    date: date
    kcal: float
    protein_g: float
    fiber_g: float
    meals: int
    flags: tuple[str, ...] = ()
    closed: bool = False
    had_lunch: bool = False
    evening_kcal: float = 0.0
    first_meal_at: datetime | None = None  # local

    def as_facts(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "weekday": self.date.strftime("%a"),
            "kcal": round(self.kcal),
            "protein_g": round(self.protein_g),
            "fiber_g": round(self.fiber_g),
            "meals": self.meals,
            "flags": list(self.flags),
            "closed": self.closed,
            "had_lunch": self.had_lunch,
            "evening_kcal": round(self.evening_kcal),
        }


@dataclass(frozen=True, kw_only=True)
class SkippedLunchStats:
    """Evening intake on days with a skipped lunch versus days with one (last N days)."""

    skipped_days: int
    with_lunch_days: int
    avg_evening_kcal_skipped: float | None
    avg_evening_kcal_with_lunch: float | None
    avg_day_kcal_skipped: float | None
    avg_day_kcal_with_lunch: float | None
    examples: tuple[DayKcal, ...] = ()

    def as_facts(self) -> dict[str, Any]:
        return {
            "skipped_lunch_days": self.skipped_days,
            "with_lunch_days": self.with_lunch_days,
            "avg_evening_kcal_after_skipped_lunch": _round_opt(self.avg_evening_kcal_skipped),
            "avg_evening_kcal_with_lunch": _round_opt(self.avg_evening_kcal_with_lunch),
            "avg_day_kcal_after_skipped_lunch": _round_opt(self.avg_day_kcal_skipped),
            "avg_day_kcal_with_lunch": _round_opt(self.avg_day_kcal_with_lunch),
            "examples": [d.as_facts() for d in self.examples],
        }


@dataclass(frozen=True, kw_only=True)
class SleepNight:
    night_of: date  # local date the sleep ended on
    started_local: datetime
    ended_local: datetime
    asleep_min: float | None
    in_bed_min: float | None
    performance_pct: float | None

    def as_facts(self) -> dict[str, Any]:
        return {
            "date": self.night_of.isoformat(),
            "onset": self.started_local.strftime("%H:%M"),
            "woke": self.ended_local.strftime("%H:%M"),
            "asleep_min": _round_opt(self.asleep_min),
            "in_bed_min": _round_opt(self.in_bed_min),
            "performance_pct": _round_opt(self.performance_pct),
        }


@dataclass(frozen=True, kw_only=True)
class WorkoutFacts:
    sport: str
    started_local: datetime
    duration_min: float | None
    kcal: float | None
    avg_hr: float | None
    max_hr: float | None
    strain: float | None
    z0_pct: float | None
    kcal_per_min: float | None

    @classmethod
    def from_row(cls, row: Workout, tz: str) -> WorkoutFacts:
        return cls.from_values(
            sport=row.sport,
            started_at=row.started_at,
            tz=tz,
            duration_min=row.duration_min,
            kcal=row.kcal,
            avg_hr=row.avg_hr,
            max_hr=row.max_hr,
            strain=row.strain,
            zones_min=row.zones_min,
        )

    @classmethod
    def from_values(
        cls,
        *,
        sport: str,
        started_at: datetime,
        tz: str,
        duration_min: float | None,
        kcal: float | None,
        avg_hr: float | None,
        max_hr: float | None,
        strain: float | None,
        zones_min: dict[str, Any] | None,
    ) -> WorkoutFacts:
        return cls(
            sport=sport,
            started_local=to_local(started_at, tz),
            duration_min=duration_min,
            kcal=kcal,
            avg_hr=avg_hr,
            max_hr=max_hr,
            strain=strain,
            z0_pct=zone_zero_share(zones_min),
            kcal_per_min=density(kcal, duration_min),
        )

    def as_facts(self) -> dict[str, Any]:
        return {
            "sport": self.sport,
            "date": self.started_local.date().isoformat(),
            "started": self.started_local.strftime("%H:%M"),
            "duration_min": _round_opt(self.duration_min),
            "kcal": _round_opt(self.kcal),
            "avg_hr": _round_opt(self.avg_hr),
            "max_hr": _round_opt(self.max_hr),
            "strain": None if self.strain is None else round(self.strain, 1),
            "zone0_pct": _round_opt(self.z0_pct),
            "kcal_per_min": None if self.kcal_per_min is None else round(self.kcal_per_min, 1),
        }


@dataclass(frozen=True, kw_only=True)
class WorkoutComparison:
    previous: WorkoutFacts | None
    avg_count: int
    avg_duration_min: float | None
    avg_kcal: float | None
    avg_avg_hr: float | None
    avg_strain: float | None

    def as_facts(self) -> dict[str, Any]:
        return {
            "previous_same_sport": None if self.previous is None else self.previous.as_facts(),
            "avg_30d": {
                "sessions": self.avg_count,
                "duration_min": _round_opt(self.avg_duration_min),
                "kcal": _round_opt(self.avg_kcal),
                "avg_hr": _round_opt(self.avg_avg_hr),
                "strain": None if self.avg_strain is None else round(self.avg_strain, 1),
            },
        }


@dataclass(frozen=True, kw_only=True)
class SameMealStreak:
    name: str
    days: int
    last_date: date


@dataclass(frozen=True, kw_only=True)
class MeasurementStatus:
    type: str
    cadence_days: int
    days_since: int | None  # None = never measured
    last_value: float | None
    last_unit: str | None
    days_overdue: int  # 0 when on schedule

    def as_facts(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "cadence_days": self.cadence_days,
            "days_since": self.days_since,
            "days_overdue": self.days_overdue,
            "last_value": self.last_value,
            "unit": self.last_unit,
            "never_measured": self.days_since is None,
        }


@dataclass(frozen=True, kw_only=True)
class HistoryFacts:
    """Everything a precondition may look at besides today's ``DayState``."""

    recent_days: tuple[DayKcal, ...] = ()  # oldest → newest, excludes today
    skipped_lunch: SkippedLunchStats | None = None
    weekend_blowups: tuple[DayKcal, ...] = ()
    sleep_nights: tuple[SleepNight, ...] = ()  # newest first
    sleep_target_min: int = DEFAULT_SLEEP_TARGET_MIN
    recent_recoveries: tuple[tuple[date, float | None], ...] = ()  # oldest → newest
    last_workout: WorkoutFacts | None = None
    workout_comparison: WorkoutComparison | None = None
    recent_weights: tuple[tuple[datetime, float], ...] = ()  # local instants, oldest → newest
    measurements: tuple[MeasurementStatus, ...] = ()
    same_meal: SameMealStreak | None = None
    streaks: Streaks = field(default_factory=Streaks)
    scorecard: WeekScorecard | None = None
    last_user_message_at: datetime | None = None  # UTC
    unanswered_sends: int = 0
    pending_reminders: tuple[Reminder, ...] = ()
    yesterday_summary: str | None = None
    yesterday_verdict: str | None = None


# ----------------------------------------------------------------------------------- helpers


def _round_opt(value: float | None) -> int | None:
    return None if value is None else round(value)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def zone_zero_share(zones_min: dict[str, Any] | None) -> float | None:
    """Percent of the session spent in HR zone 0 (the "rest more than lift" number)."""
    if not zones_min:
        return None
    total = 0.0
    z0 = 0.0
    for key, value in zones_min.items():
        try:
            minutes = float(value)
        except (TypeError, ValueError):
            continue
        total += minutes
        if str(key).lower() in {"z0", "zone0", "0"}:
            z0 = minutes
    if total <= 0:
        return None
    return 100.0 * z0 / total


def density(kcal: float | None, duration_min: float | None) -> float | None:
    if kcal is None or not duration_min:
        return None
    return kcal / duration_min


def meal_instant(meal: Meal) -> datetime:
    return ensure_utc(meal.eaten_at or meal.logged_at)


def in_lunch_window(local: datetime) -> bool:
    return LUNCH_WINDOW[0] <= local.time() < LUNCH_WINDOW[1]


def sleep_target_minutes(profile: Profile | None) -> int:
    """Asleep target from the agreed window (bed→wake minus 30 min), floor 6 h, default 7 h."""
    if profile is None or profile.wake_time is None or profile.bed_time is None:
        return DEFAULT_SLEEP_TARGET_MIN
    wake = profile.wake_time.hour * 60 + profile.wake_time.minute
    bed = profile.bed_time.hour * 60 + profile.bed_time.minute
    in_bed = (wake - bed) % 1440
    if in_bed == 0:
        return DEFAULT_SLEEP_TARGET_MIN
    return max(MIN_SLEEP_TARGET_MIN, in_bed - 30)


def sessions_per_week(profile: Profile | None) -> int:
    plan = profile.training_plan if profile is not None else None
    if not isinstance(plan, dict):
        return DEFAULT_SESSIONS_PER_WEEK
    raw = plan.get("sessions_per_week")
    try:
        value = int(raw) if raw is not None else DEFAULT_SESSIONS_PER_WEEK
    except (TypeError, ValueError):
        return DEFAULT_SESSIONS_PER_WEEK
    return max(1, value)


def allowed_workout_gap_days(profile: Profile | None) -> int:
    """Longest acceptable gap between sessions for the plan (3/week → 4 days)."""
    return math.ceil(7 / sessions_per_week(profile)) + 1


def normalise_item_name(name: str) -> str:
    return re.sub(r"[^a-zа-яё0-9 ]+", " ", name.lower()).strip()


# ---------------------------------------------------------------------------------- day facts


async def day_facts(
    session: AsyncSession,
    user_id: int,
    *,
    date_from: date,
    date_to: date,
    tz: str,
) -> list[DayKcal]:
    """Per-day intake facts for ``[date_from, date_to]`` (days without meals get zero rows)."""
    meals = await repo.list_meals_range(session, user_id, date_from, date_to)
    days = {d.date: d for d in await repo.list_days_range(session, user_id, date_from, date_to)}
    by_day: dict[date, list[Meal]] = {}
    for meal in meals:
        by_day.setdefault(meal.day_date, []).append(meal)
    out: list[DayKcal] = []
    current = date_from
    while current <= date_to:
        out.append(_build_day(current, by_day.get(current, []), days.get(current), tz))
        current += timedelta(days=1)
    return out


def _build_day(day: date, meals: list[Meal], row: Day | None, tz: str) -> DayKcal:
    total = Macros.zero()
    evening = 0.0
    had_lunch = False
    first_at: datetime | None = None
    for meal in meals:
        macros = repo.meal_macros(meal)
        total = total + macros
        local = to_local(meal_instant(meal), tz)
        if first_at is None or local < first_at:
            first_at = local
        if str(meal.slot) == "lunch" or (str(meal.slot) == "unknown" and in_lunch_window(local)):
            had_lunch = True
        if local.time() >= EVENING_FROM:
            evening += macros.kcal
    flags = tuple(str(f) for f in (row.flags or [])) if row is not None else ()
    return DayKcal(
        date=day,
        kcal=total.kcal,
        protein_g=total.protein_g,
        fiber_g=total.fiber_g,
        meals=len(meals),
        flags=flags,
        closed=bool(row is not None and row.closed_at is not None),
        had_lunch=had_lunch,
        evening_kcal=evening,
        first_meal_at=first_at,
    )


def skipped_lunch_stats(days: Iterable[DayKcal]) -> SkippedLunchStats:
    tracked = [d for d in days if d.meals > 0]
    skipped = [d for d in tracked if not d.had_lunch]
    with_lunch = [d for d in tracked if d.had_lunch]
    worst = sorted(skipped, key=lambda d: d.evening_kcal, reverse=True)[:3]
    return SkippedLunchStats(
        skipped_days=len(skipped),
        with_lunch_days=len(with_lunch),
        avg_evening_kcal_skipped=_mean([d.evening_kcal for d in skipped]),
        avg_evening_kcal_with_lunch=_mean([d.evening_kcal for d in with_lunch]),
        avg_day_kcal_skipped=_mean([d.kcal for d in skipped]),
        avg_day_kcal_with_lunch=_mean([d.kcal for d in with_lunch]),
        examples=tuple(worst),
    )


def is_off_day(day: DayKcal, kcal_target: float) -> bool:
    if "off" in day.flags:
        return True
    return day.meals > 0 and kcal_target > 0 and day.kcal > kcal_target * OVER_TARGET_RATIO


def weekend_blowups(days: Iterable[DayKcal], kcal_target: float) -> list[DayKcal]:
    """Saturdays/Sundays that went over the target or were flagged alcohol/off."""
    out: list[DayKcal] = []
    for day in days:
        if day.date.weekday() not in (5, 6):
            continue
        if is_off_day(day, kcal_target) or ("alcohol" in day.flags and day.meals > 0):
            out.append(day)
    return out


def same_meal_streak(
    per_day_items: Sequence[tuple[date, set[str]]], *, min_days: int = 5
) -> SameMealStreak | None:
    """An item name present on ``min_days`` consecutive days ending on the latest day."""
    if len(per_day_items) < min_days:
        return None
    ordered = sorted(per_day_items, key=lambda pair: pair[0])
    last_date, last_items = ordered[-1]
    best: SameMealStreak | None = None
    for name in sorted(last_items):
        count = 0
        expected = last_date
        for day, items in reversed(ordered):
            if day != expected or name not in items:
                break
            count += 1
            expected = day - timedelta(days=1)
        if count >= min_days and (best is None or count > best.days):
            best = SameMealStreak(name=name, days=count, last_date=last_date)
    return best


async def per_day_item_names(
    session: AsyncSession, user_id: int, *, date_from: date, date_to: date
) -> list[tuple[date, set[str]]]:
    meals = await repo.list_meals_range(session, user_id, date_from, date_to)
    by_day: dict[date, set[str]] = {}
    for meal in meals:
        names = by_day.setdefault(meal.day_date, set())
        for item in meal.items:
            names.add(normalise_item_name(item.name))
    return sorted(by_day.items())


# --------------------------------------------------------------------------------- sleep etc.


async def sleep_nights(
    session: AsyncSession, user_id: int, *, tz: str, n: int = 3
) -> list[SleepNight]:
    """Last ``n`` sleeps, newest first, expressed in local time."""
    stmt = (
        select(Sleep)
        .where(Sleep.user_id == user_id)
        .order_by(Sleep.ended_at.desc(), Sleep.id.desc())
        .limit(n)
    )
    rows = list((await session.scalars(stmt)).all())
    return [sleep_night(row, tz) for row in rows]


def sleep_night(row: Sleep, tz: str) -> SleepNight:
    ended = to_local(row.ended_at, tz)
    return SleepNight(
        night_of=ended.date(),
        started_local=to_local(row.started_at, tz),
        ended_local=ended,
        asleep_min=row.asleep_min,
        in_bed_min=row.in_bed_min,
        performance_pct=row.performance_pct,
    )


async def recent_recoveries(
    session: AsyncSession, user_id: int, *, today: date, days: int = 5
) -> list[tuple[date, float | None]]:
    rows: list[Recovery] = await repo.list_recoveries_range(
        session, user_id, today - timedelta(days=days), today
    )
    return [(row.date, row.score) for row in rows]


async def last_workout(session: AsyncSession, user_id: int) -> Workout | None:
    stmt = (
        select(Workout)
        .where(Workout.user_id == user_id)
        .order_by(Workout.started_at.desc(), Workout.id.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def workout_comparison(
    session: AsyncSession,
    user_id: int,
    *,
    sport: str,
    started_at: datetime,
    now: datetime,
    tz: str,
    exclude_id: int | None = None,
) -> WorkoutComparison:
    previous = await repo.last_same_sport(
        session, user_id, sport, before=started_at, exclude_id=exclude_id
    )
    avg = await repo.avg_30d(session, user_id, now=now, sport=sport)
    return WorkoutComparison(
        previous=None if previous is None else WorkoutFacts.from_row(previous, tz),
        avg_count=avg.count,
        avg_duration_min=avg.duration_min,
        avg_kcal=avg.kcal,
        avg_avg_hr=avg.avg_hr,
        avg_strain=avg.strain,
    )


async def recent_weights(
    session: AsyncSession, user_id: int, *, now: datetime, tz: str, days: int = 7
) -> list[tuple[datetime, float]]:
    rows = await repo.list_measurements_range(
        session,
        user_id,
        now - timedelta(days=days),
        now + timedelta(minutes=1),
        type=MeasurementType.weight,
    )
    return [(to_local(row.measured_at, tz), row.value) for row in rows]


async def measurement_statuses(
    session: AsyncSession, user_id: int, profile: Profile, *, now: datetime
) -> list[MeasurementStatus]:
    out: list[MeasurementStatus] = []
    for mtype, cadence in (
        (MeasurementType.waist, profile.waist_cadence_days),
        (MeasurementType.weight, profile.weight_cadence_days),
    ):
        latest = await repo.latest_by_type(session, user_id, mtype)
        if latest is None:
            days_since: int | None = None
            since_onboarding = (
                (ensure_utc(now) - ensure_utc(profile.onboarding_done_at)).days
                if profile.onboarding_done_at is not None
                else 0
            )
            overdue = max(0, since_onboarding - cadence)
            value = None
            unit = None
        else:
            days_since = max(0, (ensure_utc(now) - ensure_utc(latest.measured_at)).days)
            overdue = max(0, days_since - cadence)
            value = latest.value
            unit = latest.unit
        out.append(
            MeasurementStatus(
                type=str(mtype),
                cadence_days=cadence,
                days_since=days_since,
                last_value=value,
                last_unit=unit,
                days_overdue=overdue,
            )
        )
    return out


async def last_user_message_at(session: AsyncSession, user_id: int) -> datetime | None:
    stmt = (
        select(ConversationTurn.created_at)
        .where(ConversationTurn.user_id == user_id, ConversationTurn.role == TurnRole.user)
        .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
        .limit(1)
    )
    value = (await session.scalars(stmt)).first()
    return None if value is None else ensure_utc(value)


async def unanswered_sends(session: AsyncSession, user_id: int, *, since: datetime) -> int:
    stmt = select(ProactiveSend.id).where(
        ProactiveSend.user_id == user_id,
        ProactiveSend.sent_at >= since,
        ProactiveSend.responded_at.is_(None),
    )
    return len(list((await session.scalars(stmt)).all()))


async def sends_for_window(
    session: AsyncSession, user_id: int, window_key: str
) -> list[ProactiveSend]:
    stmt = (
        select(ProactiveSend)
        .where(ProactiveSend.user_id == user_id, ProactiveSend.window_key == window_key)
        .order_by(ProactiveSend.sent_at, ProactiveSend.id)
    )
    return list((await session.scalars(stmt)).all())


async def event_notes_for(
    session: AsyncSession, user_id: int, *, day: date, tz: str, now: datetime
) -> list[Note]:
    """Active event/commitment notes that expire on ``day`` (the agent dates events that way)."""
    start, end = local_day_bounds(day, tz)
    notes = await repo.list_active_notes(
        session, user_id, now=now, kinds=(NoteKind.event, NoteKind.commitment)
    )
    return [
        n for n in notes if n.expires_at is not None and start <= ensure_utc(n.expires_at) < end
    ]


async def yesterday_summary(
    session: AsyncSession, user_id: int, *, yesterday: date
) -> tuple[str | None, str | None]:
    summary: Summary | None = await repo.get_summary(session, user_id, SummaryKind.day, yesterday)
    day = await repo.get_day(session, user_id, yesterday)
    return (summary.text if summary else None, day.verdict if day else None)


# ------------------------------------------------------------------------------------ loader


async def load_history(
    session: AsyncSession,
    user: User,
    profile: Profile,
    protocol: Protocol | None,
    *,
    local_now: datetime,
    now: datetime,
    payload: dict[str, Any] | None = None,
    pattern_days: int = 30,
) -> HistoryFacts:
    """Load the ``HistoryFacts`` bundle for one fire. ~12 small queries; nothing is cached."""
    tz = user.timezone
    today = local_now.date()
    yesterday = today - timedelta(days=1)
    kcal_target = protocol.kcal if protocol is not None else 0.0
    payload = payload or {}

    days = await day_facts(
        session,
        user.id,
        date_from=today - timedelta(days=max(pattern_days, 56)),
        date_to=yesterday,
        tz=tz,
    )
    pattern_window = [d for d in days if d.date >= today - timedelta(days=pattern_days)]
    nights = await sleep_nights(session, user.id, tz=tz, n=3)
    recoveries = await recent_recoveries(session, user.id, today=today)
    last = await last_workout(session, user.id)

    comparison: WorkoutComparison | None = None
    sport = payload.get("sport")
    started_raw = payload.get("started_at")
    if isinstance(sport, str) and started_raw:
        started = _parse_dt(started_raw)
        if started is not None:
            exclude = payload.get("workout_id")
            comparison = await workout_comparison(
                session,
                user.id,
                sport=sport,
                started_at=started,
                now=now,
                tz=tz,
                exclude_id=int(exclude) if isinstance(exclude, int) else None,
            )

    weights = await recent_weights(session, user.id, now=now, tz=tz)
    measurements = await measurement_statuses(session, user.id, profile, now=now)
    items = await per_day_item_names(
        session, user.id, date_from=today - timedelta(days=10), date_to=today
    )
    streaks = await compute_streaks(
        session,
        user.id,
        today=today,
        tz=tz,
        kcal_target=kcal_target,
        bed_time=profile.bed_time,
    )
    card = await week_scorecard(
        session,
        user.id,
        week_start=week_start(today),
        tz=tz,
        targets=repo.protocol_targets(protocol),
        bed_time=profile.bed_time,
        through=today,
    )
    last_msg = await last_user_message_at(session, user.id)
    day_start, _ = local_day_bounds(today, tz)
    unanswered = await unanswered_sends(session, user.id, since=day_start)
    reminders = await repo.pending_reminders(session, user.id, due_before=now)
    y_summary, y_verdict = await yesterday_summary(session, user.id, yesterday=yesterday)

    return HistoryFacts(
        recent_days=tuple(pattern_window),
        skipped_lunch=skipped_lunch_stats(pattern_window),
        weekend_blowups=tuple(weekend_blowups(days, kcal_target)),
        sleep_nights=tuple(nights),
        sleep_target_min=sleep_target_minutes(profile),
        recent_recoveries=tuple(recoveries),
        last_workout=None if last is None else WorkoutFacts.from_row(last, tz),
        workout_comparison=comparison,
        recent_weights=tuple(weights),
        measurements=tuple(measurements),
        same_meal=same_meal_streak(items),
        streaks=streaks,
        scorecard=card,
        last_user_message_at=last_msg
        or (ensure_utc(user.last_seen_at) if user.last_seen_at else None),
        unanswered_sends=unanswered,
        pending_reminders=tuple(reminders),
        yesterday_summary=y_summary,
        yesterday_verdict=y_verdict,
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        try:
            return ensure_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None
