"""Typed in-process event bus.

Integrations (WHOOP, Withings, Apple Health) and the turn loop publish events; the proactive
engine subscribes. Handlers run concurrently per publish; a failing handler is logged and never
breaks the publisher. The event dataclasses are the common schema every integration maps to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, TypeVar

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event. ``source`` is the provider/module that produced it."""

    user_id: int
    occurred_at: datetime
    source: str = "internal"


@dataclass(frozen=True, kw_only=True)
class WorkoutEvent(Event):
    external_id: str | None = None
    sport: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_min: float | None = None
    strain: float | None = None
    kcal: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    zones_min: dict[str, float] | None = None
    distance_m: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SleepEvent(Event):
    external_id: str | None = None
    started_at: datetime
    ended_at: datetime
    in_bed_min: float | None = None
    asleep_min: float | None = None
    performance_pct: float | None = None
    stages_min: dict[str, float] | None = None
    respiratory_rate: float | None = None
    disturbances: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RecoveryEvent(Event):
    external_id: str | None = None
    date: date
    score: float | None = None
    rhr: float | None = None
    hrv_ms: float | None = None
    spo2: float | None = None
    skin_temp_c: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class MeasurementEvent(Event):
    type: str
    value: float
    unit: str
    measured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class DayStateChanged(Event):
    """Fired by the turn loop after any tool that changes today's numbers."""

    date: date
    reason: str


@dataclass(frozen=True, kw_only=True)
class UserReplied(Event):
    """Fired when the user sends anything; the ladder resets on it."""

    turn_id: int | None = None


E = TypeVar("E", bound=Event)
Handler = Callable[[E], Awaitable[None]]


class EventBus:
    """Subscribe by event class; publishing dispatches to handlers of the class and its bases."""

    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Callable[[Any], Awaitable[None]]]] = {}

    def subscribe(self, event_type: type[E], handler: Handler[E]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type``; returns an unsubscribe callable."""
        self._handlers.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            handlers = self._handlers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    def handlers_for(self, event: Event) -> list[Callable[[Any], Awaitable[None]]]:
        found: list[Callable[[Any], Awaitable[None]]] = []
        for cls in type(event).__mro__:
            if cls is object:
                continue
            found.extend(self._handlers.get(cls, ()))
        return found

    async def publish(self, event: Event) -> int:
        """Run every matching handler concurrently. Returns how many handlers ran."""
        handlers = self.handlers_for(event)
        if not handlers:
            return 0
        results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                log.error(
                    "event_handler_failed",
                    event=type(event).__name__,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    error=repr(result),
                )
        return len(handlers)

    def clear(self) -> None:
        self._handlers.clear()
