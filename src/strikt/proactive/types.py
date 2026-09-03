"""Shared types for the proactivity engine.

Frozen contract between ``strikt.proactive`` (scheduler, triggers, ladder, engine) and
``strikt.agent.proactive_decide`` (the model-backed decider). Both packages import from here so
they can be built independently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.types import DayState
from strikt.db.models import User

TriggerClass = Literal["time", "data", "pattern"]

TriggerName = Literal[
    # class A: time-based (silence detection)
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
    # class B: data-based (integrations)
    "whoop_workout_synced",
    "whoop_recovery_low",
    "whoop_recovery_high",
    "whoop_no_workout",
    "scale_weight_received",
    "sleep_debt_accumulating",
    "sleep_onset_late",
    # class C: pattern-based (history + notes)
    "weekend_risk",
    "two_off_days",
    "same_meal_streak",
    "event_planned",
    "post_travel_reentry",
    "clean_streak",
    "intensity_restored",
    "reminder_due",
]


#: Triggers the user asked for (their own reminders): they bypass ``proactive_enabled``, quiet
#: hours and the daily cap, and they do not consume the cap either.
USER_REQUESTED: frozenset[TriggerName] = frozenset({"reminder_due"})


@dataclass(frozen=True, kw_only=True)
class TriggerFire:
    """One decision request for the decider.

    ``window_key`` identifies the idempotency window (for example ``"no_lunch:2026-09-03"``);
    the ladder counts unanswered sends per window. ``facts`` carries the concrete numbers the
    precondition computed (protein so far, hours since wake, last three same-window outcomes)
    so the message can quote real data. ``payload`` carries the raw event for data triggers.
    """

    name: TriggerName
    klass: TriggerClass
    window_key: str
    local_now: datetime
    day: date
    facts: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class LadderState:
    """Where the escalation ladder stands for this window before the new send."""

    step: int  # 1..4, the step the NEXT send would be
    sends_today: int
    cap_today: int
    intensity: str  # gentle | direct | pushy | drill_sergeant
    response_rate: float | None  # 0..1 for this trigger name, None when unknown
    clean_streak_days: int = 0
    in_quiet_hours: bool = False


@dataclass(frozen=True, kw_only=True)
class ProactiveDecision:
    """What the decider returned. ``send=False`` means stay silent this window."""

    send: bool
    text: str = ""
    step: int = 1
    reason: str = ""


class Decider(Protocol):
    """Writes the proactive message (or decides to stay silent). Implemented by the agent."""

    async def decide(
        self,
        session: AsyncSession,
        user: User,
        fire: TriggerFire,
        ladder: LadderState,
        state: DayState | None,
    ) -> ProactiveDecision: ...


class StateProvider(Protocol):
    """Builds today's DayState for a user. Implemented by ``strikt.memory.daystate``."""

    async def day_state(self, session: AsyncSession, user: User, day: date) -> DayState: ...


SessionFactory = Callable[[], Any]
Sender = Callable[[User, str], Awaitable[int | None]]
