"""Escalation ladder state per ``(user, window_key)`` (brief §7.2, §7.3; PLAN §8).

Rules:
- step = last unanswered step in the window + 1, capped at ``MAX_STEP`` (4); step 1 when the
  window has no send yet, when the last send was answered (``responded_at``), or when the
  user's latest message is newer than the last send (a reply the bus may have missed);
- daily cap: 5 (``drill_sergeant`` 8, ``gentle`` 2), counted from the local midnight;
- quiet hours come from the profile (default 00:00–07:30); the engine exempts
  ``bedtime_minus_30`` and user-set reminders;
- effective intensity honours ``temp_intensity`` until ``temp_intensity_until``;
- three clean days set the cooldown flag (``clean_streak_days`` ≥ 3): class A pressure backs
  off, class B/C still fire.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.config import Settings
from strikt.core.clock import Clock, ensure_utc, in_quiet_hours, local_day_bounds, to_local
from strikt.db import repo
from strikt.db.models import CoachingIntensity, ProactiveSend, Profile, User
from strikt.proactive import stats
from strikt.proactive.types import LadderState, TriggerFire, TriggerName

MAX_STEP = 4
COOLDOWN_CLEAN_DAYS = 3
DEFAULT_CAP_GENTLE = 2
QUIET_EXEMPT: frozenset[TriggerName] = frozenset({"bedtime_minus_30", "reminder_due"})


@dataclass(frozen=True, kw_only=True)
class WindowStatus:
    """What the window looks like before this fire."""

    last: ProactiveSend | None
    answered: bool  # the last send got a reply (or the user wrote after it)
    minutes_since_last: float | None
    next_step: int  # the step the next send would be (1..MAX_STEP)
    exhausted: bool  # step 4 already sent and still unanswered


def effective_intensity(profile: Profile | None, now: datetime) -> str:
    """The intensity in force: the temporary one until its deadline, else the base one."""
    if profile is None:
        return str(CoachingIntensity.pushy)
    if profile.temp_intensity is not None:
        until = profile.temp_intensity_until
        if until is None or ensure_utc(until) > ensure_utc(now):
            return str(profile.temp_intensity)
    return str(profile.coaching_intensity)


def daily_cap(settings: Settings, intensity: str) -> int:
    if intensity == str(CoachingIntensity.gentle):
        return int(getattr(settings, "proactive_daily_cap_gentle", DEFAULT_CAP_GENTLE))
    return settings.daily_cap_for(intensity)


def quiet_window(profile: Profile | None, settings: Settings) -> tuple[time, time]:
    if profile is None:
        return settings.quiet_start, settings.quiet_end
    return profile.quiet_start or settings.quiet_start, profile.quiet_end or settings.quiet_end


def is_quiet(local_now: datetime, profile: Profile | None, settings: Settings) -> bool:
    start, end = quiet_window(profile, settings)
    return in_quiet_hours(local_now, start, end)


def quiet_exempt(name: TriggerName) -> bool:
    return name in QUIET_EXEMPT


async def inspect_window(
    session: AsyncSession,
    user_id: int,
    window_key: str,
    *,
    now: datetime,
    last_user_message_at: datetime | None = None,
) -> WindowStatus:
    last = await repo.last_send_for_window(session, user_id, window_key)
    if last is None:
        return WindowStatus(
            last=None, answered=False, minutes_since_last=None, next_step=1, exhausted=False
        )
    sent_at = ensure_utc(last.sent_at)
    answered = last.responded_at is not None
    if not answered and last_user_message_at is not None:
        answered = ensure_utc(last_user_message_at) > sent_at
    minutes = (ensure_utc(now) - sent_at).total_seconds() / 60
    if answered:
        return WindowStatus(
            last=last, answered=True, minutes_since_last=minutes, next_step=1, exhausted=False
        )
    exhausted = last.step >= MAX_STEP
    return WindowStatus(
        last=last,
        answered=False,
        minutes_since_last=minutes,
        next_step=min(last.step + 1, MAX_STEP),
        exhausted=exhausted,
    )


async def compute_ladder(
    session: AsyncSession,
    user: User,
    fire: TriggerFire,
    *,
    clock: Clock,
    settings: Settings,
    profile: Profile | None = None,
    window: WindowStatus | None = None,
    clean_streak_days: int | None = None,
) -> LadderState:
    """The ``LadderState`` the decider sees for this fire."""
    now = clock.now()
    tz = user.timezone
    local_now = to_local(now, tz)
    if profile is None:
        profile = await repo.get_profile(session, user.id)
    if window is None:
        window = await inspect_window(session, user.id, fire.window_key, now=now)
    day_start, _ = local_day_bounds(local_now.date(), tz)
    sends_today = await repo.count_sends_today(session, user.id, since=day_start)
    intensity = effective_intensity(profile, now)
    rate = await stats.response_rate(session, user, fire.name, now=now)
    if clean_streak_days is None:
        protocol = await repo.get_active_protocol(session, user.id)
        streaks = await stats.compute_streaks(
            session,
            user.id,
            today=local_now.date(),
            tz=tz,
            kcal_target=protocol.kcal if protocol is not None else 0.0,
            bed_time=profile.bed_time if profile is not None else None,
        )
        clean_streak_days = streaks.clean_days
    return LadderState(
        step=window.next_step,
        sends_today=sends_today,
        cap_today=daily_cap(settings, intensity),
        intensity=intensity,
        response_rate=rate,
        clean_streak_days=clean_streak_days,
        in_quiet_hours=is_quiet(local_now, profile, settings),
    )


def in_cooldown(ladder: LadderState) -> bool:
    return ladder.clean_streak_days >= COOLDOWN_CLEAN_DAYS


def followup_at(now: datetime, settings: Settings) -> datetime:
    return ensure_utc(now) + timedelta(minutes=settings.proactive_followup_minutes)


def too_soon(window: WindowStatus, settings: Settings) -> bool:
    """A second fire inside the follow-up delay is a duplicate, not an escalation."""
    if window.last is None or window.answered or window.minutes_since_last is None:
        return False
    return window.minutes_since_last < settings.proactive_followup_minutes
