"""Per-user timers on APScheduler 3.x ``AsyncIOScheduler`` (PLAN §8, brief §7.6).

``reschedule_user(user, profile)`` computes the cron jobs from the profile in the user's
timezone with stable ids ``user:<id>:<trigger>`` (``replace_existing`` makes a recompute
idempotent); ``remove_user`` drops them. Global jobs: ``integration_sync`` every 30 minutes,
``reminder_due`` every minute, and a per-user ``nightly_summary`` at 03:00 local. Follow-ups
are one-shot ``date`` jobs ``user:<id>:followup:<window_key>`` that fire the parent trigger
again through ``escalation_followup``.

The job table (local time; ``wake``/``bed`` from the profile, defaults 08:00 / 00:30):

    morning_line           wake + 0:15     event_planned, post_travel_reentry  wake + 0:20
    sleep_debt_accumulating wake + 0:45    no_first_meal                       wake + 3:00
    measurement_overdue    08:05           clean_streak, two_off_days, intensity_restored 09:00
    whoop_no_workout       10:00           silence_check 12:00     same_meal_streak 12:05
    fiber_check            13:30           no_lunch 15:00          weekend_risk Fri 17:00
    protein_check          18:00           weekly_review Sun 20:00 no_dinner 21:00
    day_not_closed         23:00           bedtime_minus_30 bed − 0:30
    nightly_summary        03:00 (callback into the memory module)

Brief §7 adds ``whoop_no_workout``, ``two_off_days``, ``same_meal_streak``, ``event_planned``,
``post_travel_reentry``, ``clean_streak`` and ``intensity_restored`` to PLAN §8's list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from strikt.core.clock import Clock, ensure_utc, to_local, zone
from strikt.db import repo
from strikt.db.models import Profile, User, UserStatus
from strikt.proactive.engine import ProactiveEngine
from strikt.proactive.types import SessionFactory, TriggerName

log = structlog.get_logger(__name__)

DEFAULT_WAKE = time(8, 0)
DEFAULT_BED = time(0, 30)
NIGHTLY_SUMMARY = "nightly_summary"
GLOBAL_SYNC_ID = "global:integration_sync"
GLOBAL_REMINDERS_ID = "global:reminder_due"
INTEGRATION_SYNC_MINUTES = 30
FOLLOWUP_GRACE_S = 600
CRON_GRACE_S = 900

NightlyCallback = Callable[[int, date], Awaitable[None]]
SyncCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class JobSpec:
    """One cron entry in the user's local time."""

    trigger: str  # a TriggerName, or NIGHTLY_SUMMARY
    hour: int
    minute: int
    day_of_week: str | None = None  # APScheduler names: mon..sun

    @property
    def at(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


def _plus(base: time, delta: timedelta) -> time:
    anchor = datetime.combine(date(2000, 1, 3), base, tzinfo=zone("UTC")) + delta
    return anchor.time().replace(tzinfo=None)


def build_job_specs(profile: Profile | None) -> list[JobSpec]:
    """The per-user job table from the profile (pure; tested against a fixture profile)."""
    wake = (profile.wake_time if profile is not None else None) or DEFAULT_WAKE
    bed = (profile.bed_time if profile is not None else None) or DEFAULT_BED
    plus15 = _plus(wake, timedelta(minutes=15))
    plus20 = _plus(wake, timedelta(minutes=20))
    plus45 = _plus(wake, timedelta(minutes=45))
    plus3h = _plus(wake, timedelta(hours=3))
    bed30 = _plus(bed, timedelta(minutes=-30))
    specs: list[tuple[str, time, str | None]] = [
        ("morning_line", plus15, None),
        ("event_planned", plus20, None),
        ("post_travel_reentry", plus20, None),
        ("sleep_debt_accumulating", plus45, None),
        ("no_first_meal", plus3h, None),
        ("measurement_overdue", time(8, 5), None),
        ("clean_streak", time(9, 0), None),
        ("two_off_days", time(9, 0), None),
        ("intensity_restored", time(9, 0), None),
        ("whoop_no_workout", time(10, 0), None),
        ("silence_check", time(12, 0), None),
        ("same_meal_streak", time(12, 5), None),
        ("fiber_check", time(13, 30), None),
        ("no_lunch", time(15, 0), None),
        ("weekend_risk", time(17, 0), "fri"),
        ("protein_check", time(18, 0), None),
        ("weekly_review", time(20, 0), "sun"),
        ("no_dinner", time(21, 0), None),
        ("day_not_closed", time(23, 0), None),
        ("bedtime_minus_30", bed30, None),
        (NIGHTLY_SUMMARY, time(3, 0), None),
    ]
    return [
        JobSpec(trigger=name, hour=at.hour, minute=at.minute, day_of_week=dow)
        for name, at, dow in specs
    ]


def job_id(user_id: int, trigger: str) -> str:
    return f"user:{user_id}:{trigger}"


def followup_job_id(user_id: int, window_key: str) -> str:
    return f"user:{user_id}:followup:{window_key}"


class ProactiveScheduler:
    """Owns the ``AsyncIOScheduler``; the engine calls back into it for follow-ups."""

    def __init__(
        self,
        engine: ProactiveEngine,
        session_factory: SessionFactory,
        clock: Clock,
        *,
        scheduler: AsyncIOScheduler | None = None,
        nightly_summary: NightlyCallback | None = None,
        integration_sync: SyncCallback | None = None,
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory
        self._clock = clock
        self._scheduler: Any = scheduler or AsyncIOScheduler(timezone=zone("UTC"))
        self._nightly = nightly_summary
        self._sync = integration_sync
        self._stopping = False
        engine.attach_followups(self)

    @property
    def scheduler(self) -> Any:
        return self._scheduler

    # --------------------------------------------------------------------------- lifecycle

    def start(self, *, paused: bool = False) -> None:
        """Start the scheduler (needs a running event loop). ``paused`` = no job processing."""
        if not self._scheduler.running:
            self._scheduler.start(paused=paused)
        self._stopping = False
        self.add_global_jobs()

    def resume(self) -> None:
        self._scheduler.resume()

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop job processing. APScheduler flips ``running`` on the next loop tick."""
        if self._scheduler.running and not self._stopping:
            self._stopping = True
            self._scheduler.shutdown(wait=wait)

    # ------------------------------------------------------------------------- user jobs

    def reschedule_user(self, user: User, profile: Profile | None) -> list[str]:
        """(Re)build the user's cron jobs; returns the job ids. Removes stale ids first."""
        self.remove_user(user.id, keep_followups=True)
        tz = zone(user.timezone)
        ids: list[str] = []
        for spec in build_job_specs(profile):
            jid = job_id(user.id, spec.trigger)
            trigger = CronTrigger(
                hour=spec.hour, minute=spec.minute, day_of_week=spec.day_of_week, timezone=tz
            )
            if spec.trigger == NIGHTLY_SUMMARY:
                self._scheduler.add_job(
                    self._run_nightly_summary,
                    trigger=trigger,
                    args=[user.id],
                    id=jid,
                    name=jid,
                    replace_existing=True,
                    coalesce=True,
                    misfire_grace_time=CRON_GRACE_S,
                )
            else:
                self._scheduler.add_job(
                    self._engine.fire,
                    trigger=trigger,
                    args=[user.id, spec.trigger],
                    id=jid,
                    name=jid,
                    replace_existing=True,
                    coalesce=True,
                    misfire_grace_time=CRON_GRACE_S,
                )
            ids.append(jid)
        log.info("proactive_rescheduled", user_id=user.id, jobs=len(ids), tz=user.timezone)
        return ids

    def remove_user(self, user_id: int, *, keep_followups: bool = False) -> int:
        prefix = f"user:{user_id}:"
        followup_prefix = followup_job_id(user_id, "")
        removed = 0
        for job in list(self._scheduler.get_jobs()):
            if not job.id.startswith(prefix):
                continue
            if keep_followups and job.id.startswith(followup_prefix):
                continue
            self._scheduler.remove_job(job.id)
            removed += 1
        return removed

    def user_job_ids(self, user_id: int) -> list[str]:
        prefix = f"user:{user_id}:"
        return sorted(job.id for job in self._scheduler.get_jobs() if job.id.startswith(prefix))

    async def reschedule_all(self) -> int:
        """Build jobs for every active user (call once at startup)."""
        count = 0
        async with self._session_factory() as session:
            users = await repo.list_users(session, statuses=[UserStatus.active])
            for user in users:
                profile = await repo.get_profile(session, user.id)
                self.reschedule_user(user, profile)
                count += 1
        return count

    # ------------------------------------------------------------------------- follow-ups

    def schedule_followup(
        self, user_id: int, parent: TriggerName, window_key: str, at: datetime
    ) -> str:
        jid = followup_job_id(user_id, window_key)
        self._scheduler.add_job(
            self._engine.fire,
            trigger=DateTrigger(run_date=ensure_utc(at)),
            args=[user_id, "escalation_followup"],
            kwargs={"payload": {"parent": parent, "window_key": window_key}},
            id=jid,
            name=jid,
            replace_existing=True,
            misfire_grace_time=FOLLOWUP_GRACE_S,
        )
        return jid

    def cancel_followups(
        self, user_id: int, *, window_prefixes: Sequence[str] | None = None
    ) -> int:
        base = followup_job_id(user_id, "")
        removed = 0
        for job in list(self._scheduler.get_jobs()):
            if not job.id.startswith(base):
                continue
            window = job.id[len(base) :]
            if window_prefixes is not None and not any(
                window.startswith(p) for p in window_prefixes
            ):
                continue
            self._scheduler.remove_job(job.id)
            removed += 1
        return removed

    def pending_followups(self, user_id: int) -> list[str]:
        base = followup_job_id(user_id, "")
        return sorted(
            job.id[len(base) :] for job in self._scheduler.get_jobs() if job.id.startswith(base)
        )

    # ------------------------------------------------------------------------ global jobs

    def add_global_jobs(self) -> list[str]:
        self._scheduler.add_job(
            self._run_integration_sync,
            trigger=IntervalTrigger(minutes=INTEGRATION_SYNC_MINUTES),
            id=GLOBAL_SYNC_ID,
            name=GLOBAL_SYNC_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=CRON_GRACE_S,
        )
        self._scheduler.add_job(
            self._run_reminder_checks,
            trigger=IntervalTrigger(minutes=1),
            id=GLOBAL_REMINDERS_ID,
            name=GLOBAL_REMINDERS_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=FOLLOWUP_GRACE_S,
        )
        return [GLOBAL_SYNC_ID, GLOBAL_REMINDERS_ID]

    async def _run_reminder_checks(self) -> int:
        """Every minute: fire ``reminder_due`` for each pending reminder whose time has come."""
        now = self._clock.now()
        async with self._session_factory() as session:
            due = await repo.pending_reminders(session, due_before=now)
        fired = 0
        for reminder in due:
            await self._engine.fire(
                reminder.user_id,
                "reminder_due",
                {
                    "reminder_id": reminder.id,
                    "text": reminder.text,
                    "due_at": ensure_utc(reminder.due_at).isoformat(),
                },
            )
            fired += 1
        return fired

    async def _run_integration_sync(self) -> None:
        if self._sync is None:
            return
        try:
            await self._sync()
        except Exception as exc:
            log.warning("integration_sync_failed", error=repr(exc))

    async def _run_nightly_summary(self, user_id: int) -> None:
        """03:00 local: summarise yesterday for the user (the memory module's callback)."""
        if self._nightly is None:
            return
        async with self._session_factory() as session:
            user = await repo.get_user(session, user_id)
        if user is None:
            return
        yesterday = to_local(self._clock.now(), user.timezone).date() - timedelta(days=1)
        try:
            await self._nightly(user_id, yesterday)
        except Exception as exc:
            log.warning("nightly_summary_failed", user_id=user_id, error=repr(exc))
