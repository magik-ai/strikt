"""The ``<body>`` block: the latest body numbers, for the turn context.

Why it exists: weigh-ins, pressure and steps arrive from Withings and Apple Health by themselves,
but the coach only saw them when it thought to call ``get_history``. It asked "what do you weigh
now?" a day after the scale had sent the number. Now every turn carries one line of the body
the database holds, each number with its date, so a claim about the scale has somewhere to come
from and a question about it is never needed.

One line, ~200 characters: weight (latest, 7-day average, change over about 30 days), body fat,
waist, visceral fat, blood pressure with heart rate, yesterday's steps and active kcal. Every
figure is read from ``measurements``; nothing is inferred. Three queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select

from strikt.core.clock import ensure_utc, local_day_bounds, to_local
from strikt.db.models import Measurement, MeasurementType

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: How far back the block looks for the latest of each number.
WINDOW_DAYS = 60
#: A 30-day change needs a reading from this window (25 to 35 days before the latest one).
TREND_DAYS = (25, 35)

_LATEST: tuple[tuple[MeasurementType, str | None, str], ...] = (
    (MeasurementType.bodyfat, None, "body fat"),
    (MeasurementType.waist, None, "waist"),
    (MeasurementType.other, "visceral_fat", "visceral fat"),
)


@dataclass(frozen=True)
class _Reading:
    type: MeasurementType
    note: str | None
    value: float
    unit: str
    at: datetime
    source: str


def _n(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


async def _readings(session: AsyncSession, user_id: int, since: datetime) -> list[_Reading]:
    rows = await session.scalars(
        select(Measurement)
        .where(Measurement.user_id == user_id, Measurement.measured_at >= since)
        .order_by(Measurement.measured_at, Measurement.id)
    )
    return [
        _Reading(
            type=MeasurementType(row.type),
            note=row.note,
            value=float(row.value),
            unit=row.unit,
            at=ensure_utc(row.measured_at),
            source=row.source,
        )
        for row in rows
    ]


def _latest(readings: list[_Reading], kind: MeasurementType, note: str | None) -> _Reading | None:
    found = [r for r in readings if r.type == kind and (note is None or r.note == note)]
    if kind != MeasurementType.other:
        found = [r for r in found if r.note in (None, "")] or found
    return found[-1] if found else None


def _weight(readings: list[_Reading], tz: str) -> str | None:
    weights = [r for r in readings if r.type == MeasurementType.weight]
    if not weights:
        return None
    last = weights[-1]
    text = f"weight {_n(last.value)} {last.unit} ({to_local(last.at, tz):%m-%d}, {last.source})"
    week = [r.value for r in weights if r.at > last.at - timedelta(days=7)]
    if len(week) >= 2:
        text += f" · 7d avg {_n(sum(week) / len(week))} ({len(week)} readings)"
    low, high = TREND_DAYS
    earlier = [
        r
        for r in weights
        if last.at - timedelta(days=high) <= r.at <= last.at - timedelta(days=low)
    ]
    if earlier:
        delta = last.value - earlier[-1].value
        sign = "+" if delta > 0 else ""
        text += f" · 30d {sign}{_n(delta)}"
    return text


def _pressure(readings: list[_Reading], tz: str) -> str | None:
    sys_ = _latest(readings, MeasurementType.bp_sys, None)
    if sys_ is None:
        return None
    dia = next(
        (
            r
            for r in reversed(readings)
            if r.type == MeasurementType.bp_dia and abs((r.at - sys_.at).total_seconds()) < 300
        ),
        None,
    )
    pulse = next(
        (
            r
            for r in reversed(readings)
            if r.note == "heart_rate" and abs((r.at - sys_.at).total_seconds()) < 300
        ),
        None,
    )
    text = f"bp {_n(sys_.value)}/{_n(dia.value) if dia else '?'}"
    if pulse is not None:
        text += f", pulse {_n(pulse.value)}"
    return text + f" ({to_local(sys_.at, tz):%m-%d})"


def _activity(readings: list[_Reading], today: date, tz: str) -> str | None:
    """Yesterday's steps and active kcal (today's are still counting)."""
    start, end = local_day_bounds(today - timedelta(days=1), tz)
    steps = [r for r in readings if r.type == MeasurementType.steps and start <= r.at < end]
    active = [r for r in readings if r.note == "active_kcal" and start <= r.at < end]
    bits: list[str] = []
    if steps:
        bits.append(f"steps yesterday {round(max(r.value for r in steps))}")
    if active:
        bits.append(f"active {round(max(r.value for r in active))} kcal")
    return ", ".join(bits) or None


async def body_block(session: AsyncSession, user_id: int, *, today: date, tz: str) -> str:
    """The block's text, or an empty string when nothing was measured in the window."""
    start, _ = local_day_bounds(today - timedelta(days=WINDOW_DAYS), tz)
    readings = await _readings(session, user_id, start)
    if not readings:
        return ""
    parts: list[str] = []
    weight = _weight(readings, tz)
    if weight:
        parts.append(weight)
    for kind, note, label in _LATEST:
        found = _latest(readings, kind, note)
        if found is not None:
            unit = "" if found.unit in ("index", "") else f" {found.unit}".replace(" %", "%")
            parts.append(f"{label} {_n(found.value)}{unit} ({to_local(found.at, tz):%m-%d})")
    pressure = _pressure(readings, tz)
    if pressure:
        parts.append(pressure)
    activity = _activity(readings, today, tz)
    if activity:
        parts.append(activity)
    return " | ".join(parts)
