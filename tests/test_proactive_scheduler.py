"""``ProactiveScheduler``: job ids per profile, recompute on change, follow-ups, global jobs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.config import Settings
from strikt.core.clock import FakeClock, zone
from strikt.db import repo
from strikt.db.engine import make_session_factory
from strikt.db.models import Profile, ReminderStatus, User, UserStatus
from strikt.proactive import engine as sched_engine, scheduler as sched
from strikt.proactive.engine import ProactiveEngine
from strikt.telegram.messenger import FakeMessenger
from tests.test_proactive_helpers import (
    TODAY,
    DbStateProvider,
    FakeDecider,
    make_profile,
    make_sender,
)

EXPECTED_TRIGGERS = {
    "morning_line",
    "event_planned",
    "post_travel_reentry",
    "sleep_debt_accumulating",
    "no_first_meal",
    "measurement_overdue",
    "clean_streak",
    "two_off_days",
    "intensity_restored",
    "whoop_no_workout",
    "silence_check",
    "same_meal_streak",
    "fiber_check",
    "no_lunch",
    "weekend_risk",
    "protein_check",
    "weekly_review",
    "no_dinner",
    "day_not_closed",
    "bedtime_minus_30",
    "nightly_summary",
}


@pytest.fixture
def decider() -> FakeDecider:
    return FakeDecider()


@pytest.fixture
async def proactive(
    engine: AsyncEngine,
    clock: FakeClock,
    settings: Settings,
    messenger: FakeMessenger,
    decider: FakeDecider,
) -> AsyncIterator[tuple[ProactiveEngine, sched.ProactiveScheduler]]:
    eng = ProactiveEngine(
        make_session_factory(engine),
        decider,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
    )
    calls: list[tuple[int, date]] = []

    async def nightly(user_id: int, day: date) -> None:
        calls.append((user_id, day))

    async def sync() -> None:
        calls.append((0, TODAY))

    ps = sched.ProactiveScheduler(
        eng,
        make_session_factory(engine),
        clock,
        scheduler=AsyncIOScheduler(timezone=zone("UTC")),
        nightly_summary=nightly,
        integration_sync=sync,
    )
    ps.nightly_calls = calls  # type: ignore[attr-defined]
    ps.start(paused=True)
    try:
        yield eng, ps
    finally:
        ps.shutdown()


def test_build_job_specs_from_profile() -> None:
    specs = {
        s.trigger: s
        for s in sched.build_job_specs(make_profile(wake_time=time(7, 30), bed_time=time(23, 45)))
    }
    assert set(specs) == EXPECTED_TRIGGERS
    assert specs["morning_line"].at == "07:45" and specs["no_first_meal"].at == "10:30"
    assert specs["sleep_debt_accumulating"].at == "08:15" and specs["event_planned"].at == "07:50"
    assert specs["bedtime_minus_30"].at == "23:15" and specs["weekend_risk"].day_of_week == "fri"
    assert specs["weekly_review"].day_of_week == "sun" and specs["weekly_review"].at == "20:00"
    assert specs["nightly_summary"].at == "03:00" and specs["no_lunch"].day_of_week is None
    defaults = {s.trigger: s for s in sched.build_job_specs(None)}
    assert defaults["no_first_meal"].at == "11:00" and defaults["bedtime_minus_30"].at == "00:00"
    assert sched.job_id(7, "no_lunch") == "user:7:no_lunch"
    assert sched.followup_job_id(7, "no_lunch:2026-09-03") == "user:7:followup:no_lunch:2026-09-03"


async def test_reschedule_user_builds_stable_ids_in_user_timezone(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler], user: User, profile: Profile
) -> None:
    _, ps = proactive
    ids = ps.reschedule_user(user, profile)
    assert set(ids) == {f"user:{user.id}:{t}" for t in EXPECTED_TRIGGERS}
    assert ps.user_job_ids(user.id) == sorted(ids)
    job = ps.scheduler.get_job(f"user:{user.id}:no_first_meal")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (
        fields["hour"] == "11"
        and fields["minute"] == "0"
        and str(job.trigger.timezone) == "Asia/Dubai"
    )
    weekend = ps.scheduler.get_job(f"user:{user.id}:weekend_risk")
    assert {f.name: str(f) for f in weekend.trigger.fields}["day_of_week"] == "fri"
    # the next run of no_first_meal is 11:00 Dubai = 07:00 UTC
    nxt = job.trigger.get_next_fire_time(None, datetime(2026, 9, 3, 6, 0, tzinfo=zone("UTC")))
    assert nxt.astimezone(zone("UTC")).hour == 7 and nxt.minute == 0
    assert job.args == (user.id, "no_first_meal")


async def test_reschedule_recomputes_on_profile_change_and_remove_user(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler], user: User, profile: Profile
) -> None:
    _, ps = proactive
    ps.reschedule_user(user, profile)
    before = len(ps.scheduler.get_jobs())
    profile.wake_time = time(6, 0)
    profile.bed_time = time(22, 30)
    ids = ps.reschedule_user(user, profile)
    assert len(ps.scheduler.get_jobs()) == before and set(ids) == set(ps.user_job_ids(user.id))
    fields = {
        f.name: str(f) for f in ps.scheduler.get_job(f"user:{user.id}:no_first_meal").trigger.fields
    }
    assert fields["hour"] == "9"
    bed = {
        f.name: str(f)
        for f in ps.scheduler.get_job(f"user:{user.id}:bedtime_minus_30").trigger.fields
    }
    assert bed["hour"] == "22" and bed["minute"] == "0"
    # follow-ups survive a reschedule, remove_user drops everything of the user
    ps.schedule_followup(
        user.id, "no_lunch", f"no_lunch:{TODAY}", datetime(2026, 9, 3, 12, 0, tzinfo=zone("UTC"))
    )
    ps.reschedule_user(user, profile)
    assert ps.pending_followups(user.id) == [f"no_lunch:{TODAY}"]
    removed = ps.remove_user(user.id)
    assert removed == len(EXPECTED_TRIGGERS) + 1 and ps.user_job_ids(user.id) == []
    assert {j.id for j in ps.scheduler.get_jobs()} == {
        sched.GLOBAL_SYNC_ID,
        sched.GLOBAL_REMINDERS_ID,
    }


async def test_followups_schedule_replace_and_cancel_by_prefix(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler], user: User
) -> None:
    _, ps = proactive
    at = datetime(2026, 9, 3, 7, 50, tzinfo=zone("UTC"))
    jid = ps.schedule_followup(user.id, "no_first_meal", f"no_first_meal:{TODAY}", at)
    ps.schedule_followup(
        user.id, "no_first_meal", f"no_first_meal:{TODAY}", at + timedelta(minutes=5)
    )  # replaces
    ps.schedule_followup(user.id, "no_lunch", f"no_lunch:{TODAY}", at)
    ps.schedule_followup(user.id, "silence_check", f"silence_check:{TODAY}", at)
    assert ps.pending_followups(user.id) == [
        f"no_first_meal:{TODAY}",
        f"no_lunch:{TODAY}",
        f"silence_check:{TODAY}",
    ]
    job = ps.scheduler.get_job(jid)
    assert job.kwargs == {
        "payload": {"parent": "no_first_meal", "window_key": f"no_first_meal:{TODAY}"}
    }
    assert job.trigger.run_date == at + timedelta(minutes=5) and job.args == (
        user.id,
        "escalation_followup",
    )
    assert (
        ps.cancel_followups(
            user.id, window_prefixes=[f"no_first_meal:{TODAY}", f"no_lunch:{TODAY}"]
        )
        == 2
    )
    assert ps.pending_followups(user.id) == [f"silence_check:{TODAY}"]
    assert ps.cancel_followups(user.id) == 1 and ps.pending_followups(user.id) == []
    assert ps.cancel_followups(999) == 0


async def test_engine_schedules_followup_through_the_scheduler(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler], user: User, clock: FakeClock
) -> None:
    eng, ps = proactive
    clock.set(datetime(2026, 9, 3, 7, 5, tzinfo=zone("UTC")))  # 11:05 Dubai
    out = await eng.fire(user.id, "no_first_meal")
    assert out.sent and ps.pending_followups(user.id) == [f"no_first_meal:{TODAY}"]
    job = ps.scheduler.get_job(sched.followup_job_id(user.id, f"no_first_meal:{TODAY}"))
    assert job.trigger.run_date == clock.now() + timedelta(minutes=45)


async def test_global_jobs_reminder_check_and_nightly(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler],
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    messenger: FakeMessenger,
) -> None:
    _, ps = proactive
    ids = {j.id for j in ps.scheduler.get_jobs()}
    assert {sched.GLOBAL_SYNC_ID, sched.GLOBAL_REMINDERS_ID} <= ids
    sync_job = ps.scheduler.get_job(sched.GLOBAL_SYNC_ID)
    assert sync_job.trigger.interval == timedelta(minutes=30)
    assert ps.scheduler.get_job(sched.GLOBAL_REMINDERS_ID).trigger.interval == timedelta(minutes=1)

    due = await repo.add_reminder(
        session, user.id, due_at=clock.now() - timedelta(minutes=2), text="waist", now=clock.now()
    )
    await repo.add_reminder(
        session, user.id, due_at=clock.now() + timedelta(hours=2), text="later", now=clock.now()
    )
    await session.commit()
    assert await ps._run_reminder_checks() == 1
    assert messenger.texts(user.chat_id) == ["reminder_due step 1: "]
    await session.refresh(due)
    assert due.status == ReminderStatus.sent
    assert await ps._run_reminder_checks() == 0

    await ps._run_nightly_summary(user.id)
    await ps._run_nightly_summary(999)
    await ps._run_integration_sync()
    assert ps.nightly_calls == [(user.id, TODAY - timedelta(days=1)), (0, TODAY)]  # type: ignore[attr-defined]


async def test_a_reminder_that_cannot_be_sent_is_retired_not_retried(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler],
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    messenger: FakeMessenger,
) -> None:
    """The minute job picks up every pending reminder whose time has come. When the attempt ends
    in anything but a message, leaving it pending fires it again in sixty seconds, and again
    after that, for as long as the process runs."""
    _, ps = proactive
    user.status = UserStatus.paused  # the engine skips this one with reason user_not_active
    due = await repo.add_reminder(
        session, user.id, due_at=clock.now() - timedelta(minutes=2), text="waist", now=clock.now()
    )
    await session.commit()

    assert await ps._run_reminder_checks() == 1
    assert messenger.texts(user.chat_id) == []
    session.expire_all()
    await session.refresh(due)
    assert due.status == ReminderStatus.missed
    assert await ps._run_reminder_checks() == 0


async def test_a_reminder_whose_send_failed_is_retried_then_written_off(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler],
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Telegram hiccup on the one send is not the user changing their mind. It is retried for
    half an hour, and only then written off, because a permanent error would loop forever."""
    eng, ps = proactive

    async def failing(*args: object, **kwargs: object) -> sched_engine.FireOutcome:
        return sched_engine.FireOutcome(name="reminder_due", status="error", reason="send_failed")

    monkeypatch.setattr(eng, "fire", failing)
    due = await repo.add_reminder(
        session, user.id, due_at=clock.now() - timedelta(minutes=2), text="waist", now=clock.now()
    )
    await session.commit()

    assert await ps._run_reminder_checks() == 1
    session.expire_all()
    await session.refresh(due)
    assert due.status == ReminderStatus.pending, "still worth another minute"

    clock.set(clock.now() + timedelta(minutes=40))
    assert await ps._run_reminder_checks() == 1
    session.expire_all()
    await session.refresh(due)
    assert due.status == ReminderStatus.missed, "and eventually it is not"


