"""Ladder logic: steps 1→4, cap, quiet hours, reset on reply, duplicate window."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.config import Settings
from strikt.core.clock import FakeClock, zone
from strikt.db import repo
from strikt.db.models import CoachingIntensity, Profile, User
from strikt.proactive import ladder
from strikt.proactive.types import TriggerFire
from tests.test_proactive_helpers import TODAY, TZ

WINDOW = f"no_first_meal:{TODAY}"


def _fire(window: str = WINDOW) -> TriggerFire:
    return TriggerFire(
        name="no_first_meal",
        klass="time",
        window_key=window,
        local_now=datetime.combine(TODAY, time(12, 0), tzinfo=zone(TZ)),
        day=TODAY,
    )


async def _send(
    session: AsyncSession, user_id: int, step: int, at: datetime, window: str = WINDOW
) -> None:
    await repo.add_proactive_send(
        session,
        user_id,
        trigger="no_first_meal",
        window_key=window,
        step=step,
        sent_at=at,
        text=f"s{step}",
    )
    await session.commit()


async def test_ladder_steps_one_to_four_and_exhaustion(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings
) -> None:
    fresh = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
    assert (
        fresh.step == 1
        and fresh.sends_today == 0
        and fresh.cap_today == 5
        and fresh.intensity == "pushy"
    )
    assert (
        fresh.response_rate is None
        and fresh.clean_streak_days == 0
        and fresh.in_quiet_hours is False
    )

    for step in (1, 2, 3):
        await _send(session, user.id, step, clock.now())
        clock.advance(timedelta(minutes=45))
        state = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
        assert state.step == step + 1 and state.sends_today == step
    await _send(session, user.id, 4, clock.now())
    clock.advance(timedelta(minutes=45))
    window = await ladder.inspect_window(session, user.id, WINDOW, now=clock.now())
    assert window.exhausted is True and window.next_step == 4 and window.answered is False
    capped = await ladder.compute_ladder(
        session, user, _fire(), clock=clock, settings=settings, window=window
    )
    assert capped.step == 4 and capped.sends_today == 4


async def test_reset_on_reply_and_on_newer_user_message(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings
) -> None:
    await _send(session, user.id, 2, clock.now())
    clock.advance(timedelta(minutes=10))
    # a reply the bus reported
    assert await repo.mark_responded(session, user.id, at=clock.now(), turn_id=5) == 1
    await session.commit()
    window = await ladder.inspect_window(session, user.id, WINDOW, now=clock.now())
    assert window.answered is True and window.next_step == 1
    # a fresh window with an unanswered send but a user message newer than it: also reset
    await _send(session, user.id, 3, clock.now(), window="no_lunch:x")
    later = clock.now() + timedelta(minutes=1)
    w2 = await ladder.inspect_window(
        session, user.id, "no_lunch:x", now=later, last_user_message_at=later
    )
    assert w2.answered is True and w2.next_step == 1
    w3 = await ladder.inspect_window(
        session,
        user.id,
        "no_lunch:x",
        now=later,
        last_user_message_at=clock.now() - timedelta(hours=1),
    )
    assert w3.answered is False and w3.next_step == 4


async def test_too_soon_is_a_duplicate_not_an_escalation(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings
) -> None:
    await _send(session, user.id, 1, clock.now())
    clock.advance(timedelta(minutes=5))
    window = await ladder.inspect_window(session, user.id, WINDOW, now=clock.now())
    assert ladder.too_soon(window, settings) is True and window.next_step == 2
    clock.advance(timedelta(minutes=40))
    window = await ladder.inspect_window(session, user.id, WINDOW, now=clock.now())
    assert ladder.too_soon(window, settings) is False
    empty = await ladder.inspect_window(session, user.id, "other:window", now=clock.now())
    assert ladder.too_soon(empty, settings) is False
    assert ladder.followup_at(clock.now(), settings) == clock.now() + timedelta(minutes=45)


async def test_daily_cap_by_intensity(
    session: AsyncSession, user: User, profile: Profile, clock: FakeClock, settings: Settings
) -> None:
    assert ladder.daily_cap(settings, "pushy") == 5
    assert ladder.daily_cap(settings, "drill_sergeant") == 8
    assert ladder.daily_cap(settings, "gentle") == 2
    profile.coaching_intensity = CoachingIntensity.drill_sergeant
    await session.commit()
    state = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
    assert state.cap_today == 8 and state.intensity == "drill_sergeant"
    # sends before the local midnight do not count for today
    day_start_utc = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)  # 00:00 Dubai on the 3rd
    await _send(session, user.id, 1, day_start_utc - timedelta(minutes=1), window="a")
    await _send(session, user.id, 1, day_start_utc + timedelta(minutes=1), window="b")
    state = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
    assert state.sends_today == 1


async def test_effective_intensity_temporary_until(profile: Profile, clock: FakeClock) -> None:
    assert ladder.effective_intensity(profile, clock.now()) == "pushy"
    profile.temp_intensity = CoachingIntensity.gentle
    profile.temp_intensity_until = clock.now() + timedelta(days=3)
    assert ladder.effective_intensity(profile, clock.now()) == "gentle"
    profile.temp_intensity_until = clock.now() - timedelta(minutes=1)
    assert ladder.effective_intensity(profile, clock.now()) == "pushy"
    profile.temp_intensity_until = None
    assert ladder.effective_intensity(profile, clock.now()) == "gentle"
    assert ladder.effective_intensity(None, clock.now()) == "pushy"


async def test_quiet_hours_from_profile_with_exemptions(
    session: AsyncSession, user: User, profile: Profile, clock: FakeClock, settings: Settings
) -> None:
    clock.set(datetime(2026, 9, 2, 22, 30, tzinfo=UTC))  # 02:30 Dubai
    state = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
    assert state.in_quiet_hours is True
    assert ladder.quiet_exempt("bedtime_minus_30") and ladder.quiet_exempt("reminder_due")
    assert not ladder.quiet_exempt("no_first_meal")
    profile.quiet_start = time(1, 0)
    profile.quiet_end = time(2, 0)
    await session.commit()
    state = await ladder.compute_ladder(session, user, _fire(), clock=clock, settings=settings)
    assert state.in_quiet_hours is False
    assert ladder.quiet_window(None, settings) == (time(0, 0), time(7, 30))


async def test_cooldown_flag_from_clean_streak(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings
) -> None:
    state = await ladder.compute_ladder(
        session, user, _fire(), clock=clock, settings=settings, clean_streak_days=3
    )
    assert ladder.in_cooldown(state) is True
    state = await ladder.compute_ladder(
        session, user, _fire(), clock=clock, settings=settings, clean_streak_days=2
    )
    assert ladder.in_cooldown(state) is False
