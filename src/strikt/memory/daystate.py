"""Today's state from typed rows (PLAN §4 ``DayState``) and its compact context rendering.

``DayStateBuilder`` implements ``proactive.types.StateProvider``. It reads non-deleted meals,
the active protocol, workouts, the latest sleep ending that day, the recovery for the day,
measurement staleness against the profile's cadences and the ``days`` row (closed/flags/plan/
verdict). ``render_context`` is the plain-text block for the model (distinct from the Telegram
card in ``telegram.render``); ``yesterday_close_line`` is the one-liner the morning context
and the greeting use.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, ensure_utc, local_day_bounds, to_local
from strikt.core.types import (
    DayState,
    DayTotals,
    Macros,
    MealItemView,
    MealView,
    RecoveryView,
    Remaining,
    SleepView,
    WorkoutView,
)
from strikt.db import repo
from strikt.db.models import Meal, MeasurementType, Profile, Sleep, User, Workout
from strikt.memory import queries
from strikt.telegram.copy import resolve_lang, weekday_name

if TYPE_CHECKING:
    from strikt.config import Settings

log = structlog.get_logger(__name__)

DEFAULT_TARGETS = Macros(kcal=2000, protein_g=150, carbs_g=200, fat_g=60, fiber_g=25)
"""Used only when the user has no active protocol yet (onboarding not finished)."""

CONTEXT_MAX_CHARS = 2300  # ≈ 600 tokens at 4 chars/token (research/07 D3: today's rows ≤ 800)
MAX_CONTEXT_ITEM_NAME = 28
MAX_CONTEXT_MEALS = 10

_CADENCE_ATTR: dict[str, str] = {
    "waist": "waist_cadence_days",
    "weight": "weight_cadence_days",
}

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "open": "open",
        "closed": "closed",
        "yesterday": "Yesterday",
        "not_closed": "not closed",
        "nothing": "nothing logged",
        "flags": "flags",
    },
    "ru": {
        "open": "открыт",
        "closed": "закрыт",
        "yesterday": "Вчера",
        "not_closed": "не закрыт",
        "nothing": "ничего не записано",
        "flags": "флаги",
    },
}


def default_targets(settings: Settings | None) -> Macros:
    """Fallback targets from settings (fields are wishes; see the memory build report)."""
    if settings is None:
        return DEFAULT_TARGETS
    return Macros(
        kcal=float(getattr(settings, "default_target_kcal", DEFAULT_TARGETS.kcal)),
        protein_g=float(getattr(settings, "default_target_protein_g", DEFAULT_TARGETS.protein_g)),
        carbs_g=float(getattr(settings, "default_target_carbs_g", DEFAULT_TARGETS.carbs_g)),
        fat_g=float(getattr(settings, "default_target_fat_g", DEFAULT_TARGETS.fat_g)),
        fiber_g=float(getattr(settings, "default_target_fiber_g", DEFAULT_TARGETS.fiber_g)),
    )


# ------------------------------------------------------------------------------------- views


def meal_view(meal: Meal) -> MealView:
    items = [
        MealItemView(
            id=item.id,
            name=item.name,
            grams=item.grams,
            macros=repo.item_macros(item),
            countable=item.countable,
            confidence=item.confidence,
            flags=[str(f) for f in (item.flags or [])],
        )
        for item in meal.items
    ]
    return MealView(
        id=meal.id,
        slot=meal.slot.value,
        logged_at=ensure_utc(meal.logged_at),
        eaten_at=ensure_utc(meal.eaten_at) if meal.eaten_at else None,
        items=items,
        macros=repo.meal_macros(meal),
        note=meal.note,
    )


def workout_view(row: Workout) -> WorkoutView:
    zones = (
        {str(k): float(v) for k, v in row.zones_min.items() if v is not None}
        if row.zones_min
        else None
    )
    return WorkoutView(
        id=row.id,
        sport=row.sport,
        started_at=ensure_utc(row.started_at),
        ended_at=ensure_utc(row.ended_at) if row.ended_at else None,
        duration_min=row.duration_min,
        strain=row.strain,
        kcal=row.kcal,
        avg_hr=row.avg_hr,
        max_hr=row.max_hr,
        zones_min=zones,
        source=row.source.value,
    )


def sleep_view(row: Sleep) -> SleepView:
    return SleepView(
        started_at=ensure_utc(row.started_at),
        ended_at=ensure_utc(row.ended_at),
        in_bed_min=row.in_bed_min,
        asleep_min=row.asleep_min,
        performance_pct=row.performance_pct,
    )


def sum_meals(meals: list[Meal]) -> DayTotals:
    total = Macros.zero()
    items = 0
    for meal in meals:
        total = total + repo.meal_macros(meal)
        items += len(meal.items)
    return DayTotals(macros=total, items=items, meals=len(meals))


# ----------------------------------------------------------------------------------- builder


class DayStateBuilder:
    """Builds ``DayState`` for one local day. Implements ``StateProvider``.

    ``fallback_targets=False`` keeps zero targets when no protocol exists (the card then shows
    "no protocol yet"); the default falls back to ``settings`` defaults so budgets still work
    while onboarding is in progress.
    """

    def __init__(
        self,
        clock: Clock,
        settings: Settings | None = None,
        *,
        fallback_targets: bool = True,
    ) -> None:
        self._clock = clock
        self._defaults = default_targets(settings)
        self._fallback = fallback_targets

    async def day_state(self, session: AsyncSession, user: User, day: date) -> DayState:
        tz = user.timezone or "UTC"
        start, end = local_day_bounds(day, tz)

        meals = await repo.list_meals_for_date(session, user.id, day)
        protocol = await repo.get_active_protocol(session, user.id)
        if protocol is not None:
            targets = repo.protocol_targets(protocol)
        elif self._fallback:
            targets = self._defaults
        else:
            targets = Macros.zero()

        totals = sum_meals(meals)
        workouts = await repo.list_workouts_range(session, user.id, start, end)
        sleeps = await repo.list_sleep_range(session, user.id, start, end)
        latest_sleep = max(sleeps, key=lambda s: ensure_utc(s.ended_at)) if sleeps else None
        recovery = await repo.recovery_for_date(session, user.id, day)
        profile = await repo.get_profile(session, user.id)
        due = await self._measurements_due(session, user.id, profile, end)
        day_row = await repo.get_day(session, user.id, day)
        flags = [str(f) for f in (day_row.flags or [])] if day_row else []
        if "sick" in flags:
            # brief §3.6: protocol paused, no calorie targets on a sick day
            targets = Macros.zero()

        return DayState(
            date=day,
            totals=totals,
            targets=targets,
            remaining=Remaining.from_targets(targets, totals.macros),
            meals=[meal_view(m) for m in meals],
            workouts=[workout_view(w) for w in workouts],
            sleep=sleep_view(latest_sleep) if latest_sleep else None,
            recovery=(
                RecoveryView(
                    date=recovery.date,
                    score=recovery.score,
                    rhr=recovery.rhr,
                    hrv_ms=recovery.hrv_ms,
                    spo2=recovery.spo2,
                )
                if recovery
                else None
            ),
            measurements_due=due,
            closed=bool(day_row and day_row.closed_at),
            flags=flags,
            plan=dict(day_row.plan) if day_row and day_row.plan else None,
            verdict=day_row.verdict if day_row else None,
        )

    async def _measurements_due(
        self,
        session: AsyncSession,
        user_id: int,
        profile: Profile | None,
        day_end: datetime,
    ) -> list[str]:
        """Measurement types whose cadence has lapsed by the end of the day (or now, if today).

        A type that was never measured counts as due once onboarding is done: the interview
        collects the baseline, so its absence means the user skipped it.
        """
        if profile is None:
            return []
        reference = min(ensure_utc(self._clock.now()), day_end)
        order = ["waist", "weight"]
        if profile.primary_kpi and profile.primary_kpi.value in order:
            order.remove(profile.primary_kpi.value)
            order.insert(0, profile.primary_kpi.value)
        due: list[str] = []
        for mtype in order:
            cadence = int(getattr(profile, _CADENCE_ATTR[mtype]) or 0)
            if cadence <= 0:
                continue
            latest = await queries.latest_measurement_before(
                session, user_id, MeasurementType(mtype), before=reference
            )
            if latest is None:
                if profile.onboarding_done_at is not None:
                    due.append(mtype)
                continue
            days = (reference - ensure_utc(latest.measured_at)).days
            if days >= cadence:
                due.append(mtype)
        return due


# --------------------------------------------------------------------------------- rendering


def _n(value: float | None) -> str:
    return "?" if value is None else str(round(value))


def _macro_row(m: Macros) -> str:
    return f"{_n(m.kcal)} kcal | P {_n(m.protein_g)} | C {_n(m.carbs_g)} | F {_n(m.fat_g)} | fiber {_n(m.fiber_g)}"


def _short(name: str, limit: int = MAX_CONTEXT_ITEM_NAME) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _local_hhmm(dt: datetime, tz: str) -> str:
    return to_local(dt, tz).strftime("%H:%M")


def _meal_line(meal: MealView, tz: str, *, detailed: bool) -> str:
    when = _local_hhmm(meal.eaten_at or meal.logged_at, tz)
    parts: list[str] = []
    for item in meal.items:
        text = _short(item.name)
        if detailed:
            m = item.macros
            text += f" {_n(m.kcal)} kcal ({_n(m.protein_g)}P/{_n(m.carbs_g)}C/{_n(m.fat_g)}F"
            if m.fiber_g:
                text += f"/{_n(m.fiber_g)}fib"
            text += ")"
            if not item.countable:
                text += " ~loose"
            if item.flags:
                text += " [" + ",".join(item.flags) + "]"
        parts.append(text)
    line = f"- {when} {meal.slot} #{meal.id}: " + (", ".join(parts) or "—")
    line += f" = {_n(meal.macros.kcal)} kcal, P {_n(meal.macros.protein_g)}"
    if meal.note and detailed:
        line += f" — {_short(meal.note, 60)}"
    return line


def _workout_line(w: WorkoutView, tz: str) -> str:
    bits = [f"{w.sport} {_local_hhmm(w.started_at, tz)}"]
    if w.duration_min:
        bits.append(f"{_n(w.duration_min)} min")
    if w.strain is not None:
        bits.append(f"strain {w.strain:.1f}")
    if w.kcal:
        bits.append(f"{_n(w.kcal)} kcal")
    if w.avg_hr:
        bits.append(f"avg HR {w.avg_hr}")
    if w.max_hr:
        bits.append(f"max {w.max_hr}")
    if w.zones_min:
        zones = " ".join(f"{k}:{_n(v)}" for k, v in sorted(w.zones_min.items()))
        bits.append(f"zones(min) {zones}")
    return " · ".join(bits)


def _hm(minutes: float) -> str:
    hours, mins = divmod(round(minutes), 60)
    return f"{hours}h{mins:02d}"


def render_context(state: DayState, lang: str | None, *, tz: str = "UTC") -> str:
    """Compact plain-text block (< 600 tokens) describing one day for the model.

    Not the Telegram card: no HTML, no bars, every number present, ids on meals so the
    model can call ``update_meal``/``delete_meal`` without guessing.
    """
    lang = resolve_lang(lang)
    labels = _LABELS[lang]
    status = labels["closed"] if state.closed else labels["open"]
    head = [
        f"day {state.date.isoformat()} ({weekday_name(lang, state.date.weekday())}) {status}",
        f"totals: {_macro_row(state.totals.macros)} | {state.totals.meals} meals, {state.totals.items} items",
        f"targets: {_macro_row(state.targets)}",
        (
            f"remaining: {_n(state.remaining.kcal)} kcal | P {_n(state.remaining.protein_g)}"
            f" | C {_n(state.remaining.carbs_g)} | F {_n(state.remaining.fat_g)}"
            f" | fiber {_n(state.remaining.fiber_g)} (negative = over)"
        ),
    ]
    tail: list[str] = []
    if state.workouts:
        tail.append("training: " + "; ".join(_workout_line(w, tz) for w in state.workouts))
    if state.sleep:
        s = state.sleep
        bits = [f"{_local_hhmm(s.started_at, tz)}→{_local_hhmm(s.ended_at, tz)}"]
        if s.asleep_min:
            bits.append(f"asleep {_hm(s.asleep_min)}")
        if s.in_bed_min:
            bits.append(f"in bed {_hm(s.in_bed_min)}")
        if s.performance_pct is not None:
            bits.append(f"{_n(s.performance_pct)}%")
        tail.append("sleep: " + " · ".join(bits))
    if state.recovery:
        r = state.recovery
        bits = []
        if r.score is not None:
            bits.append(f"{_n(r.score)}%")
        if r.rhr is not None:
            bits.append(f"rhr {_n(r.rhr)}")
        if r.hrv_ms is not None:
            bits.append(f"hrv {_n(r.hrv_ms)} ms")
        if r.spo2 is not None:
            bits.append(f"spo2 {_n(r.spo2)}%")
        if bits:
            tail.append("recovery: " + " · ".join(bits))
    if state.measurements_due:
        tail.append("measurements due: " + ", ".join(state.measurements_due))
    if state.flags:
        tail.append("flags: " + ", ".join(state.flags))
    if state.plan:
        plan = "; ".join(f"{k}: {_short(str(v), 40)}" for k, v in sorted(state.plan.items()))
        tail.append("plan: " + _short(plan, 200))
    if state.verdict:
        tail.append("verdict: " + _short(state.verdict, 200))

    def assemble(detailed: bool, max_meals: int) -> str:
        if not state.meals:
            meals_block = ["meals: none logged"]
        else:
            shown = state.meals[:max_meals]
            meals_block = [f"meals ({len(state.meals)}):"]
            meals_block += [_meal_line(m, tz, detailed=detailed) for m in shown]
            hidden = len(state.meals) - len(shown)
            if hidden > 0:
                meals_block.append(f"- +{hidden} more meals (see get_day_state)")
        return "\n".join([*head, *meals_block, *tail])

    text = assemble(True, MAX_CONTEXT_MEALS)
    if len(text) <= CONTEXT_MAX_CHARS:
        return text
    text = assemble(False, MAX_CONTEXT_MEALS)
    max_meals = MAX_CONTEXT_MEALS
    while len(text) > CONTEXT_MAX_CHARS and max_meals > 1:
        max_meals -= 1
        text = assemble(False, max_meals)
    return text


async def yesterday_close_line(session: AsyncSession, user: User, day: date) -> str | None:
    """One line about the day before ``day``: verdict if closed, else totals, else None."""
    yesterday = day - timedelta(days=1)
    lang = resolve_lang(user.language)
    labels = _LABELS[lang]
    row = await repo.get_day(session, user.id, yesterday)
    meals = await repo.list_meals_for_date(session, user.id, yesterday)
    if row is None and not meals:
        return None
    totals = sum_meals(meals).macros
    label = f"{labels['yesterday']} ({yesterday.isoformat()}, {weekday_name(lang, yesterday.weekday())})"
    if meals:
        numbers = (
            f"{_n(totals.kcal)} kcal / P {_n(totals.protein_g)} / fiber {_n(totals.fiber_g)}"
            f" / {len(meals)} meals"
        )
    else:
        numbers = labels["nothing"]
    closed = bool(row and row.closed_at)
    line = f"{label}: {numbers}, {labels['closed'] if closed else labels['not_closed']}"
    if row and row.flags:
        line += f", {labels['flags']}: " + ", ".join(str(f) for f in row.flags)
    if row and row.verdict:
        line += f". {_short(row.verdict, 240)}"
    return line
