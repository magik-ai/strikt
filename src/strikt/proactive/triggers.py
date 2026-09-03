"""Trigger preconditions: pure functions over today's ``DayState`` and the history facts.

One ``check_<name>`` per ``TriggerName`` (brief §7.1). Each returns a ``TriggerFire`` whose
``facts`` carry the concrete numbers the message must quote (protein so far, hours since wake,
evening kcal on the last skipped-lunch days…), or ``None`` when the precondition does not hold.
No IO here: the engine loads the ``TriggerContext`` through ``store.load_history``.

Class A = time-based silence detection, B = data events from integrations, C = patterns from
history and notes. Thresholds scale with the user's protocol (nothing hard-codes one user's
numbers): protein check at 70 % of the protein target by 18:00, fiber check at a third of the
fiber target by 13:30, "off day" at +15 % kcal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, cast

from strikt.core.clock import ensure_utc, to_local
from strikt.core.types import DayState, Macros, MealView
from strikt.db.models import Note, ProactiveSend, Profile, Protocol, Summary
from strikt.proactive.stats import minutes_after_noon
from strikt.proactive.store import (
    HistoryFacts,
    WorkoutFacts,
    allowed_workout_gap_days,
    is_off_day,
    sessions_per_week,
)
from strikt.proactive.types import TriggerClass, TriggerFire, TriggerName

# ------------------------------------------------------------------------------- thresholds

FIRST_MEAL_GRACE = timedelta(hours=3)
LUNCH_DEADLINE = time(15, 0)
FIBER_CHECK_AT = time(13, 30)
PROTEIN_CHECK_AT = time(18, 0)
DINNER_DEADLINE = time(21, 0)
CLOSE_DEADLINE = time(23, 0)
DINNER_FROM = time(17, 0)
LUNCH_FROM = time(11, 0)
LUNCH_TO = time(16, 0)
PROTEIN_CHECK_RATIO = 0.7
FIBER_CHECK_RATIO = 1 / 3
DEFAULT_PROTEIN_THRESHOLD = 150.0
DEFAULT_FIBER_THRESHOLD = 10.0
RECOVERY_LOW = 40.0
RECOVERY_HIGH = 67.0
RECOVERY_BAD = 50.0
WAKE_LATE_MIN = 30
ONSET_LATE_MIN = 30
SILENCE_HOURS = 24.0
DEFAULT_WAKE = time(8, 0)
DEFAULT_BED = time(0, 30)
CLEAN_STREAK_EVERY = 3
FRIDAY = 4


@dataclass(frozen=True, kw_only=True)
class TriggerContext:
    """Everything a precondition may read. Built by the engine per fire."""

    local_now: datetime
    tz: str
    profile: Profile
    protocol: Protocol | None
    targets: Macros
    history: HistoryFacts
    recent_summaries: list[Summary] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    last_sends: list[ProactiveSend] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def day(self) -> date:
        return self.local_now.date()

    @property
    def wake_time(self) -> time:
        return self.profile.wake_time or DEFAULT_WAKE

    @property
    def bed_time(self) -> time:
        return self.profile.bed_time or DEFAULT_BED

    @property
    def clock(self) -> str:
        return self.local_now.strftime("%H:%M")


Precondition = Callable[[DayState | None, TriggerContext], TriggerFire | None]

# ---------------------------------------------------------------------------------- helpers


def _fire(
    name: TriggerName,
    klass: TriggerClass,
    ctx: TriggerContext,
    *,
    window_key: str | None = None,
    facts: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> TriggerFire:
    base: dict[str, Any] = {"local_time": ctx.clock, "date": ctx.day.isoformat()}
    base.update(facts or {})
    return TriggerFire(
        name=name,
        klass=klass,
        window_key=window_key or f"{name}:{ctx.day.isoformat()}",
        local_now=ctx.local_now,
        day=ctx.day,
        facts=base,
        payload=dict(payload or ctx.payload),
    )


def _meal_local(meal: MealView, tz: str) -> datetime:
    return to_local(ensure_utc(meal.eaten_at or meal.logged_at), tz)


def _meals_between(state: DayState, tz: str, start: time, end: time | None) -> list[MealView]:
    out: list[MealView] = []
    for meal in state.meals:
        local_t = _meal_local(meal, tz).time()
        if local_t >= start and (end is None or local_t < end):
            out.append(meal)
    return out


def _has_slot(state: DayState, slot: str) -> bool:
    return any(meal.slot == slot for meal in state.meals)


def _totals(state: DayState, ctx: TriggerContext) -> dict[str, Any]:
    m = state.totals.macros
    return {
        "kcal_so_far": round(m.kcal),
        "protein_so_far": round(m.protein_g),
        "carbs_so_far": round(m.carbs_g),
        "fat_so_far": round(m.fat_g),
        "fiber_so_far": round(m.fiber_g),
        "meals_logged": len(state.meals),
        "kcal_remaining": round(state.remaining.kcal),
        "protein_remaining": round(state.remaining.protein_g),
        "kcal_target": round(ctx.targets.kcal),
        "protein_target": round(ctx.targets.protein_g),
        "fiber_target": round(ctx.targets.fiber_g),
    }


def _last_meal_time(state: DayState, tz: str) -> str | None:
    if not state.meals:
        return None
    return max(_meal_local(m, tz) for m in state.meals).strftime("%H:%M")


def _at_or_after(ctx: TriggerContext, t: time) -> bool:
    return ctx.local_now.time() >= t


def _combine(day: date, t: time, ctx: TriggerContext) -> datetime:
    return datetime.combine(day, t, tzinfo=ctx.local_now.tzinfo)


def _skipped_lunch_facts(ctx: TriggerContext) -> dict[str, Any]:
    stats = ctx.history.skipped_lunch
    return stats.as_facts() if stats is not None else {}


def _hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def _payload_dt(ctx: TriggerContext, key: str) -> datetime | None:
    raw = ctx.payload.get(key)
    if isinstance(raw, datetime):
        return to_local(raw, ctx.tz)
    if isinstance(raw, str):
        try:
            return to_local(datetime.fromisoformat(raw), ctx.tz)
        except ValueError:
            return None
    return None


def _payload_float(ctx: TriggerContext, key: str) -> float | None:
    raw = ctx.payload.get(key)
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _open_day(state: DayState | None) -> DayState | None:
    """The DayState when today is still open; None when unknown or closed."""
    if state is None or state.closed:
        return None
    return state


# ------------------------------------------------------------------------- class A: time


def check_morning_line(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Wake + 0:15: yesterday's close, recovery, overdue measurements, wake adherence."""
    if ctx.local_now.time() < ctx.wake_time:
        return None
    h = ctx.history
    facts: dict[str, Any] = {
        "wake_time": _hhmm(ctx.wake_time),
        "yesterday": h.recent_days[-1].as_facts() if h.recent_days else None,
        "yesterday_verdict": h.yesterday_verdict,
        "yesterday_summary": h.yesterday_summary,
        "recovery": None,
        "measurements_overdue": [m.as_facts() for m in h.measurements if m.days_overdue > 0],
        "plan_set": bool(state is not None and state.plan),
        "streaks": h.streaks.as_facts(),
    }
    if state is not None and state.recovery is not None:
        facts["recovery"] = {
            "score": state.recovery.score,
            "rhr": state.recovery.rhr,
            "hrv_ms": state.recovery.hrv_ms,
        }
    if h.sleep_nights and h.sleep_nights[0].night_of == ctx.day:
        night = h.sleep_nights[0]
        woke = minutes_after_noon(night.ended_local.time()) - minutes_after_noon(ctx.wake_time)
        facts["last_night"] = night.as_facts()
        facts["woke_minutes_late"] = woke
    return _fire("morning_line", "time", ctx, facts=facts)


