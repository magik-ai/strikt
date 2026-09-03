"""Time handling: store UTC, compute local with ``zoneinfo``. ``FakeClock`` for tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = ensure_utc(now) if now else datetime(2026, 9, 3, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = ensure_utc(now)

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def tick(self, seconds: float = 1.0) -> None:
        self.advance(timedelta(seconds=seconds))


def zone(tz: str) -> ZoneInfo:
    """IANA zone or UTC when the name is unknown (never raises into a handler)."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes (SQLite returns naive) and normalise aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_local(dt: datetime, tz: str) -> datetime:
    return ensure_utc(dt).astimezone(zone(tz))


def local_now(clock: Clock, tz: str) -> datetime:
    return to_local(clock.now(), tz)


def local_date(clock: Clock, tz: str) -> date:
    return local_now(clock, tz).date()


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` of a local calendar day."""
    start = datetime.combine(day, time.min, tzinfo=zone(tz))
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def local_datetime(day: date, at: time, tz: str) -> datetime:
    """A local wall-clock instant expressed in UTC."""
    return datetime.combine(day, at, tzinfo=zone(tz)).astimezone(UTC)


def in_quiet_hours(local: datetime, quiet_start: time, quiet_end: time) -> bool:
    """True when ``local.time()`` falls inside a window that may cross midnight."""
    now_t = local.time()
    if quiet_start <= quiet_end:
        return quiet_start <= now_t < quiet_end
    return now_t >= quiet_start or now_t < quiet_end


def week_start(day: date) -> date:
    """Monday of the ISO week containing ``day``."""
    return day - timedelta(days=day.weekday())
