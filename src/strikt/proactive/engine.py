"""The proactive engine: trigger → precondition → ladder → decider → send → record → follow-up.

``ProactiveEngine.fire(user_id, name, payload)`` is the single entry point for every timer
(scheduler) and every event (bus). It is idempotent per ``window_key``: one send per window,
and a later escalation step in the same window is a new send only after the follow-up delay
(a duplicate timer inside the delay is dropped). Guards, in order:

1. user active, profile present, proactivity enabled (user-set reminders bypass this);
2. precondition holds (``triggers.PRECONDITIONS``) and produced facts;
3. window not answered / not exhausted (step 4 already sent) / not too soon;
4. quiet hours (except ``bedtime_minus_30`` and reminders); daily cap; three-day cooldown
   (class A pressure backs off, the meal-silence triggers still fire: "pressure returns on the
   first missed meal");
5. the decider writes the text or stays silent (never a template);
6. send, record in ``proactive_sends``, schedule the 45-minute follow-up when the trigger
   escalates.

Bus subscriptions: ``UserReplied`` resets every open ladder and cancels pending follow-ups;
``WorkoutEvent`` / ``RecoveryEvent`` / ``SleepEvent`` / ``MeasurementEvent`` feed the class B
triggers; ``DayStateChanged`` cancels the meal-window follow-ups for that day.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Protocol

import structlog

from strikt.config import Settings
from strikt.core.clock import Clock, ensure_utc, local_day_bounds, to_local
from strikt.core.types import DayState
from strikt.db import repo
from strikt.db.models import Profile, Protocol as ProtocolRow, User, UserStatus
from strikt.events import (
    DayStateChanged,
    EventBus,
    MeasurementEvent,
    RecoveryEvent,
    SleepEvent,
    UserReplied,
    WorkoutEvent,
)
from strikt.proactive import ladder as ladder_mod, store
from strikt.proactive.triggers import PRECONDITIONS, TriggerContext
from strikt.proactive.types import (
    Decider,
    ProactiveDecision,
    Sender,
    SessionFactory,
    StateProvider,
    TriggerFire,
    TriggerName,
)

log = structlog.get_logger(__name__)

FireStatus = Literal["sent", "silent", "skipped", "error"]

#: Triggers whose unanswered send gets a sharper follow-up 45 minutes later (brief §7.1/§7.2).
ESCALATING: frozenset[TriggerName] = frozenset(
    {
        "no_first_meal",
        "no_lunch",
        "no_dinner",
        "day_not_closed",
        "protein_check",
        "silence_check",
        "sleep_debt_accumulating",
        "two_off_days",
    }
)
#: Follow-ups cancelled when today's numbers change (a meal was logged, the day closed…).
MEAL_WINDOW_TRIGGERS: tuple[TriggerName, ...] = (
    "no_first_meal",
    "no_lunch",
    "no_dinner",
    "day_not_closed",
    "protein_check",
    "fiber_check",
)
#: Class A pressure that backs off during a three-clean-day cooldown. The meal-silence
#: triggers are deliberately not here: "pressure returns on the first missed meal".
COOLDOWN_SUPPRESSED: frozenset[TriggerName] = frozenset(
    {"fiber_check", "protein_check", "day_not_closed", "silence_check"}
)
#: Bypasses proactivity settings, quiet hours, the cap and the cooldown: the user asked for it.
USER_REQUESTED: frozenset[TriggerName] = frozenset({"reminder_due"})
#: Workouts/weights that arrived through the chat were already answered in the turn.
CHAT_SOURCES: frozenset[str] = frozenset({"manual", "screenshot", "internal"})
#: A backfill (first sync, re-sync) replays old rows: data older than this gets no message.
STALE_EVENT = timedelta(days=2)


@dataclass(frozen=True, kw_only=True)
class FireOutcome:
    name: TriggerName
    status: FireStatus
    reason: str
    window_key: str | None = None
    step: int = 0
    send_id: int | None = None
    message_id: int | None = None
    text: str = ""

    @property
    def sent(self) -> bool:
        return self.status == "sent"


class FollowupPlanner(Protocol):
    """Implemented by ``ProactiveScheduler``; the engine only knows this surface."""

    def schedule_followup(
        self, user_id: int, parent: TriggerName, window_key: str, at: datetime
    ) -> str: ...

    def cancel_followups(
        self, user_id: int, *, window_prefixes: Sequence[str] | None = None
    ) -> int: ...


def event_payload(event: Any) -> dict[str, Any]:
    """A JSON-friendly dict of an event dataclass (datetimes/dates → ISO strings)."""
    if not dataclasses.is_dataclass(event) or isinstance(event, type):
        return {}
    out: dict[str, Any] = {}
    for key, value in dataclasses.asdict(event).items():
        out[key] = _jsonable(value)
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class ProactiveEngine:
    def __init__(
        self,
        session_factory: SessionFactory,
        decider: Decider,
        state_provider: StateProvider,
        sender: Sender,
        clock: Clock,
        settings: Settings,
        bus: EventBus | None = None,
        *,
        followups: FollowupPlanner | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._decider = decider
        self._state_provider = state_provider
        self._sender = sender
        self._clock = clock
        self._settings = settings
        self._bus = bus
        self._followups = followups
        self._locks: dict[int, asyncio.Lock] = {}
        self._unsubscribe: list[Callable[[], None]] = []
        self.outcomes: list[FireOutcome] = []
        if bus is not None:
            self.subscribe(bus)

    # ------------------------------------------------------------------------------ wiring

    def attach_followups(self, planner: FollowupPlanner) -> None:
        self._followups = planner

    def subscribe(self, bus: EventBus) -> None:
        self._unsubscribe.extend(
            [
                bus.subscribe(UserReplied, self.on_user_replied),
                bus.subscribe(WorkoutEvent, self.on_workout),
                bus.subscribe(RecoveryEvent, self.on_recovery),
                bus.subscribe(SleepEvent, self.on_sleep),
                bus.subscribe(MeasurementEvent, self.on_measurement),
                bus.subscribe(DayStateChanged, self.on_day_state_changed),
            ]
        )

    def close(self) -> None:
        for unsub in self._unsubscribe:
            unsub()
        self._unsubscribe.clear()

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    # -------------------------------------------------------------------------------- fire

    async def fire(
        self, user_id: int, name: TriggerName, payload: dict[str, Any] | None = None
    ) -> FireOutcome:
        """Run one trigger for one user. Never raises: errors become ``FireOutcome.error``."""
        async with self._lock(user_id):
            try:
                outcome = await self._fire(user_id, name, payload or {})
            except Exception as exc:
                log.exception("proactive_fire_failed", user_id=user_id, trigger=name)
                outcome = FireOutcome(name=name, status="error", reason=repr(exc))
        self.outcomes.append(outcome)
        log.info(
            "proactive_fire",
            user_id=user_id,
            trigger=name,
            status=outcome.status,
            reason=outcome.reason,
            window=outcome.window_key,
            step=outcome.step,
        )
        return outcome

    async def _fire(self, user_id: int, name: TriggerName, payload: dict[str, Any]) -> FireOutcome:
        if name not in PRECONDITIONS:
            return FireOutcome(name=name, status="skipped", reason="unknown_trigger")
        requested = name in USER_REQUESTED
        now = self._clock.now()
        async with self._session_factory() as session:
            user = await repo.get_user(session, user_id)
            if user is None or user.status != UserStatus.active:
                return FireOutcome(name=name, status="skipped", reason="user_not_active")
            profile = await repo.get_profile(session, user_id)
            if profile is None:
                return FireOutcome(name=name, status="skipped", reason="no_profile")
            if not profile.proactive_enabled and not requested:
                return FireOutcome(name=name, status="skipped", reason="proactive_disabled")
            protocol = await repo.get_active_protocol(session, user_id)
            local_now = to_local(now, user.timezone)
            state = await self._day_state(session, user, local_now.date())
            ctx = await self._context(session, user, profile, protocol, local_now, now, payload)

            fire = PRECONDITIONS[name](state, ctx)
            if fire is None:
                return FireOutcome(name=name, status="skipped", reason="precondition_false")

            window = await ladder_mod.inspect_window(
                session,
                user_id,
                fire.window_key,
                now=now,
                last_user_message_at=ctx.history.last_user_message_at,
            )
            if window.last is not None and window.answered:
                return self._skip(fire, "window_answered")
            if window.exhausted:
                return self._skip(fire, "ladder_exhausted")
            if ladder_mod.too_soon(window, self._settings):
                return self._skip(fire, "duplicate_in_window")

            ladder = await ladder_mod.compute_ladder(
                session,
                user,
                fire,
                clock=self._clock,
                settings=self._settings,
                profile=profile,
                window=window,
                clean_streak_days=ctx.history.streaks.clean_days,
            )
            if not requested:
                if ladder.in_quiet_hours and not ladder_mod.quiet_exempt(fire.name):
                    return self._skip(fire, "quiet_hours", ladder.step)
                if ladder.sends_today >= ladder.cap_today:
                    return self._skip(fire, "daily_cap", ladder.step)
                if ladder_mod.in_cooldown(ladder) and fire.name in COOLDOWN_SUPPRESSED:
                    return self._skip(fire, "clean_streak_cooldown", ladder.step)

            decision = await self._decide(session, user, fire, ladder, state)
            if decision is None:
                return FireOutcome(
                    name=fire.name,
                    status="error",
                    reason="decider_failed",
                    window_key=fire.window_key,
                    step=ladder.step,
                )
            if not decision.send or not decision.text.strip():
                return FireOutcome(
                    name=fire.name,
                    status="silent",
                    reason=decision.reason or "decider_silent",
                    window_key=fire.window_key,
                    step=ladder.step,
                )

            step = max(1, min(decision.step or ladder.step, ladder_mod.MAX_STEP))
            step = max(step, ladder.step)  # the ladder never goes down inside a window
            try:
                message_id = await self._sender(user, decision.text)
            except Exception as exc:
                log.warning("proactive_send_failed", user_id=user_id, trigger=name, error=repr(exc))
                return FireOutcome(
                    name=fire.name,
                    status="error",
                    reason="send_failed",
                    window_key=fire.window_key,
                    step=step,
                )
            row = await repo.add_proactive_send(
                session,
                user_id,
                trigger=fire.name,
                window_key=fire.window_key,
                step=step,
                sent_at=now,
                text=decision.text,
                telegram_message_id=message_id,
            )
            await self._after_send(session, user_id, fire, now)
            await session.commit()

        if step < ladder_mod.MAX_STEP and fire.name in ESCALATING:
            self._schedule_followup(user_id, fire, now)
        return FireOutcome(
            name=fire.name,
            status="sent",
            reason="sent",
            window_key=fire.window_key,
            step=step,
            send_id=row.id,
            message_id=message_id,
            text=decision.text,
        )

    @staticmethod
    def _skip(fire: TriggerFire, reason: str, step: int = 0) -> FireOutcome:
        return FireOutcome(
            name=fire.name, status="skipped", reason=reason, window_key=fire.window_key, step=step
        )

    async def _day_state(self, session: Any, user: User, day: date) -> DayState | None:
        try:
            return await self._state_provider.day_state(session, user, day)
        except Exception as exc:
            log.warning("proactive_day_state_failed", user_id=user.id, error=repr(exc))
            return None

    async def _context(
        self,
        session: Any,
        user: User,
        profile: Profile,
        protocol: ProtocolRow | None,
        local_now: datetime,
        now: datetime,
        payload: dict[str, Any],
    ) -> TriggerContext:
        history = await store.load_history(
            session, user, profile, protocol, local_now=local_now, now=now, payload=payload
        )
        summaries = await repo.list_recent_summaries(session, user.id, "day", limit=3)
        notes = await store.event_notes_for(
            session, user.id, day=local_now.date(), tz=user.timezone, now=now
        )
        day_start, _ = local_day_bounds(local_now.date(), user.timezone)
        sends = await repo.list_sends_since(session, user.id, since=day_start)
        return TriggerContext(
            local_now=local_now,
            tz=user.timezone,
            profile=profile,
            protocol=protocol,
            targets=repo.protocol_targets(protocol),
            history=history,
            recent_summaries=summaries,
            notes=notes,
            last_sends=sends,
            payload=payload,
        )

    async def _decide(
        self, session: Any, user: User, fire: TriggerFire, ladder: Any, state: DayState | None
    ) -> ProactiveDecision | None:
        try:
            return await self._decider.decide(session, user, fire, ladder, state)
        except Exception as exc:
            log.warning(
                "proactive_decide_failed", user_id=user.id, trigger=fire.name, error=repr(exc)
            )
            return None

    async def _after_send(
        self, session: Any, user_id: int, fire: TriggerFire, now: datetime
    ) -> None:
        if fire.name == "reminder_due":
            reminder_id = fire.payload.get("reminder_id")
            if isinstance(reminder_id, int):
                await repo.mark_reminder_sent(session, user_id, reminder_id)
        elif fire.name == "intensity_restored":
            await repo.upsert_profile(
                session, user_id, {"temp_intensity": None, "temp_intensity_until": None}, now=now
            )

    def _schedule_followup(self, user_id: int, fire: TriggerFire, now: datetime) -> None:
        if self._followups is None:
            return
        at = ladder_mod.followup_at(now, self._settings)
        try:
            self._followups.schedule_followup(user_id, fire.name, fire.window_key, at)
        except Exception as exc:
            log.warning("proactive_followup_schedule_failed", user_id=user_id, error=repr(exc))

    def _cancel_followups(self, user_id: int, prefixes: Sequence[str] | None) -> int:
        if self._followups is None:
            return 0
        try:
            return self._followups.cancel_followups(user_id, window_prefixes=prefixes)
        except Exception as exc:
            log.warning("proactive_followup_cancel_failed", user_id=user_id, error=repr(exc))
            return 0

    # ------------------------------------------------------------------------- bus handlers

    async def on_user_replied(self, event: UserReplied) -> None:
        """Any reply resets every open ladder and cancels the pending follow-ups."""
        async with self._session_factory() as session:
            count = await repo.mark_responded(
                session, event.user_id, at=event.occurred_at, turn_id=event.turn_id
            )
            await session.commit()
        cancelled = self._cancel_followups(event.user_id, None)
        log.debug(
            "proactive_reset", user_id=event.user_id, marked=count, followups_cancelled=cancelled
        )

    def _stale(self, at: datetime) -> bool:
        return ensure_utc(at) < self._clock.now() - STALE_EVENT

    async def on_workout(self, event: WorkoutEvent) -> None:
        if event.source in CHAT_SOURCES or self._stale(event.ended_at or event.started_at):
            return
        await self.fire(event.user_id, "whoop_workout_synced", event_payload(event))

    async def on_recovery(self, event: RecoveryEvent) -> None:
        if self._stale(datetime.combine(event.date, time.min, tzinfo=UTC) + timedelta(days=1)):
            return
        payload = event_payload(event)
        await self.fire(event.user_id, "whoop_recovery_low", payload)
        await self.fire(event.user_id, "whoop_recovery_high", payload)

    async def on_sleep(self, event: SleepEvent) -> None:
        if self._stale(event.ended_at):
            return
        payload = event_payload(event)
        await self.fire(event.user_id, "wake_check", payload)
        await self.fire(event.user_id, "sleep_onset_late", payload)
        await self.fire(event.user_id, "sleep_debt_accumulating", payload)

    async def on_measurement(self, event: MeasurementEvent) -> None:
        if event.type != "weight" or event.source in CHAT_SOURCES or self._stale(event.measured_at):
            return
        await self.fire(event.user_id, "scale_weight_received", event_payload(event))

    async def on_day_state_changed(self, event: DayStateChanged) -> None:
        prefixes = [f"{name}:{event.date.isoformat()}" for name in MEAL_WINDOW_TRIGGERS]
        self._cancel_followups(event.user_id, prefixes)

    # ------------------------------------------------------------------------------ helpers

    def followup_delay(self) -> timedelta:
        return timedelta(minutes=self._settings.proactive_followup_minutes)
