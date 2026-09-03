"""Shared helpers for the proactive tests: FakeDecider, a DB-backed state provider, seeders.

Not a test module (no ``test_`` functions); named ``test_proactive_*`` so it lives with its
package's tests and stays importable as ``tests.test_proactive_helpers``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc, local_datetime, to_local, zone
from strikt.core.types import (
    DayState,
    DayTotals,
    FoodItemIn,
    Macros,
    MealItemView,
    MealView,
    RecoveryView,
    Remaining,
)
from strikt.db import repo
from strikt.db.models import (
    CoachingIntensity,
    DataSource,
    MealSlot,
    MeasurementType,
    PrimaryKpi,
    Profile,
    Protocol,
    User,
)
from strikt.proactive.stats import Streaks
from strikt.proactive.store import HistoryFacts
from strikt.proactive.triggers import TriggerContext
from strikt.proactive.types import LadderState, ProactiveDecision, TriggerFire
from strikt.telegram.messenger import FakeMessenger

TZ = "Asia/Dubai"
TODAY = date(2026, 9, 3)  # Thursday


# --------------------------------------------------------------------------------- decider


@dataclass
class DeciderCall:
    fire: TriggerFire
    ladder: LadderState
    state: DayState | None


@dataclass
class FakeDecider:
    """Scripted decider: sends ``text`` (with the trigger name) unless ``silent``."""

    send: bool = True
    text: str = "{name} step {step}: {facts}"
    step: int | None = None
    raise_error: bool = False
    calls: list[DeciderCall] = field(default_factory=list)

    async def decide(
        self,
        session: AsyncSession,
        user: User,
        fire: TriggerFire,
        ladder: LadderState,
        state: DayState | None,
    ) -> ProactiveDecision:
        self.calls.append(DeciderCall(fire=fire, ladder=ladder, state=state))
        if self.raise_error:
            raise RuntimeError("llm down")
        step = self.step or ladder.step
        facts = ", ".join(
            f"{k}={v}" for k, v in sorted(fire.facts.items()) if k == "hours_since_wake"
        )
        return ProactiveDecision(
            send=self.send,
            text=self.text.format(name=fire.name, step=step, facts=facts),
            step=step,
            reason="scripted",
        )

    @property
    def last(self) -> DeciderCall:
        return self.calls[-1]


# ---------------------------------------------------------------------------- state provider


class DbStateProvider:
    """Builds a ``DayState`` from the meals/day/recovery rows (a small stand-in for memory/)."""

    async def day_state(self, session: AsyncSession, user: User, day: date) -> DayState:
        protocol = await repo.get_active_protocol(session, user.id)
        targets = repo.protocol_targets(protocol)
        meals = await repo.list_meals_for_date(session, user.id, day)
        views: list[MealView] = []
        total = Macros.zero()
        for meal in meals:
            macros = repo.meal_macros(meal)
            total = total + macros
            views.append(
                MealView(
                    id=meal.id,
                    slot=str(meal.slot),  # type: ignore[arg-type]
                    logged_at=ensure_utc(meal.logged_at),
                    eaten_at=ensure_utc(meal.eaten_at) if meal.eaten_at else None,
                    items=[
                        MealItemView(id=i.id, name=i.name, macros=repo.item_macros(i))
                        for i in meal.items
                    ],
                    macros=macros,
                )
            )
        row = await repo.get_day(session, user.id, day)
        recovery = await repo.recovery_for_date(session, user.id, day)
        return DayState(
            date=day,
            totals=DayTotals(
                macros=total, items=sum(len(m.items) for m in views), meals=len(views)
            ),
            targets=targets,
            remaining=Remaining.from_targets(targets, total),
            meals=views,
            recovery=(
                RecoveryView(
                    date=day, score=recovery.score, rhr=recovery.rhr, hrv_ms=recovery.hrv_ms
                )
                if recovery
                else None
            ),
            closed=bool(row is not None and row.closed_at is not None),
            flags=[str(f) for f in (row.flags or [])] if row else [],
            plan=row.plan if row else None,
        )


class BrokenStateProvider:
    async def day_state(self, session: AsyncSession, user: User, day: date) -> DayState:
        raise RuntimeError("no state")


# ----------------------------------------------------------------------------------- sender


def make_sender(messenger: FakeMessenger) -> Any:
    async def send(user: User, text: str) -> int | None:
        return await messenger.send(user.chat_id, text)

    return send


# ---------------------------------------------------------------------------------- seeding


def at_local(day: date, hhmm: str, tz: str = TZ) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return local_datetime(day, time(hour, minute), tz)


def item(
    name: str, kcal: float, p: float, c: float = 20, f: float = 10, fiber: float = 2
) -> FoodItemIn:
    return FoodItemIn(
        name=name, macros=Macros(kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=fiber)
    )


async def seed_meal(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    *,
    kcal: float = 500,
    protein: float = 40,
    fiber: float = 3,
    slot: str = "unknown",
    name: str = "meal",
    tz: str = TZ,
) -> None:
    when = at_local(day, hhmm, tz)
    await repo.add_meal_with_items(
        session,
        user_id,
        day_date=day,
        items=[item(name, kcal, protein, fiber=fiber)],
        slot=MealSlot(slot),
        logged_at=when,
        eaten_at=when,
    )


async def seed_day_flag(
    session: AsyncSession, user_id: int, day: date, flag: str, now: datetime
) -> None:
    await repo.set_day_flag(session, user_id, day, flag, True, now=now)


async def seed_sleep(
    session: AsyncSession,
    user_id: int,
    night_end: date,
    *,
    onset: str,
    woke: str,
    asleep_min: float,
    tz: str = TZ,
) -> None:
    """A night ending on ``night_end`` (onset before midnight belongs to the previous date)."""
    onset_day = night_end - timedelta(days=1) if int(onset[:2]) >= 12 else night_end
    started = at_local(onset_day, onset, tz)
    ended = at_local(night_end, woke, tz)
    await repo.upsert_sleep_by_external(
        session,
        user_id,
        source=DataSource.whoop,
        external_id=f"sleep-{night_end}",
        started_at=started,
        ended_at=ended,
        now=ended,
        asleep_min=asleep_min,
        in_bed_min=(ended - started).total_seconds() / 60,
        performance_pct=None,
    )


async def seed_workout(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    *,
    sport: str = "weightlifting",
    duration_min: float = 60,
    kcal: float = 400,
    avg_hr: int = 120,
    strain: float = 10.0,
    zones_min: dict[str, float] | None = None,
    external_id: str | None = None,
    tz: str = TZ,
) -> int:
    started = at_local(day, hhmm, tz)
    row, _ = await repo.upsert_workout_by_external(
        session,
        user_id,
        source=DataSource.whoop,
        external_id=external_id or f"w-{day}-{hhmm}",
        sport=sport,
        started_at=started,
        now=started,
        ended_at=started + timedelta(minutes=duration_min),
        duration_min=duration_min,
        kcal=kcal,
        avg_hr=avg_hr,
        max_hr=avg_hr + 40,
        strain=strain,
        zones_min=zones_min,
    )
    return row.id


async def seed_measurement(
    session: AsyncSession, user_id: int, mtype: str, value: float, when: datetime, unit: str = "kg"
) -> None:
    await repo.add_measurement(
        session,
        user_id,
        type=MeasurementType(mtype),
        value=value,
        unit=unit,
        measured_at=when,
        source="scale",
    )


# ----------------------------------------------------------------------- pure trigger context


def make_profile(**overrides: Any) -> Profile:
    values: dict[str, Any] = {
        "user_id": 1,
        "wake_time": time(8, 0),
        "bed_time": time(0, 30),
        "coaching_intensity": CoachingIntensity.pushy,
        "proactive_enabled": True,
        "quiet_start": time(0, 0),
        "quiet_end": time(7, 30),
        "waist_cadence_days": 14,
        "weight_cadence_days": 7,
        "onboarding_step": 10,
        "onboarding_done_at": datetime(2026, 8, 1, tzinfo=UTC),
        "primary_kpi": PrimaryKpi.waist,
        "kpi_target_low": 94,
        "updated_at": datetime(2026, 9, 1, tzinfo=UTC),
        "training_plan": {"days": ["mon", "wed", "fri"], "sessions_per_week": 3},
    }
    values.update(overrides)
    return Profile(**values)


def make_protocol(kcal: float = 2000, protein: float = 210, fiber: float = 30) -> Protocol:
    return Protocol(
        user_id=1,
        version=1,
        kcal=kcal,
        protein_g=protein,
        fat_g=105,
        carbs_g=75,
        fiber_g=fiber,
        active=True,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def make_ctx(
    local_time: str = "12:00",
    *,
    day: date = TODAY,
    history: HistoryFacts | None = None,
    payload: dict[str, Any] | None = None,
    profile: Profile | None = None,
    protocol: Protocol | None = None,
    notes: list[Any] | None = None,
    tz: str = TZ,
) -> TriggerContext:
    hour, minute = (int(x) for x in local_time.split(":"))
    local_now = datetime.combine(day, time(hour, minute), tzinfo=zone(tz))
    protocol = protocol or make_protocol()
    return TriggerContext(
        local_now=local_now,
        tz=tz,
        profile=profile or make_profile(),
        protocol=protocol,
        targets=repo.protocol_targets(protocol),
        history=history or HistoryFacts(streaks=Streaks()),
        notes=notes or [],
        payload=payload or {},
    )


def make_state(
    *,
    day: date = TODAY,
    meals: list[tuple[str, float, float, str]] | None = None,
    closed: bool = False,
    flags: list[str] | None = None,
    plan: dict[str, Any] | None = None,
    recovery: float | None = None,
    fiber: float = 0.0,
    tz: str = TZ,
    targets: Macros | None = None,
) -> DayState:
    """``meals`` = (local HH:MM, kcal, protein, slot)."""
    targets = targets or repo.protocol_targets(make_protocol())
    views: list[MealView] = []
    total = Macros.zero()
    for index, (hhmm, kcal, protein, slot) in enumerate(meals or [], start=1):
        macros = Macros(kcal=kcal, protein_g=protein, carbs_g=20, fat_g=10, fiber_g=fiber)
        total = total + macros
        when = at_local(day, hhmm, tz)
        views.append(
            MealView(id=index, slot=slot, logged_at=when, eaten_at=when, macros=macros)  # type: ignore[arg-type]
        )
    return DayState(
        date=day,
        totals=DayTotals(macros=total, items=len(views), meals=len(views)),
        targets=targets,
        remaining=Remaining.from_targets(targets, total),
        meals=views,
        closed=closed,
        flags=flags or [],
        plan=plan,
        recovery=RecoveryView(date=day, score=recovery) if recovery is not None else None,
    )


def local_str(dt: datetime, tz: str = TZ) -> str:
    return to_local(dt, tz).strftime("%Y-%m-%d %H:%M")
