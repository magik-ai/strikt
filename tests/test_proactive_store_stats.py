"""``proactive.store`` history queries and ``proactive.stats`` streaks against seeded rows."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.models import Profile, Protocol, TurnRole, User
from strikt.proactive import stats, store
from tests.test_proactive_helpers import (
    TODAY,
    TZ,
    at_local,
    seed_day_flag,
    seed_meal,
    seed_measurement,
    seed_sleep,
    seed_workout,
)


async def test_day_facts_lunch_window_and_evening(session: AsyncSession, user: User) -> None:
    d = TODAY - timedelta(days=1)
    await seed_meal(session, user.id, d, "09:00", kcal=400, protein=30)
    await seed_meal(session, user.id, d, "19:30", kcal=1200, protein=60)
    await seed_meal(session, user.id, d, "22:10", kcal=500, protein=20)
    await session.commit()
    [day] = await store.day_facts(session, user.id, date_from=d, date_to=d, tz=TZ)
    assert day.meals == 3 and day.kcal == 2100 and day.had_lunch is False
    assert day.evening_kcal == 1700 and day.first_meal_at is not None
    assert day.first_meal_at.strftime("%H:%M") == "09:00"
    assert day.as_facts()["weekday"] == "Wed"


async def test_skipped_lunch_stats_and_weekend_blowups(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    # two skipped-lunch days with big evenings, two normal days, one weekend blowup with alcohol
    for offset, lunch, evening in (
        (10, False, 1600),
        (8, False, 1500),
        (6, True, 700),
        (4, True, 650),
    ):
        d = TODAY - timedelta(days=offset)
        await seed_meal(session, user.id, d, "09:00", kcal=400)
        if lunch:
            await seed_meal(session, user.id, d, "13:00", kcal=600)
        await seed_meal(session, user.id, d, "20:00", kcal=evening)
    saturday = date(2026, 8, 22)
    await seed_meal(session, user.id, saturday, "16:30", kcal=900)
    await seed_meal(session, user.id, saturday, "21:00", kcal=1800)
    await seed_day_flag(session, user.id, saturday, "alcohol", clock.now())
    await session.commit()

    days = await store.day_facts(
        session,
        user.id,
        date_from=TODAY - timedelta(days=30),
        date_to=TODAY - timedelta(days=1),
        tz=TZ,
    )
    st = store.skipped_lunch_stats(days)
    assert st.skipped_days == 3 and st.with_lunch_days == 2
    assert st.avg_evening_kcal_skipped == pytest.approx(1633.3, abs=0.1)
    assert st.avg_evening_kcal_with_lunch == 675 and st.avg_day_kcal_with_lunch == 1675
    assert st.examples[0].evening_kcal == 1800  # the weekend day tops the list
    blowups = store.weekend_blowups(days, 2000)
    assert [b.date for b in blowups] == [saturday] and "alcohol" in blowups[0].flags
    assert store.is_off_day(blowups[0], 2000) is True


def test_same_meal_streak_pure() -> None:
    days = [
        (TODAY - timedelta(days=n), {"chicken breast", "rice"} if n < 5 else {"salmon"})
        for n in range(7)
    ]
    streak = store.same_meal_streak(days)
    assert streak is not None and streak.name == "chicken breast" and streak.days == 5
    assert store.same_meal_streak(days, min_days=6) is None
    broken = [
        (TODAY - timedelta(days=n), {"chicken breast"} if n != 2 else {"beef"}) for n in range(7)
    ]
    assert store.same_meal_streak(broken) is None
    assert store.same_meal_streak(days[:2]) is None


async def test_per_day_item_names_normalises(session: AsyncSession, user: User) -> None:
    for n in range(3):
        await seed_meal(
            session, user.id, TODAY - timedelta(days=n), "13:00", name="Chicken Breast, grilled!"
        )
    await session.commit()
    rows = await store.per_day_item_names(
        session, user.id, date_from=TODAY - timedelta(days=5), date_to=TODAY
    )
    assert len(rows) == 3 and rows[-1][1] == {"chicken breast grilled"}


async def test_sleep_nights_and_target(session: AsyncSession, user: User, profile: Profile) -> None:
    await seed_sleep(session, user.id, TODAY, onset="01:10", woke="07:40", asleep_min=360)
    await seed_sleep(
        session, user.id, TODAY - timedelta(days=1), onset="00:20", woke="08:00", asleep_min=430
    )
    await session.commit()
    nights = await store.sleep_nights(session, user.id, tz=TZ, n=3)
    assert [n.night_of for n in nights] == [TODAY, TODAY - timedelta(days=1)]
    assert nights[0].as_facts()["onset"] == "01:10" and nights[0].as_facts()["woke"] == "07:40"
    assert store.sleep_target_minutes(profile) == 420  # 00:30 → 08:00 = 450 − 30
    profile.bed_time = None
    assert store.sleep_target_minutes(profile) == 420
    profile.bed_time = time(2, 0)
    assert store.sleep_target_minutes(profile) == 360  # floor


async def test_workout_comparison_last_same_sport_and_avg(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await seed_workout(
        session, user.id, TODAY - timedelta(days=10), "18:00", duration_min=45, kcal=406, avg_hr=130
    )
    await seed_workout(
        session,
        user.id,
        TODAY - timedelta(days=5),
        "18:00",
        sport="running",
        duration_min=30,
        kcal=300,
        avg_hr=150,
    )
    this_id = await seed_workout(
        session,
        user.id,
        TODAY,
        "10:00",
        duration_min=94,
        kcal=361,
        avg_hr=104,
        zones_min={"z0": 54, "z1": 40},
    )
    await session.commit()
    cmp_ = await store.workout_comparison(
        session,
        user.id,
        sport="Weightlifting",
        started_at=at_local(TODAY, "10:00"),
        now=clock.now(),
        tz=TZ,
        exclude_id=this_id,
    )
    assert (
        cmp_.previous is not None
        and cmp_.previous.kcal == 406
        and cmp_.previous.kcal_per_min is not None
    )
    assert cmp_.avg_count == 2 and cmp_.avg_kcal == (406 + 361) / 2
    last = await store.last_workout(session, user.id)
    assert last is not None and last.id == this_id
    assert store.zone_zero_share({"z0": 54, "z1": 40}) == 100 * 54 / 94
    assert store.zone_zero_share(None) is None and store.density(361, 94) is not None
    assert store.allowed_workout_gap_days(None) == 4


async def test_measurement_statuses_overdue_at_16_days(
    session: AsyncSession, user: User, profile: Profile, clock: FakeClock
) -> None:
    await seed_measurement(
        session, user.id, "waist", 103, clock.now() - timedelta(days=16), unit="cm"
    )
    await seed_measurement(session, user.id, "weight", 104.5, clock.now() - timedelta(days=2))
    await session.commit()
    statuses = await store.measurement_statuses(session, user.id, profile, now=clock.now())
    waist, weight = statuses
    assert (
        waist.type == "waist"
        and waist.days_since == 16
        and waist.days_overdue == 2
        and waist.last_value == 103
    )
    assert weight.days_overdue == 0 and weight.as_facts()["never_measured"] is False


async def test_measurement_never_taken_counts_from_onboarding(
    session: AsyncSession, user: User, profile: Profile, clock: FakeClock
) -> None:
    profile.onboarding_done_at = clock.now() - timedelta(days=20)
    statuses = await store.measurement_statuses(session, user.id, profile, now=clock.now())
    assert statuses[0].days_since is None and statuses[0].days_overdue == 6
    assert statuses[1].days_overdue == 13


async def test_recent_weights_and_last_user_message(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    for n in (5, 3, 1):
        await seed_measurement(
            session, user.id, "weight", 104 + n * 0.1, clock.now() - timedelta(days=n)
        )
    await seed_measurement(session, user.id, "weight", 110, clock.now() - timedelta(days=20))
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=[{"type": "text", "text": "hi"}],
        now=clock.now() - timedelta(hours=2),
    )
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.assistant,
        content=[{"type": "text", "text": "ok"}],
        now=clock.now() - timedelta(hours=1),
    )
    await session.commit()
    weights = await store.recent_weights(session, user.id, now=clock.now(), tz=TZ)
    assert [round(w, 1) for _, w in weights] == [104.5, 104.3, 104.1]
    assert await store.last_user_message_at(session, user.id) == clock.now() - timedelta(hours=2)


async def test_streaks_and_clean_days(session: AsyncSession, user: User, clock: FakeClock) -> None:
    # 4 days ago over target; last three days clean with three meals; today open (ignored)
    await seed_meal(session, user.id, TODAY - timedelta(days=4), "13:00", kcal=2600)
    for n in (3, 2, 1):
        d = TODAY - timedelta(days=n)
        for hhmm in ("09:00", "13:00", "20:00"):
            await seed_meal(session, user.id, d, hhmm, kcal=600)
        await seed_sleep(
            session, user.id, d + timedelta(days=1), onset="00:40", woke="08:00", asleep_min=420
        )
    await seed_meal(session, user.id, TODAY, "09:00", kcal=2500)
    await session.commit()
    s = await stats.compute_streaks(
        session, user.id, today=TODAY, tz=TZ, kcal_target=2000, bed_time=time(0, 30)
    )
    assert s.clean_days == 3 and s.three_meal_days == 3 and s.bedtime_hits == 3
    assert s.clean_streak_start == TODAY - timedelta(days=3)
    # closing today at 2500 breaks the streak; the closed day counts
    await repo.close_day(session, user.id, TODAY, verdict="over", now=clock.now())
    await session.commit()
    s2 = await stats.compute_streaks(
        session, user.id, today=TODAY, tz=TZ, kcal_target=2000, bed_time=None
    )
    assert s2.clean_days == 0 and s2.bedtime_hits == 0


async def test_week_scorecard(
    session: AsyncSession, user: User, protocol: Protocol, clock: FakeClock
) -> None:
    monday = date(2026, 8, 31)
    for n in range(3):
        d = monday + timedelta(days=n)
        await seed_meal(session, user.id, d, "09:00", kcal=600, protein=60, fiber=8)
        await seed_meal(session, user.id, d, "19:00", kcal=1200, protein=120, fiber=12)
    await repo.close_day(session, user.id, monday, verdict="ok", now=clock.now())
    await seed_workout(session, user.id, monday + timedelta(days=1), "18:00")
    await seed_sleep(
        session, user.id, monday + timedelta(days=1), onset="00:10", woke="08:00", asleep_min=430
    )
    await seed_sleep(
        session, user.id, monday + timedelta(days=2), onset="01:30", woke="08:00", asleep_min=380
    )
    await seed_measurement(session, user.id, "weight", 104, at_local(monday, "07:30"))
    await session.commit()
    card = await stats.week_scorecard(
        session,
        user.id,
        week_start=monday,
        tz=TZ,
        targets=repo.protocol_targets(protocol),
        bed_time=time(0, 30),
        through=TODAY,
    )
    assert card.days_logged == 3 and card.days_closed == 1 and card.days_within_target == 3
    assert card.avg_kcal == 1800 and card.avg_protein_g == 180 and card.avg_fiber_g == 20
    assert (
        card.sessions == 1
        and card.bedtime_hits == 1
        and card.nights_tracked == 2
        and card.measurements_taken == 1
    )
    assert card.as_facts()["week_start"] == "2026-08-31"


async def test_response_rate_window(session: AsyncSession, user: User, clock: FakeClock) -> None:
    old = clock.now() - timedelta(days=40)
    await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_lunch",
        window_key="no_lunch:old",
        step=1,
        sent_at=old,
        text="x",
    )
    for n, answered in ((3, True), (2, False), (1, True)):
        row = await repo.add_proactive_send(
            session,
            user.id,
            trigger="no_lunch",
            window_key=f"no_lunch:{n}",
            step=1,
            sent_at=clock.now() - timedelta(days=n),
            text="x",
        )
        if answered:
            row.responded_at = row.sent_at + timedelta(minutes=5)
    await session.commit()
    assert await stats.response_rate(session, user, "no_lunch", now=clock.now()) == 2 / 3
    assert await stats.response_rate(session, user, "protein_check", now=clock.now()) is None
    assert (
        stats.minutes_after_noon(time(0, 30)) == 750
        and stats.minutes_after_noon(time(23, 30)) == 690
    )
    onset = datetime(2026, 9, 3, 1, 15, tzinfo=UTC)
    assert (
        stats.bedtime_hit(onset, time(0, 30)) is False
        and stats.bedtime_hit(onset, time(1, 0)) is True
    )
    assert stats.bedtime_hit(onset, None) is None


async def test_load_history_bundle(
    session: AsyncSession, user: User, profile: Profile, protocol: Protocol, clock: FakeClock
) -> None:
    await seed_meal(session, user.id, TODAY - timedelta(days=1), "09:00", kcal=500)
    await seed_sleep(session, user.id, TODAY, onset="00:40", woke="08:00", asleep_min=400)
    await seed_workout(session, user.id, TODAY - timedelta(days=2), "18:00")
    await repo.add_reminder(
        session, user.id, due_at=clock.now() - timedelta(minutes=1), text="waist", now=clock.now()
    )
    await repo.add_note(
        session,
        user.id,
        kind="event",
        text="Dinner at Kinoya",
        confidence=0.9,
        now=clock.now(),
        expires_at=clock.now() + timedelta(hours=8),
    )
    await session.commit()
    local_now = at_local(TODAY, "12:00")
    facts = await store.load_history(
        session,
        user,
        profile,
        protocol,
        local_now=local_now,
        now=clock.now(),
        payload={"sport": "weightlifting", "started_at": clock.now().isoformat()},
    )
    assert (
        facts.recent_days[-1].date == TODAY - timedelta(days=1)
        and facts.recent_days[-1].kcal == 500
    )
    assert facts.sleep_nights[0].night_of == TODAY and facts.sleep_target_min == 420
    assert facts.last_workout is not None and facts.workout_comparison is not None
    assert facts.workout_comparison.previous is not None
    assert len(facts.pending_reminders) == 1 and facts.scorecard is not None
    assert facts.measurements[0].type == "waist"
    notes = await store.event_notes_for(session, user.id, day=TODAY, tz=TZ, now=clock.now())
    assert [n.text for n in notes] == ["Dinner at Kinoya"]
    assert store._parse_dt("not a date") is None and store._parse_dt(clock.now()) == clock.now()