async def test_reschedule_all_active_users(
    proactive: tuple[ProactiveEngine, sched.ProactiveScheduler], user: User
) -> None:
    _, ps = proactive
    assert await ps.reschedule_all() == 1
    assert len(ps.user_job_ids(user.id)) == len(EXPECTED_TRIGGERS)


async def test_start_is_idempotent_and_resume(
    engine: AsyncEngine, clock: FakeClock, settings: Settings, messenger: FakeMessenger
) -> None:
    eng = ProactiveEngine(
        make_session_factory(engine),
        FakeDecider(),
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
    )
    ps = sched.ProactiveScheduler(eng, make_session_factory(engine), clock)
    ps.start(paused=True)
    ps.start(paused=True)
    assert ps.scheduler.running and len(ps.scheduler.get_jobs()) == 2
    ps.resume()
    ps.shutdown()
    ps.shutdown()  # no-op while stopping
    await asyncio.sleep(0)  # APScheduler flips the state on the next loop tick
    assert not ps.scheduler.running
    ps.shutdown()  # no-op when stopped


def test_checkin_times_move_the_silence_slots() -> None:
    """Brief §4 step 9 / §5.6: the check-in times the user gives during onboarding are the
    deadlines of the meal-silence triggers, not a stored-and-ignored preference."""
    specs = {
        s.trigger: s
        for s in sched.build_job_specs(make_profile(checkin_times=["13:00", "20:00", "23:30"]))
    }
    assert specs["no_lunch"].at == "13:00"
    assert specs["no_dinner"].at == "20:00"
    assert specs["day_not_closed"].at == "23:30"
    assert specs["no_first_meal"].at == "11:00"  # untouched: no morning check-in given
    assert set(specs) == EXPECTED_TRIGGERS
    morning = {s.trigger: s for s in sched.build_job_specs(make_profile(checkin_times=["09:30"]))}
    assert morning["no_first_meal"].at == "09:30" and morning["no_lunch"].at == "15:00"
    # garbage and duplicates: earliest valid time per window wins, the rest is ignored
    messy = {
        s.trigger: s
        for s in sched.build_job_specs(make_profile(checkin_times=["14:30", "12:15", "noon", ""]))
    }
    assert messy["no_lunch"].at == "12:15"
    assert sched.build_job_specs(make_profile(checkin_times=None)) == sched.build_job_specs(
        make_profile()
    )
