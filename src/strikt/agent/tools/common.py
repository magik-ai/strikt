"""Helpers shared by the tool handlers: compact JSON, rounding, time, day state.

Every handler returns a short JSON-ish text the model can quote. Numbers are rounded here
(kcal and milligrams whole, grams to one decimal) so no handler formats numbers by hand.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from strikt.core.clock import coaching_day, ensure_utc, to_local, zone
from strikt.core.types import DayState, Flag, Macros
from strikt.memory.daystate import DayStateBuilder

if TYPE_CHECKING:
    from strikt.agent.tools.registry import ToolContext, ToolResult

WHOLE_KEYS: frozenset[str] = frozenset({"kcal", "sodium_mg", "steps", "avg_hr", "max_hr"})


# ------------------------------------------------------------------------------------ numbers


def rnd(value: float | None, ndigits: int = 1) -> float | int | None:
    """Round for display: ``None`` stays ``None``; integral results become ``int``."""
    if value is None:
        return None
    rounded = round(float(value), ndigits)
    if ndigits <= 0 or rounded == int(rounded):
        return int(rounded)
    return rounded


def macros_dict(m: Macros) -> dict[str, Any]:
    """``{"kcal": 412, "P": 38.5, "C": 12, "F": 20.1, "fiber": 4}`` (+ sodium/alcohol if set)."""
    out: dict[str, Any] = {
        "kcal": rnd(m.kcal, 0),
        "P": rnd(m.protein_g),
        "C": rnd(m.carbs_g),
        "F": rnd(m.fat_g),
        "fiber": rnd(m.fiber_g),
    }
    if m.sodium_mg is not None:
        out["sodium_mg"] = rnd(m.sodium_mg, 0)
    if m.alcohol_g:
        out["alcohol_g"] = rnd(m.alcohol_g)
    return out


def _default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat(timespec="minutes")
    if isinstance(value, date | time):
        return value.isoformat()
    return str(value)


def _rounded(node: Any, key: str | None = None) -> Any:
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, float):
        return rnd(node, 0 if key in WHOLE_KEYS else 2)
    if isinstance(node, Mapping):
        return {str(k): _rounded(v, str(k)) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_rounded(v, key) for v in node]
    return node


def compact(data: Any) -> str:
    """Compact JSON (no spaces, unicode kept, floats rounded, dates as ISO strings)."""
    return json.dumps(_rounded(data), ensure_ascii=False, separators=(",", ":"), default=_default)


def ok(data: Any) -> ToolResult:
    from strikt.agent.tools.registry import ToolResult

    return ToolResult(content=compact(data))


def fail(message: str) -> ToolResult:
    from strikt.agent.tools.registry import ToolResult

    return ToolResult.error(message)


# --------------------------------------------------------------------------------------- time


def to_utc(dt: datetime, tz: str) -> datetime:
    """Naive datetimes are wall-clock time in ``tz``; aware ones are normalised to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone(tz)).astimezone(UTC)
    return dt.astimezone(UTC)


def local_day(dt: datetime, tz: str) -> date:
    return to_local(dt, tz).date()


def meal_day(ctx: ToolContext, eaten_at: datetime) -> date:
    """The coaching date a meal belongs to: the calendar date, except that a meal eaten after
    midnight but before the rollover (03:00, or the bedtime + 1 h for a bedtime past 02:00, never
    past 06:00; see ``core.clock.day_rollover``) is the evening's day."""
    profile = ctx.profile
    bed = profile.bed_time if profile is not None else None
    wake = profile.wake_time if profile is not None else None
    return coaching_day(to_local(eaten_at, ctx.tz), bed, wake)


def hhmm(dt: datetime, tz: str) -> str:
    return to_local(ensure_utc(dt), tz).strftime("%H:%M")


def minutes_between(a: datetime, b: datetime) -> float:
    return (ensure_utc(b) - ensure_utc(a)).total_seconds() / 60.0


def clock_diff_min(actual: time, target: time) -> int:
    """Signed minutes from ``target`` to ``actual`` on a 24 h ring (00:30 vs 23:50 = +40)."""
    diff = (actual.hour * 60 + actual.minute) - (target.hour * 60 + target.minute)
    return (diff + 720) % 1440 - 720


# ---------------------------------------------------------------------------------- day state


async def build_state(ctx: ToolContext, day: date | None = None) -> DayState:
    """Today's (or ``day``'s) ``DayState`` from the typed rows, with protocol targets."""
    builder = DayStateBuilder(ctx.clock, ctx.settings)
    return await builder.day_state(ctx.session, ctx.user, day or ctx.local_date)


def state_numbers(state: DayState) -> dict[str, Any]:
    """The numbers every food reply needs: totals, targets, remaining (negative = over)."""
    return {
        "date": state.date.isoformat(),
        "totals": macros_dict(state.totals.macros),
        "targets": macros_dict(state.targets),
        "remaining": {
            "kcal": rnd(state.remaining.kcal, 0),
            "P": rnd(state.remaining.protein_g),
            "C": rnd(state.remaining.carbs_g),
            "F": rnd(state.remaining.fat_g),
            "fiber": rnd(state.remaining.fiber_g),
        },
        "meals": state.totals.meals,
        "closed": state.closed,
    }


def health_context(ctx: ToolContext) -> str | None:
    return ctx.profile.health_context if ctx.profile is not None else None


def flag_line(flag: Flag) -> str:
    """``code: message`` on one line (severity shown only when it is not a warning)."""
    prefix = f"{flag.code}"
    if flag.severity != "warn":
        prefix += f" ({flag.severity})"
    return f"{prefix}: {flag.message}"


def flag_lines(flags: Sequence[Flag]) -> list[str]:
    return [flag_line(flag) for flag in flags]


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
