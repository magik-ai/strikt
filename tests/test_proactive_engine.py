"""``ProactiveEngine.fire`` end to end with a FakeDecider, seeded rows and the event bus."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.engine import make_session_factory
from strikt.db.models import CoachingIntensity, Profile, ReminderStatus, User, UserStatus
from strikt.events import (
    DayStateChanged,
    EventBus,
    MeasurementEvent,
    RecoveryEvent,
    SleepEvent,
    UserReplied,
    WorkoutEvent,
)
from strikt.proactive.engine import ProactiveEngine, event_payload
from strikt.proactive.types import TriggerName
from strikt.telegram.messenger import FakeMessenger
from tests.test_proactive_helpers import (
    TODAY,
    BrokenStateProvider,
    DbStateProvider,
    FakeDecider,
    at_local,
    make_sender,
    seed_day_flag,
    seed_meal,
    seed_measurement,
    seed_sleep,
    seed_workout,
)


class FakePlanner:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, str, str, datetime]] = []
        self.cancelled: list[tuple[int, list[str] | None]] = []

    def schedule_followup(
        self, user_id: int, parent: TriggerName, window_key: str, at: datetime
    ) -> str:
        self.scheduled.append((user_id, parent, window_key, at))
        return f"user:{user_id}:followup:{window_key}"

    def cancel_followups(self, user_id: int, *, window_prefixes: Any = None) -> int:
        self.cancelled.append((user_id, list(window_prefixes) if window_prefixes else None))
        return 1


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def decider() -> FakeDecider:
    return FakeDecider()


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
async def engine_(
    engine: AsyncEngine,
    user: User,
    clock: FakeClock,
    settings: Settings,
    messenger: FakeMessenger,
    bus: EventBus,
    decider: FakeDecider,
    planner: FakePlanner,
) -> AsyncIterator[ProactiveEngine]:
    eng = ProactiveEngine(
        make_session_factory(engine),
        decider,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
        bus,
        followups=planner,
    )
    yield eng
    eng.close()


def _at(hhmm: str) -> datetime:
    return at_local(TODAY, hhmm)


async def test_no_first_meal_sends_records_and_schedules_followup(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    messenger: FakeMessenger,
    decider: FakeDecider,
    planner: FakePlanner,
    session: AsyncSession,
) -> None:
    clock.set(_at("11:05"))
    outcome = await engine_.fire(user.id, "no_first_meal")
    assert outcome.sent and outcome.step == 1 and outcome.window_key == f"no_first_meal:{TODAY}"
    assert messenger.texts(user.chat_id) == ["no_first_meal step 1: hours_since_wake=3.1"]
    assert outcome.message_id == messenger.sent[-1].message_id
    row = await repo.last_send_for_window(session, user.id, outcome.window_key or "")
    assert (
        row is not None
        and row.trigger == "no_first_meal"
        and row.step == 1
        and row.telegram_message_id == outcome.message_id
    )
    assert planner.scheduled == [
        (user.id, "no_first_meal", f"no_first_meal:{TODAY}", clock.now() + timedelta(minutes=45))
    ]
    call = decider.last
    assert call.ladder.step == 1 and call.state is not None and call.state.meals == []
    assert call.fire.facts["wake_time"] == "08:00"


async def test_window_is_idempotent_then_escalates_to_four(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    messenger: FakeMessenger,
    planner: FakePlanner,
) -> None:
    clock.set(_at("11:05"))
    assert (await engine_.fire(user.id, "no_first_meal")).sent
    clock.advance(timedelta(minutes=3))
    dup = await engine_.fire(user.id, "no_first_meal")
    assert dup.status == "skipped" and dup.reason == "duplicate_in_window"
    payload = {"parent": "no_first_meal", "window_key": f"no_first_meal:{TODAY}"}
    for expected in (2, 3, 4):
        clock.advance(timedelta(minutes=45))
        out = await engine_.fire(user.id, "escalation_followup", payload)
        assert out.sent and out.step == expected and out.name == "no_first_meal"
    clock.advance(timedelta(minutes=45))
    fifth = await engine_.fire(user.id, "escalation_followup", payload)
    assert fifth.status == "skipped" and fifth.reason == "ladder_exhausted"
    assert [m.text for m in messenger.sent] == [
        f"no_first_meal step {n}: hours_since_wake={h}"
        for n, h in ((1, 3.1), (2, 3.9), (3, 4.6), (4, 5.4))
    ]
    # follow-ups were scheduled after steps 1-3 only (never beyond step 4)
    assert len(planner.scheduled) == 3


async def test_reply_resets_the_window_and_cancels_followups(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    bus: EventBus,
    planner: FakePlanner,
    session: AsyncSession,
) -> None:
    clock.set(_at("11:05"))
    assert (await engine_.fire(user.id, "no_first_meal")).sent
    clock.advance(timedelta(minutes=10))
    await bus.publish(UserReplied(user_id=user.id, occurred_at=clock.now(), turn_id=42))
    row = await repo.last_send_for_window(session, user.id, f"no_first_meal:{TODAY}")
    assert row is not None
    session.expire(row)
    row = await repo.last_send_for_window(session, user.id, f"no_first_meal:{TODAY}")
    assert row is not None and row.responded_at is not None and row.response_turn_id == 42
    assert planner.cancelled == [(user.id, None)]
    clock.advance(timedelta(minutes=45))
    again = await engine_.fire(
        user.id,
        "escalation_followup",
        {"parent": "no_first_meal", "window_key": f"no_first_meal:{TODAY}"},
    )
    assert again.status == "skipped" and again.reason == "window_answered"


async def test_daily_cap_and_quiet_hours(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    profile: Profile,
    messenger: FakeMessenger,
) -> None:
    clock.set(_at("11:05"))
    for n in range(5):
        await repo.add_proactive_send(
            session,
            user.id,
            trigger="x",
            window_key=f"x:{n}",
            step=1,
            sent_at=clock.now() - timedelta(minutes=n + 1),
            text="x",
        )
    await session.commit()
    capped = await engine_.fire(user.id, "no_first_meal")
    assert capped.status == "skipped" and capped.reason == "daily_cap"
    profile.coaching_intensity = CoachingIntensity.drill_sergeant
    await session.commit()
    assert (await engine_.fire(user.id, "no_first_meal")).sent  # cap 8 in drill-sergeant mode

    # 00:00 Dubai on the 4th: quiet hours, but the bedtime message is exempt
    clock.set(datetime(2026, 9, 3, 20, 0, tzinfo=UTC))
    bedtime = await engine_.fire(user.id, "bedtime_minus_30")
    assert bedtime.sent and bedtime.window_key == f"bedtime_minus_30:{TODAY}"
    # 01:00 on the 4th: a 30-hour silence would fire, quiet hours hold it back
    await repo.add_turn(
        session,
        user.id,
        role="user",
        content=[{"type": "text", "text": "hi"}],
        now=clock.now() - timedelta(hours=30),
    )
    await session.commit()
    clock.set(datetime(2026, 9, 3, 21, 0, tzinfo=UTC))
    silent = await engine_.fire(user.id, "silence_check")
    assert silent.status == "skipped" and silent.reason == "quiet_hours"
    # 08:00 on the 4th: quiet hours are over, the same trigger goes out
    clock.set(datetime(2026, 9, 4, 4, 0, tzinfo=UTC))
    assert (await engine_.fire(user.id, "silence_check")).sent
    assert len(messenger.sent) == 3


async def test_decider_silent_error_and_broken_state(
    engine: AsyncEngine,
    user: User,
    clock: FakeClock,
    settings: Settings,
    messenger: FakeMessenger,
    session: AsyncSession,
) -> None:
    clock.set(_at("11:05"))
    silent = FakeDecider(send=False)
    eng = ProactiveEngine(
        make_session_factory(engine),
        silent,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
    )
    out = await eng.fire(user.id, "no_first_meal")
    assert out.status == "silent" and messenger.sent == []
    assert (
        await repo.count_sends_today(session, user.id, since=clock.now() - timedelta(days=1)) == 0
    )

    broken = FakeDecider(raise_error=True)
    eng2 = ProactiveEngine(
        make_session_factory(engine),
        broken,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
    )
    assert (await eng2.fire(user.id, "no_first_meal")).reason == "decider_failed"

    eng3 = ProactiveEngine(
        make_session_factory(engine),
        FakeDecider(),
        BrokenStateProvider(),
        make_sender(messenger),
        clock,
        settings,
    )
    assert (await eng3.fire(user.id, "no_first_meal")).reason == "precondition_false"
    # the class C triggers do not need the state
    await seed_day_flag(session, user.id, TODAY - timedelta(days=1), "travel", clock.now())
    await session.commit()
    clock.set(_at("08:20"))
    assert (await eng3.fire(user.id, "post_travel_reentry")).sent

    async def failing_sender(u: User, text: str) -> int | None:
        raise RuntimeError("telegram down")

    eng4 = ProactiveEngine(
        make_session_factory(engine),
        FakeDecider(),
        DbStateProvider(),
        failing_sender,
        clock,
        settings,
    )
    clock.set(_at("11:05"))
    assert (await eng4.fire(user.id, "no_first_meal")).reason == "send_failed"
    assert (await eng4.fire(user.id, "not_a_trigger")).reason == "unknown_trigger"  # type: ignore[arg-type]


async def test_inactive_user_disabled_profile_and_missing_profile(
    engine_: ProactiveEngine, user: User, clock: FakeClock, session: AsyncSession, profile: Profile
) -> None:
    clock.set(_at("11:05"))
    profile.proactive_enabled = False
    await session.commit()
    assert (await engine_.fire(user.id, "no_first_meal")).reason == "proactive_disabled"
    await repo.set_user_status(session, user.id, UserStatus.paused)
    await session.commit()
    assert (await engine_.fire(user.id, "no_first_meal")).reason == "user_not_active"
    assert (await engine_.fire(999, "no_first_meal")).reason == "user_not_active"


async def test_reminder_due_bypasses_settings_and_marks_sent(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    profile: Profile,
    messenger: FakeMessenger,
) -> None:
    profile.proactive_enabled = False
    reminder = await repo.add_reminder(
        session,
        user.id,
        due_at=clock.now() - timedelta(minutes=1),
        text="waist, fasted",
        now=clock.now(),
    )
    await session.commit()
    clock.set(datetime(2026, 9, 3, 2, 0, tzinfo=UTC))  # 06:00 Dubai, quiet hours
    out = await engine_.fire(
        user.id,
        "reminder_due",
        {"reminder_id": reminder.id, "text": reminder.text, "due_at": reminder.due_at.isoformat()},
    )
    assert out.sent and out.window_key == f"reminder_due:{reminder.id}"
    reminder_id = reminder.id
    await session.refresh(reminder)
    assert reminder.status == ReminderStatus.sent
    assert messenger.texts(user.chat_id) == ["reminder_due step 1: "]
    # a second fire for the same reminder is a no-op (window answered or duplicate)
    assert not (
        await engine_.fire(user.id, "reminder_due", {"reminder_id": reminder_id, "text": "waist"})
    ).sent


async def test_intensity_restored_clears_the_temporary_level(
    engine_: ProactiveEngine, user: User, clock: FakeClock, session: AsyncSession, profile: Profile
) -> None:
    clock.set(_at("09:00"))
    profile.temp_intensity = CoachingIntensity.gentle
    profile.temp_intensity_until = clock.now() - timedelta(hours=1)
    await session.commit()
    out = await engine_.fire(user.id, "intensity_restored")
    assert out.sent
    fresh = await repo.get_profile(session, user.id)
    await session.refresh(fresh)
    assert fresh is not None
    assert fresh.temp_intensity is None
    assert fresh.temp_intensity_until is None


async def test_cooldown_suppresses_pressure_but_not_missed_meals(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    decider: FakeDecider,
) -> None:
    for n in (3, 2, 1):
        for hhmm in ("09:00", "13:00", "20:00"):
            await seed_meal(session, user.id, TODAY - timedelta(days=n), hhmm, kcal=600, protein=60)
    await seed_meal(session, user.id, TODAY, "09:00", kcal=400, protein=30)
    await session.commit()
    clock.set(_at("18:00"))
    out = await engine_.fire(user.id, "protein_check")
    assert out.status == "skipped" and out.reason == "clean_streak_cooldown"
    clock.set(_at("15:00"))
    lunch = await engine_.fire(user.id, "no_lunch")
    assert lunch.sent and decider.last.ladder.clean_streak_days == 3
    clock.set(_at("09:00"))
    assert (await engine_.fire(user.id, "clean_streak")).sent


async def test_workout_event_feeds_the_analysis_trigger(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    bus: EventBus,
    decider: FakeDecider,
    messenger: FakeMessenger,
) -> None:
    await seed_workout(
        session,
        user.id,
        TODAY - timedelta(days=7),
        "18:00",
        duration_min=45,
        kcal=406,
        avg_hr=130,
        external_id="prev",
    )
    await session.commit()
    clock.set(_at("11:40"))
    started = _at("10:00")
    event = WorkoutEvent(
        user_id=user.id,
        occurred_at=clock.now(),
        source="whoop",
        external_id="w-42",
        sport="weightlifting",
        started_at=started,
        ended_at=started + timedelta(minutes=94),
        duration_min=94,
        kcal=361,
        avg_hr=104,
        zones_min={"z0": 54.5, "z1": 39.5},
    )
    assert event_payload(event)["started_at"] == started.isoformat()
    await bus.publish(event)
    assert len(messenger.sent) == 1 and decider.last.fire.name == "whoop_workout_synced"
    facts = decider.last.fire.facts
    assert (
        facts["previous_same_sport"]["kcal"] == 406 and facts["deltas_vs_previous"]["avg_hr"] == -26
    )
    assert facts["avg_30d"]["sessions"] == 1 and facts["this"]["zone0_pct"] == 58
    # the same webhook delivered twice: one message
    await bus.publish(event)
    assert len(messenger.sent) == 1
    # a workout logged through the chat is not re-analysed
    manual = WorkoutEvent(
        user_id=user.id, occurred_at=clock.now(), source="manual", sport="run", started_at=started
    )
    await bus.publish(manual)
    assert len(messenger.sent) == 1
    # a backfilled workout from last month is stored but not announced
    stale = WorkoutEvent(
        user_id=user.id,
        occurred_at=clock.now(),
        source="whoop",
        external_id="old",
        sport="weightlifting",
        started_at=started - timedelta(days=20),
    )
    await bus.publish(stale)
    assert len(messenger.sent) == 1


async def test_recovery_sleep_and_scale_events(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    bus: EventBus,
    decider: FakeDecider,
    messenger: FakeMessenger,
) -> None:
    clock.set(_at("08:30"))
    await bus.publish(
        RecoveryEvent(
            user_id=user.id,
            occurred_at=clock.now(),
            source="whoop",
            date=TODAY,
            score=21,
            rhr=58,
            hrv_ms=31,
        )
    )
    assert decider.last.fire.name == "whoop_recovery_low" and decider.last.fire.facts["score"] == 21

    for n in (2, 1):
        await seed_sleep(
            session, user.id, TODAY - timedelta(days=n), onset="01:20", woke="07:30", asleep_min=350
        )
    await seed_sleep(session, user.id, TODAY, onset="01:15", woke="08:50", asleep_min=360)
    await session.commit()
    await bus.publish(
        SleepEvent(
            user_id=user.id,
            occurred_at=clock.now(),
            source="whoop",
            started_at=at_local(TODAY, "01:15"),
            ended_at=at_local(TODAY, "08:50"),
            asleep_min=360,
        )
    )
    names = [c.fire.name for c in decider.calls]
    assert names[-3:] == ["wake_check", "sleep_onset_late", "sleep_debt_accumulating"]
    assert decider.calls[-1].fire.facts["total_deficit_min"] == 200

    for n in (3, 2, 1):
        await seed_measurement(
            session, user.id, "weight", 104 + n * 0.1, clock.now() - timedelta(days=n)
        )
    await session.commit()
    await bus.publish(
        MeasurementEvent(
            user_id=user.id,
            occurred_at=clock.now(),
            source="withings",
            type="weight",
            value=105.0,
            unit="kg",
            measured_at=clock.now(),
        )
    )
    weight = decider.last.fire
    assert weight.name == "scale_weight_received" and weight.facts["readings_7d"] == 3
    sent_before = len(messenger.sent)
    await bus.publish(
        MeasurementEvent(
            user_id=user.id,
            occurred_at=clock.now(),
            source="manual",
            type="weight",
            value=105.0,
            unit="kg",
            measured_at=clock.now(),
        )
    )
    await bus.publish(
        MeasurementEvent(
            user_id=user.id,
            occurred_at=clock.now(),
            source="withings",
            type="steps",
            value=8000,
            unit="count",
            measured_at=clock.now(),
        )
    )
    assert len(messenger.sent) == sent_before


async def test_day_state_changed_cancels_meal_window_followups(
    engine_: ProactiveEngine, user: User, clock: FakeClock, bus: EventBus, planner: FakePlanner
) -> None:
    await bus.publish(
        DayStateChanged(user_id=user.id, occurred_at=clock.now(), date=TODAY, reason="log_meal")
    )
    assert planner.cancelled == [
        (
            user.id,
            [
                f"{n}:{TODAY}"
                for n in (
                    "no_first_meal",
                    "no_lunch",
                    "no_dinner",
                    "day_not_closed",
                    "protein_check",
                    "fiber_check",
                )
            ],
        )
    ]


async def test_fire_is_serialised_per_user(
    engine_: ProactiveEngine, user: User, clock: FakeClock, messenger: FakeMessenger
) -> None:
    import asyncio

    clock.set(_at("11:05"))
    outcomes = await asyncio.gather(
        engine_.fire(user.id, "no_first_meal"), engine_.fire(user.id, "no_first_meal")
    )
    assert sorted(o.status for o in outcomes) == ["sent", "skipped"] and len(messenger.sent) == 1
    assert engine_.outcomes[-2:] == list(outcomes) or set(engine_.outcomes[-2:]) == set(outcomes)
    assert engine_.followup_delay() == timedelta(minutes=45)


async def test_bedtime_after_midnight_is_decided_on_the_evening_day_state(
    engine_: ProactiveEngine,
    user: User,
    clock: FakeClock,
    decider: FakeDecider,
    session: AsyncSession,
) -> None:
    """Bed 00:30 → bedtime_minus_30 fires at 00:00 on the next calendar date; the decider must
    see the evening's (closed) day, not the empty new one."""
    await seed_meal(session, user.id, TODAY, "19:00", kcal=700)
    await repo.close_day(session, user.id, TODAY, verdict="ok", now=clock.now())
    await session.commit()
    clock.set(datetime(2026, 9, 3, 20, 0, tzinfo=UTC))  # 00:00 Dubai on the 4th
    outcome = await engine_.fire(user.id, "bedtime_minus_30")
    assert outcome.sent and outcome.window_key == f"bedtime_minus_30:{TODAY}"
    call = decider.last
    assert call.fire.day == TODAY
    assert call.state is not None and call.state.date == TODAY and call.state.closed
    assert call.state.totals.macros.kcal == 700
    assert call.fire.facts["day_closed"] is True


async def test_user_reminders_do_not_consume_the_proactive_cap(
    engine_: ProactiveEngine, user: User, clock: FakeClock, session: AsyncSession
) -> None:
    clock.set(_at("11:05"))
    for n in range(5):
        await repo.add_proactive_send(
            session,
            user.id,
            trigger="reminder_due",
            window_key=f"reminder_due:{n}",
            step=1,
            sent_at=clock.now() - timedelta(minutes=n + 1),
            text="waist",
        )
    await session.commit()
    outcome = await engine_.fire(user.id, "no_first_meal")
    assert outcome.sent, outcome