def check_no_first_meal(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Wake + 3 h and nothing logged."""
    day = _open_day(state)
    if day is None or day.meals:
        return None
    deadline = _combine(ctx.day, ctx.wake_time, ctx) + FIRST_MEAL_GRACE
    if ctx.local_now < deadline:
        return None
    since_wake = ctx.local_now - _combine(ctx.day, ctx.wake_time, ctx)
    facts: dict[str, Any] = {
        "wake_time": _hhmm(ctx.wake_time),
        "hours_since_wake": round(since_wake.total_seconds() / 3600, 1),
        **_totals(day, ctx),
        **_skipped_lunch_facts(ctx),
    }
    return _fire("no_first_meal", "time", ctx, facts=facts)


def check_no_lunch(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """15:00 and no meal in the 11:00–16:00 window (or tagged lunch)."""
    day = _open_day(state)
    if day is None or not _at_or_after(ctx, LUNCH_DEADLINE):
        return None
    if _has_slot(day, "lunch") or _meals_between(day, ctx.tz, LUNCH_FROM, LUNCH_TO):
        return None
    first = min((_meal_local(m, ctx.tz) for m in day.meals), default=None)
    facts: dict[str, Any] = {
        "first_meal_at": None if first is None else first.strftime("%H:%M"),
        "last_meal_at": _last_meal_time(day, ctx.tz),
        **_totals(day, ctx),
        **_skipped_lunch_facts(ctx),
    }
    return _fire("no_lunch", "time", ctx, facts=facts)


def check_fiber_check(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """13:30 and fiber under a third of the target."""
    day = _open_day(state)
    if day is None or not _at_or_after(ctx, FIBER_CHECK_AT):
        return None
    target = ctx.targets.fiber_g
    threshold = round(target * FIBER_CHECK_RATIO) if target > 0 else DEFAULT_FIBER_THRESHOLD
    so_far = day.totals.macros.fiber_g
    if so_far >= threshold:
        return None
    facts = {
        "fiber_threshold": threshold,
        "fiber_gap_to_target": round(max(0.0, target - so_far)),
        "likes": list(ctx.profile.likes or []),
        "meal_sources": list(ctx.profile.meal_sources or []),
        **_totals(day, ctx),
    }
    return _fire("fiber_check", "time", ctx, facts=facts)


def check_protein_check(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """18:00 and protein under 70 % of the target (150 g on a 210 g protocol)."""
    day = _open_day(state)
    if day is None or not _at_or_after(ctx, PROTEIN_CHECK_AT):
        return None
    target = ctx.targets.protein_g
    threshold = round(target * PROTEIN_CHECK_RATIO) if target > 0 else DEFAULT_PROTEIN_THRESHOLD
    so_far = day.totals.macros.protein_g
    if so_far >= threshold:
        return None
    facts = {
        "protein_threshold": threshold,
        "dinner_protein_needed": round(max(0.0, target - so_far)),
        "likes": list(ctx.profile.likes or []),
        **_totals(day, ctx),
    }
    return _fire("protein_check", "time", ctx, facts=facts)


def check_no_dinner(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """21:00 and no meal since 17:00 (or tagged dinner)."""
    day = _open_day(state)
    if day is None or not _at_or_after(ctx, DINNER_DEADLINE):
        return None
    if _has_slot(day, "dinner") or _meals_between(day, ctx.tz, DINNER_FROM, None):
        return None
    facts = {"last_meal_at": _last_meal_time(day, ctx.tz), **_totals(day, ctx)}
    return _fire("no_dinner", "time", ctx, facts=facts)


def check_day_not_closed(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """23:00 and the day is still open."""
    day = _open_day(state)
    if day is None or not _at_or_after(ctx, CLOSE_DEADLINE):
        return None
    dinner = _has_slot(day, "dinner") or bool(_meals_between(day, ctx.tz, DINNER_FROM, None))
    facts = {
        "dinner_logged": dinner,
        "last_meal_at": _last_meal_time(day, ctx.tz),
        "bed_time": _hhmm(ctx.bed_time),
        **_totals(day, ctx),
    }
    return _fire("day_not_closed", "time", ctx, facts=facts)


def check_bedtime_minus_30(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Thirty minutes before the agreed bedtime, every day (quiet hours do not apply)."""
    bed = minutes_after_noon(ctx.bed_time)
    now_m = minutes_after_noon(ctx.local_now.time())
    if now_m < bed - 45:
        return None
    nights = ctx.history.sleep_nights
    asleep = [n.asleep_min for n in nights if n.asleep_min is not None]
    facts: dict[str, Any] = {
        "bed_time": _hhmm(ctx.bed_time),
        "wake_time": _hhmm(ctx.wake_time),
        "sleep_target_min": ctx.history.sleep_target_min,
        "last_nights": [n.as_facts() for n in nights],
        "avg_asleep_min_3n": round(sum(asleep) / len(asleep)) if asleep else None,
        "day_closed": bool(state is not None and state.closed),
        "bedtime_hits_streak": ctx.history.streaks.bedtime_hits,
    }
    if ctx.history.last_workout is not None:
        facts["last_workout"] = ctx.history.last_workout.as_facts()
    # the window is the night: a bedtime after midnight still belongs to the evening's date
    night_of = (
        ctx.day
        if now_m >= bed - 45 and ctx.local_now.time() >= time(12, 0)
        else (ctx.day - timedelta(days=1))
    )
    return _fire(
        "bedtime_minus_30", "time", ctx, window_key=f"bedtime_minus_30:{night_of}", facts=facts
    )


def check_wake_check(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Sleep ended more than 30 min past the agreed wake time."""
    ended = _payload_dt(ctx, "ended_at")
    if ended is None:
        nights = ctx.history.sleep_nights
        if not nights or nights[0].night_of != ctx.day:
            return None
        ended = nights[0].ended_local
    if ended.date() != ctx.day:
        return None
    late = minutes_after_noon(ended.time()) - minutes_after_noon(ctx.wake_time)
    if late <= WAKE_LATE_MIN:
        return None
    late_days = 0
    for night in ctx.history.sleep_nights:
        if minutes_after_noon(night.ended_local.time()) - minutes_after_noon(ctx.wake_time) > (
            WAKE_LATE_MIN
        ):
            late_days += 1
        else:
            break
    facts = {
        "wake_time": _hhmm(ctx.wake_time),
        "got_up": ended.strftime("%H:%M"),
        "minutes_late": late,
        "late_days_in_a_row": max(late_days, 1),
        "bed_time": _hhmm(ctx.bed_time),
        "proposed_bed_time": _shift(ctx.bed_time, -min(late, 60)),
    }
    return _fire("wake_check", "time", ctx, facts=facts)


def check_measurement_overdue(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Waist past its cadence (14 d) or weight past its cadence (7 d)."""
    overdue = [m for m in ctx.history.measurements if m.days_overdue > 0]
    if not overdue:
        return None
    worst = max(overdue, key=lambda m: m.days_overdue)
    facts = {
        "overdue": [m.as_facts() for m in overdue],
        "type": worst.type,
        "days_since": worst.days_since,
        "days_overdue": worst.days_overdue,
        "cadence_days": worst.cadence_days,
        "last_value": worst.last_value,
        "unit": worst.last_unit,
        "kpi": str(ctx.profile.primary_kpi) if ctx.profile.primary_kpi else None,
        "kpi_target": ctx.profile.kpi_target_low,
        "wake_time": _hhmm(ctx.wake_time),
    }
    return _fire("measurement_overdue", "time", ctx, facts=facts)


def check_weekly_review(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """The week in five lines (scheduler fires it Sunday 20:00)."""
    card = ctx.history.scorecard
    if card is None:
        return None
    facts = {
        "scorecard": card.as_facts(),
        "targets": {
            "kcal": round(ctx.targets.kcal),
            "protein_g": round(ctx.targets.protein_g),
            "fiber_g": round(ctx.targets.fiber_g),
        },
        "sessions_planned": sessions_per_week(ctx.profile),
        "streaks": ctx.history.streaks.as_facts(),
        "weekend_blowups": [d.as_facts() for d in ctx.history.weekend_blowups[-2:]],
    }
    return _fire(
        "weekly_review", "time", ctx, window_key=f"weekly_review:{card.week_start}", facts=facts
    )


def check_silence_check(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """No message from the user for a full day: ask why."""
    last = ctx.history.last_user_message_at
    if last is None:
        return None
    silent_h = (ensure_utc(ctx.local_now) - ensure_utc(last)).total_seconds() / 3600
    if silent_h < SILENCE_HOURS:
        return None
    facts = {
        "hours_silent": round(silent_h),
        "last_message_at": to_local(last, ctx.tz).strftime("%Y-%m-%d %H:%M"),
        "unanswered_nudges_today": ctx.history.unanswered_sends,
        "last_logged_day": ctx.history.recent_days[-1].as_facts()
        if ctx.history.recent_days
        else None,
    }
    return _fire("silence_check", "time", ctx, facts=facts)


def check_escalation_followup(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Re-run the parent precondition 45 min later; the ladder turns it into the next step."""
    parent = ctx.payload.get("parent")
    if (
        not isinstance(parent, str)
        or parent not in PRECONDITIONS
        or parent == ("escalation_followup")
    ):
        return None
    return PRECONDITIONS[cast("TriggerName", parent)](state, ctx)


# ------------------------------------------------------------------------- class B: data


def check_whoop_workout_synced(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """A synced workout: compare with the last same-sport session and the 30-day average."""
    sport = ctx.payload.get("sport")
    started = _payload_dt(ctx, "started_at")
    if not isinstance(sport, str) or started is None:
        return None
    zones = ctx.payload.get("zones_min")
    this = WorkoutFacts.from_values(
        sport=sport,
        started_at=started,
        tz=ctx.tz,
        duration_min=_payload_float(ctx, "duration_min"),
        kcal=_payload_float(ctx, "kcal"),
        avg_hr=_payload_float(ctx, "avg_hr"),
        max_hr=_payload_float(ctx, "max_hr"),
        strain=_payload_float(ctx, "strain"),
        zones_min=zones if isinstance(zones, dict) else None,
    )
    comparison = ctx.history.workout_comparison
    facts: dict[str, Any] = {"this": this.as_facts()}
    if comparison is not None:
        facts.update(comparison.as_facts())
        prev = comparison.previous
        if prev is not None:
            facts["deltas_vs_previous"] = {
                "duration_min": _delta(this.duration_min, prev.duration_min),
                "kcal": _delta(this.kcal, prev.kcal),
                "avg_hr": _delta(this.avg_hr, prev.avg_hr),
                "kcal_per_min": _delta(this.kcal_per_min, prev.kcal_per_min, 1),
            }
        facts["deltas_vs_avg_30d"] = {
            "duration_min": _delta(this.duration_min, comparison.avg_duration_min),
            "kcal": _delta(this.kcal, comparison.avg_kcal),
            "avg_hr": _delta(this.avg_hr, comparison.avg_avg_hr),
        }
    ended = _payload_dt(ctx, "ended_at")
    if ended is not None:
        facts["ended"] = ended.strftime("%H:%M")
        facts["bed_time"] = _hhmm(ctx.bed_time)
        facts["ended_within_2h_of_bed"] = (
            minutes_after_noon(ctx.bed_time) - minutes_after_noon(ended.time())
        ) <= 120
    key = ctx.payload.get("external_id") or started.isoformat()
    return _fire(
        "whoop_workout_synced", "data", ctx, window_key=f"whoop_workout_synced:{key}", facts=facts
    )


def check_whoop_recovery_low(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Recovery under 40 %: adjust the day."""
    score = _payload_float(ctx, "score")
    if score is None and state is not None and state.recovery is not None:
        score = state.recovery.score
    if score is None or score >= RECOVERY_LOW:
        return None
    facts = {
        "score": round(score),
        "rhr": _payload_float(ctx, "rhr"),
        "hrv_ms": _payload_float(ctx, "hrv_ms"),
        "training_planned_today": _training_day(ctx),
        "kcal_target": round(ctx.targets.kcal),
        "protein_target": round(ctx.targets.protein_g),
        "last_nights": [n.as_facts() for n in ctx.history.sleep_nights[:2]],
    }
    return _fire("whoop_recovery_low", "data", ctx, facts=facts)


def check_whoop_recovery_high(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Green recovery after a bad streak: sleep works, same bedtime tonight."""
    score = _payload_float(ctx, "score")
    if score is None and state is not None and state.recovery is not None:
        score = state.recovery.score
    if score is None or score < RECOVERY_HIGH:
        return None
    prior = [s for d, s in ctx.history.recent_recoveries if d < ctx.day and s is not None]
    bad = [s for s in prior[-3:] if s < RECOVERY_BAD]
    if len(bad) < 2:
        return None
    facts = {
        "score": round(score),
        "prior_scores": [round(s) for s in prior[-3:]],
        "bed_time": _hhmm(ctx.bed_time),
        "last_night": ctx.history.sleep_nights[0].as_facts() if ctx.history.sleep_nights else None,
    }
    return _fire("whoop_recovery_high", "data", ctx, facts=facts)


def check_whoop_no_workout(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """No session for longer than the plan allows (3/week → 4 days)."""
    last = ctx.history.last_workout
    if last is None:
        return None
    days_since = (ctx.day - last.started_local.date()).days
    allowed = allowed_workout_gap_days(ctx.profile)
    if days_since <= allowed:
        return None
    facts = {
        "days_since_last": days_since,
        "allowed_gap_days": allowed,
        "sessions_per_week": sessions_per_week(ctx.profile),
        "last_workout": last.as_facts(),
        "training_days": _training_days(ctx),
    }
    return _fire("whoop_no_workout", "data", ctx, facts=facts)


def check_scale_weight_received(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """A scale reading: trend versus the 7-day average, "that's water" after salty/alcohol."""
    value = _payload_float(ctx, "value")
    if value is None:
        return None
    measured = _payload_dt(ctx, "measured_at") or ctx.local_now
    prior = [w for at, w in ctx.history.recent_weights if at < measured - timedelta(minutes=1)]
    yesterday = ctx.history.recent_days[-1] if ctx.history.recent_days else None
    water_flags = (
        [f for f in yesterday.flags if f in ("salty", "alcohol")] if yesterday is not None else []
    )
    if len(prior) < 2 and not water_flags:
        return None
    avg = sum(prior) / len(prior) if prior else None
    facts = {
        "value": value,
        "unit": ctx.payload.get("unit", "kg"),
        "avg_7d": None if avg is None else round(avg, 1),
        "delta_vs_avg_7d": None if avg is None else round(value - avg, 1),
        "readings_7d": len(prior),
        "yesterday_flags": water_flags,
        "likely_water": bool(water_flags),
        "kpi": str(ctx.profile.primary_kpi) if ctx.profile.primary_kpi else None,
    }
    return _fire(
        "scale_weight_received",
        "data",
        ctx,
        window_key=f"scale_weight_received:{measured.date()}",
        facts=facts,
    )


def check_sleep_debt_accumulating(
    state: DayState | None, ctx: TriggerContext
) -> TriggerFire | None:
    """Three nights under the sleep target: escalate from reminder to intervention."""
    nights = [n for n in ctx.history.sleep_nights[:3] if n.asleep_min is not None]
    target = ctx.history.sleep_target_min
    if len(nights) < 3 or any(n.asleep_min is None or n.asleep_min >= target for n in nights):
        return None
    deficit = sum(target - (n.asleep_min or 0) for n in nights)
    facts = {
        "nights": [n.as_facts() for n in nights],
        "sleep_target_min": target,
        "total_deficit_min": round(deficit),
        "bed_time": _hhmm(ctx.bed_time),
        "wake_time": _hhmm(ctx.wake_time),
        "proposed_bed_time": _shift(ctx.bed_time, -30),
    }
    return _fire("sleep_debt_accumulating", "data", ctx, facts=facts)


def check_sleep_onset_late(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Sleep onset more than 30 min after the agreed bedtime."""
    started = _payload_dt(ctx, "started_at")
    if started is None:
        nights = ctx.history.sleep_nights
        if not nights or nights[0].night_of != ctx.day:
            return None
        started = nights[0].started_local
    late = minutes_after_noon(started.time()) - minutes_after_noon(ctx.bed_time)
    if late <= ONSET_LATE_MIN:
        return None
    facts = {
        "bed_time": _hhmm(ctx.bed_time),
        "onset": started.strftime("%H:%M"),
        "minutes_late": late,
        "last_workout": (ctx.history.last_workout.as_facts() if ctx.history.last_workout else None),
    }
    return _fire("sleep_onset_late", "data", ctx, facts=facts)


# ---------------------------------------------------------------------- class C: pattern


def check_weekend_risk(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Friday, with weekend blowups on record: plan the one meal now."""
    if ctx.day.weekday() != FRIDAY:
        return None
    blowups = ctx.history.weekend_blowups
    if not blowups:
        return None
    over = [d.kcal - ctx.targets.kcal for d in blowups if ctx.targets.kcal > 0 and d.meals > 0]
    facts = {
        "past_blowups": [d.as_facts() for d in blowups[-3:]],
        "blowup_count_8w": len(blowups),
        "avg_over_target_kcal": round(sum(over) / len(over)) if over else None,
        "kcal_target": round(ctx.targets.kcal),
        "comfort_food": ctx.profile.comfort_food,
        "alcohol": ctx.profile.alcohol,
    }
    return _fire("weekend_risk", "pattern", ctx, facts=facts)


def check_two_off_days(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Yesterday and the day before both over: today's structure is not negotiable."""
    days = ctx.history.recent_days
    if len(days) < 2:
        return None
    last_two = days[-2:]
    if last_two[-1].date != ctx.day - timedelta(days=1):
        return None
    if not all(is_off_day(d, ctx.targets.kcal) for d in last_two):
        return None
    facts = {
        "off_days": [d.as_facts() for d in last_two],
        "over_by_kcal": [round(d.kcal - ctx.targets.kcal) for d in last_two],
        "kcal_target": round(ctx.targets.kcal),
        "wake_time": _hhmm(ctx.wake_time),
        "kpi": str(ctx.profile.primary_kpi) if ctx.profile.primary_kpi else None,
        "kpi_target": ctx.profile.kpi_target_low,
    }
    return _fire("two_off_days", "pattern", ctx, facts=facts)


def check_same_meal_streak(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """The same item five days running: offer variety before boredom turns into a blowup."""
    streak = ctx.history.same_meal
    if streak is None:
        return None
    facts = {
        "item": streak.name,
        "days": streak.days,
        "likes": list(ctx.profile.likes or []),
        "dislikes": list(ctx.profile.dislikes or []),
    }
    slug = streak.name.replace(" ", "_")[:40]
    return _fire(
        "same_meal_streak",
        "pattern",
        ctx,
        window_key=f"same_meal_streak:{slug}:{streak.last_date}",
        facts=facts,
    )


def check_event_planned(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """An event today (dinner, flight, trip) or a stored day plan: confirm the plan."""
    events = [n for n in ctx.notes if str(n.kind) in ("event", "commitment")]
    plan = state.plan if state is not None else None
    if not events and not plan:
        return None
    facts = {
        "events": [{"kind": str(n.kind), "text": n.text} for n in events],
        "plan": plan,
        "kcal_target": round(ctx.targets.kcal),
        "protein_target": round(ctx.targets.protein_g),
    }
    return _fire("event_planned", "pattern", ctx, facts=facts)


def check_post_travel_reentry(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """First day back after travel-flagged days: a tight plan and no scale."""
    days = ctx.history.recent_days
    if not days or days[-1].date != ctx.day - timedelta(days=1) or "travel" not in days[-1].flags:
        return None
    if state is not None and "travel" in state.flags:
        return None
    travel_days = 0
    for d in reversed(days):
        if "travel" not in d.flags:
            break
        travel_days += 1
    weight = [m for m in ctx.history.measurements if m.type == "weight"]
    facts = {
        "travel_days": travel_days,
        "kcal_target": round(ctx.targets.kcal),
        "protein_target": round(ctx.targets.protein_g),
        "wake_time": _hhmm(ctx.wake_time),
        "last_weight": weight[0].as_facts() if weight else None,
    }
    return _fire("post_travel_reentry", "pattern", ctx, facts=facts)


def check_clean_streak(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """Three clean days (and every third after): say so once, then back off."""
    streaks = ctx.history.streaks
    if streaks.clean_days < CLEAN_STREAK_EVERY or streaks.clean_days % CLEAN_STREAK_EVERY:
        return None
    start = streaks.clean_streak_start or ctx.day
    facts = {
        "clean_days": streaks.clean_days,
        "streak_start": start.isoformat(),
        "three_meal_days": streaks.three_meal_days,
        "bedtime_hits": streaks.bedtime_hits,
        "next_weekend_in_days": (5 - ctx.day.weekday()) % 7,
    }
    return _fire(
        "clean_streak",
        "pattern",
        ctx,
        window_key=f"clean_streak:{start}:{streaks.clean_days}",
        facts=facts,
    )


def check_intensity_restored(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """The temporary intensity expired: back to normal pressure, said once."""
    p = ctx.profile
    if p.temp_intensity is None or p.temp_intensity_until is None:
        return None
    until = ensure_utc(p.temp_intensity_until)
    if until > ensure_utc(ctx.local_now):
        return None
    facts = {
        "temporary_intensity": str(p.temp_intensity),
        "restored_intensity": str(p.coaching_intensity),
        "since": to_local(until, ctx.tz).date().isoformat(),
    }
    return _fire(
        "intensity_restored",
        "pattern",
        ctx,
        window_key=f"intensity_restored:{to_local(until, ctx.tz).date()}",
        facts=facts,
    )


def check_reminder_due(state: DayState | None, ctx: TriggerContext) -> TriggerFire | None:
    """A user-set reminder is due (payload from the scheduler's minute check)."""
    raw_id = ctx.payload.get("reminder_id")
    if raw_id is None:
        due = list(ctx.history.pending_reminders)
        if not due:
            return None
        reminder = due[0]
        raw_id, text, due_at = reminder.id, reminder.text, ensure_utc(reminder.due_at)
    else:
        text = str(ctx.payload.get("text", ""))
        due_at = _payload_dt(ctx, "due_at") or ctx.local_now
    facts = {
        "reminder_id": int(raw_id),
        "text": text,
        "due_at": to_local(due_at, ctx.tz).strftime("%H:%M"),
    }
    return _fire(
        "reminder_due",
        "pattern",
        ctx,
        window_key=f"reminder_due:{int(raw_id)}",
        facts=facts,
        payload={"reminder_id": int(raw_id), "text": text},
    )


# ---------------------------------------------------------------------------------- misc


def _delta(a: float | None, b: float | None, ndigits: int = 0) -> float | None:
    if a is None or b is None:
        return None
    return round(a - b, ndigits) if ndigits else round(a - b)


def _shift(t: time, minutes: int) -> str:
    total = (t.hour * 60 + t.minute + minutes) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def _training_days(ctx: TriggerContext) -> list[str]:
    plan = ctx.profile.training_plan
    if not isinstance(plan, dict):
        return []
    days = plan.get("days")
    return [str(d) for d in days] if isinstance(days, list) else []


def _training_day(ctx: TriggerContext) -> bool | None:
    days = [d.lower()[:3] for d in _training_days(ctx)]
    if not days:
        return None
    return ctx.day.strftime("%a").lower()[:3] in days


PRECONDITIONS: dict[TriggerName, Precondition] = {
    "morning_line": check_morning_line,
    "no_first_meal": check_no_first_meal,
    "no_lunch": check_no_lunch,
    "fiber_check": check_fiber_check,
    "protein_check": check_protein_check,
    "no_dinner": check_no_dinner,
    "day_not_closed": check_day_not_closed,
    "bedtime_minus_30": check_bedtime_minus_30,
    "wake_check": check_wake_check,
    "measurement_overdue": check_measurement_overdue,
    "weekly_review": check_weekly_review,
    "silence_check": check_silence_check,
    "escalation_followup": check_escalation_followup,
    "whoop_workout_synced": check_whoop_workout_synced,
    "whoop_recovery_low": check_whoop_recovery_low,
    "whoop_recovery_high": check_whoop_recovery_high,
    "whoop_no_workout": check_whoop_no_workout,
    "scale_weight_received": check_scale_weight_received,
    "sleep_debt_accumulating": check_sleep_debt_accumulating,
    "sleep_onset_late": check_sleep_onset_late,
    "weekend_risk": check_weekend_risk,
    "two_off_days": check_two_off_days,
    "same_meal_streak": check_same_meal_streak,
    "event_planned": check_event_planned,
    "post_travel_reentry": check_post_travel_reentry,
    "clean_streak": check_clean_streak,
    "intensity_restored": check_intensity_restored,
    "reminder_due": check_reminder_due,
}

TRIGGER_CLASS: dict[TriggerName, TriggerClass] = {
    name: (
        "time"
        if name
        in (
            "morning_line",
            "no_first_meal",
            "no_lunch",
            "fiber_check",
            "protein_check",
            "no_dinner",
            "day_not_closed",
            "bedtime_minus_30",
            "wake_check",
            "measurement_overdue",
            "weekly_review",
            "silence_check",
            "escalation_followup",
        )
        else "data"
        if name
        in (
            "whoop_workout_synced",
            "whoop_recovery_low",
            "whoop_recovery_high",
            "whoop_no_workout",
            "scale_weight_received",
            "sleep_debt_accumulating",
            "sleep_onset_late",
        )
        else "pattern"
    )
    for name in PRECONDITIONS
}


def precondition(name: TriggerName) -> Precondition:
    return PRECONDITIONS[name]
