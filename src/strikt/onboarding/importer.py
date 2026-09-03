"""History import (PLAN §10, brief §4): structured rows → meals, workouts, sleep, measurements,
labs, notes and the protocol, all with ``source=imported``.

The model extracts rows in the shapes from ``agent/prompts/import.md``; this module parses them
tolerantly (fields are recognised by shape — a ``HH:MM`` is a time, ``k=v`` groups are numbers,
a slot word is a slot — so a missing time or a swapped column does not lose the row), writes
what it can and reports what it skipped and why. Re-importing the same text is idempotent:
meals dedupe on (day, time, items), workouts and sleep on a synthetic external id,
measurements on (type, instant), labs on (marker, date), notes through ``memory.notes``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import local_datetime
from strikt.core.types import FoodItemIn, LabMarker, Macros
from strikt.db import repo
from strikt.db.models import (
    DataSource,
    Meal,
    MealSlot,
    MealSource,
    Measurement,
    MeasurementType,
    NoteKind,
    User,
)
from strikt.memory import notes as notes_mod
from strikt.nutrition.math import kcal_from_macros

log = structlog.get_logger(__name__)

IMPORTED = "imported"
IMPORT_SOURCE = DataSource.other
MEASUREMENT_HOUR = time(8, 0)
MAX_LINES = 2000

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_KV_RE = re.compile(r"([a-zA-Z_]+)\s*=\s*(-?\d+(?:[.,]\d+)?)")
_REF_RE = re.compile(r"ref\s*=\s*(-?\d+(?:[.,]\d+)?)?\s*[-–]\s*(-?\d+(?:[.,]\d+)?)?", re.IGNORECASE)
_PER_ITEM_RE = re.compile(r"^(.*?)\s*\(([^()]*=[^()]*)\)\s*$")

MACRO_KEYS: dict[str, str] = {
    "kcal": "kcal",
    "cal": "kcal",
    "calories": "kcal",
    "p": "protein_g",
    "protein": "protein_g",
    "protein_g": "protein_g",
    "c": "carbs_g",
    "carbs": "carbs_g",
    "carb": "carbs_g",
    "carbs_g": "carbs_g",
    "f": "fat_g",
    "fat": "fat_g",
    "fat_g": "fat_g",
    "fiber": "fiber_g",
    "fibre": "fiber_g",
    "fiber_g": "fiber_g",
    "sodium": "sodium_mg",
    "sodium_mg": "sodium_mg",
    "alcohol": "alcohol_g",
    "alcohol_g": "alcohol_g",
}
SLOTS: frozenset[str] = frozenset(s.value for s in MealSlot)
KINDS: frozenset[str] = frozenset(
    {"meal", "workout", "sleep", "measurement", "lab", "note", "protocol"}
)
LOOSE_MARKERS: frozenset[str] = frozenset({"loose", "~loose", "неточно"})
MEASUREMENT_ALIASES: dict[str, str] = {
    "waist": "waist",
    "талия": "waist",
    "weight": "weight",
    "вес": "weight",
    "bodyfat": "bodyfat",
    "body_fat": "bodyfat",
    "fat%": "bodyfat",
    "steps": "steps",
    "шаги": "steps",
    "rhr": "rhr",
    "hrv": "hrv",
    "bp_sys": "bp_sys",
    "bp_dia": "bp_dia",
}


@dataclass
class ImportResult:
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "meals": 0,
            "workouts": 0,
            "sleep": 0,
            "measurements": 0,
            "labs": 0,
            "notes": 0,
            "protocol": 0,
        }
    )
    duplicates: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": dict(self.counts),
            "total": self.total,
            "duplicates": self.duplicates,
            "skipped": list(self.skipped[:20]),
            "skipped_total": len(self.skipped),
        }


@dataclass(frozen=True)
class Row:
    """One parsed line: ``kind`` plus the raw fields after it (trimmed, empties dropped)."""

    kind: str
    fields: tuple[str, ...]
    line_no: int
    raw: str


class RowError(ValueError):
    """A line that cannot be imported; the message goes into ``skipped``."""


# ------------------------------------------------------------------------------------ parsing


def parse_rows(text: str) -> tuple[list[Row], list[str]]:
    """Split the text into rows; returns ``(rows, skipped)`` for lines with no known kind."""
    rows: list[Row] = []
    skipped: list[str] = []
    for line_no, raw in enumerate(text.splitlines()[:MAX_LINES], start=1):
        line = raw.strip().strip("`").strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        kind = parts[0].lower().rstrip(":")
        if kind not in KINDS:
            skipped.append(f"line {line_no}: unknown row kind '{parts[0][:20]}'")
            continue
        fields = tuple(p for p in parts[1:] if p)
        rows.append(Row(kind=kind, fields=fields, line_no=line_no, raw=line))
    return rows, skipped


def _num(text: str) -> float:
    return float(text.replace(",", "."))


def parse_kv(text: str) -> dict[str, float]:
    return {key.lower(): _num(value) for key, value in _KV_RE.findall(text)}


def is_kv(text: str) -> bool:
    return bool(_KV_RE.search(text)) and "=" in text


def parse_date(text: str) -> date | None:
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_time(text: str) -> time | None:
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def macros_from_kv(kv: dict[str, float]) -> Macros | None:
    values: dict[str, float] = {}
    for key, value in kv.items():
        target = MACRO_KEYS.get(key)
        if target is not None:
            values[target] = value
    if not values:
        return None
    protein = values.get("protein_g", 0.0)
    carbs = values.get("carbs_g", 0.0)
    fat = values.get("fat_g", 0.0)
    alcohol = values.get("alcohol_g", 0.0)
    kcal = values.get("kcal")
    if kcal is None:
        kcal = kcal_from_macros(protein, carbs, fat, alcohol)
    return Macros(
        kcal=kcal,
        protein_g=protein,
        carbs_g=carbs,
        fat_g=fat,
        fiber_g=values.get("fiber_g", 0.0),
        sodium_mg=values.get("sodium_mg"),
        alcohol_g=alcohol,
    )


def _first_date(fields: Sequence[str]) -> tuple[date, list[str]]:
    rest = list(fields)
    for index, value in enumerate(rest):
        day = parse_date(value)
        if day is not None:
            del rest[index]
            return day, rest
    raise RowError("no ISO date (YYYY-MM-DD)")


def _pop_times(rest: list[str]) -> list[time]:
    times: list[time] = []
    for value in list(rest):
        parsed = parse_time(value)
        if parsed is not None:
            times.append(parsed)
            rest.remove(value)
    return times


# ------------------------------------------------------------------------------------- meals


@dataclass(frozen=True)
class MealRow:
    day: date
    at: time | None
    slot: str
    items: tuple[FoodItemIn, ...]


def _split_items(text: str, meal_macros: Macros | None, loose: bool) -> tuple[FoodItemIn, ...]:
    parts = [p.strip() for p in text.split(";") if p.strip()]
    per_item: list[tuple[str, Macros]] = []
    for part in parts:
        m = _PER_ITEM_RE.match(part)
        if m is None:
            per_item = []
            break
        macros = macros_from_kv(parse_kv(m.group(2)))
        if macros is None:
            per_item = []
            break
        per_item.append((m.group(1).strip() or part, macros))
    if per_item and len(per_item) == len(parts):
        return tuple(
            FoodItemIn(
                name=name, macros=macros, source="user", confidence=0.75, countable=not loose
            )
            for name, macros in per_item
        )
    if meal_macros is None:
        raise RowError("meal without numbers (kcal=… p=… c=… f=…)")
    name = "; ".join(parts) if parts else "imported meal"
    return (
        FoodItemIn(
            name=name[:200], macros=meal_macros, source="user", confidence=0.7, countable=not loose
        ),
    )


def parse_meal(row: Row) -> MealRow:
    day, rest = _first_date(row.fields)
    times = _pop_times(rest)
    slot = "unknown"
    loose = False
    macros: Macros | None = None
    items_text: list[str] = []
    for value in rest:
        lowered = value.lower()
        if lowered in SLOTS:
            slot = lowered
        elif lowered in LOOSE_MARKERS:
            loose = True
        elif is_kv(value) and "(" not in value:
            macros = macros_from_kv(parse_kv(value))
        else:
            items_text.append(value)
    if not items_text:
        raise RowError("meal without items")
    items = _split_items(" ; ".join(items_text), macros, loose)
    return MealRow(day=day, at=times[0] if times else None, slot=slot, items=items)


async def _meal_exists(session: AsyncSession, user_id: int, when: datetime, names: str) -> bool:
    stmt = select(Meal).where(
        Meal.user_id == user_id,
        Meal.source == MealSource.imported,
        Meal.eaten_at == when,
        Meal.deleted_at.is_(None),
    )
    for meal in (await session.scalars(stmt)).all():
        await session.refresh(meal, ["items"])
        if "; ".join(i.name for i in meal.items).casefold() == names.casefold():
            return True
    return False


async def _import_meal(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    meal = parse_meal(row)
    tz = user.timezone or "UTC"
    when = local_datetime(meal.day, meal.at or time(12, 0), tz)
    names = "; ".join(i.name for i in meal.items)
    if await _meal_exists(session, user.id, when, names):
        result.duplicates += 1
        return
    await repo.add_meal_with_items(
        session,
        user.id,
        day_date=meal.day,
        items=meal.items,
        slot=meal.slot,
        source=MealSource.imported,
        logged_at=now,
        eaten_at=when,
        raw_ref={"import_line": row.line_no},
        note=IMPORTED,
    )
    result.counts["meals"] += 1


# --------------------------------------------------------------------------------- workouts


async def _import_workout(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    day, rest = _first_date(row.fields)
    times = _pop_times(rest)
    kv: dict[str, float] = {}
    words: list[str] = []
    for value in rest:
        if is_kv(value):
            kv.update(parse_kv(value))
        else:
            words.append(value)
    sport = " ".join(words).strip().lower() or "activity"
    at = times[0] if times else time(12, 0)
    tz = user.timezone or "UTC"
    started_at = local_datetime(day, at, tz)
    duration = kv.get("duration", kv.get("duration_min", kv.get("min")))
    ended_at = started_at + timedelta(minutes=duration) if duration else None
    avg_hr = kv.get("avg_hr", kv.get("hr"))
    max_hr = kv.get("max_hr")
    _, created = await repo.upsert_workout_by_external(
        session,
        user.id,
        source=IMPORT_SOURCE,
        external_id=f"import:{day.isoformat()}:{at:%H%M}:{sport}"[:128],
        sport=sport[:64],
        started_at=started_at,
        now=now,
        ended_at=ended_at,
        duration_min=duration,
        strain=kv.get("strain"),
        kcal=kv.get("kcal"),
        avg_hr=int(avg_hr) if avg_hr is not None else None,
        max_hr=int(max_hr) if max_hr is not None else None,
        distance_m=kv.get("distance_m", (kv["km"] * 1000 if "km" in kv else None)),
        raw={"imported": True, "line": row.line_no},
    )
    if created:
        result.counts["workouts"] += 1
    else:
        result.duplicates += 1


async def _import_sleep(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    day, rest = _first_date(row.fields)
    times = _pop_times(rest)
    if len(times) < 2:
        raise RowError("sleep needs start and end times (HH:MM | HH:MM)")
    kv: dict[str, float] = {}
    for value in rest:
        if is_kv(value):
            kv.update(parse_kv(value))
    start_t, end_t = times[0], times[1]
    tz = user.timezone or "UTC"
    start_day = day - timedelta(days=1) if start_t > end_t else day
    started_at = local_datetime(start_day, start_t, tz)
    ended_at = local_datetime(day, end_t, tz)
    in_bed = (ended_at - started_at).total_seconds() / 60
    _, created = await repo.upsert_sleep_by_external(
        session,
        user.id,
        source=IMPORT_SOURCE,
        external_id=f"import:sleep:{day.isoformat()}",
        started_at=started_at,
        ended_at=ended_at,
        now=now,
        in_bed_min=kv.get("in_bed", in_bed),
        asleep_min=kv.get("asleep", kv.get("asleep_min")),
        performance_pct=kv.get("performance", kv.get("performance_pct")),
        disturbances=int(kv["disturbances"]) if "disturbances" in kv else None,
        raw={"imported": True, "line": row.line_no},
    )
    if created:
        result.counts["sleep"] += 1
    else:
        result.duplicates += 1


# ----------------------------------------------------------------------- measurements / labs


async def _import_measurement(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    day, rest = _first_date(row.fields)
    times = _pop_times(rest)
    mtype: str | None = None
    value: float | None = None
    unit: str | None = None
    for token in rest:
        lowered = token.lower()
        if mtype is None and lowered in MEASUREMENT_ALIASES:
            mtype = MEASUREMENT_ALIASES[lowered]
        elif value is None and re.fullmatch(r"-?\d+(?:[.,]\d+)?", token):
            value = _num(token)
        elif unit is None:
            unit = token[:16]
    if mtype is None or value is None:
        raise RowError("measurement needs a type (weight/waist/…) and a number")
    unit = unit or {"weight": "kg", "waist": "cm", "bodyfat": "%", "steps": "steps"}.get(mtype, "")
    tz = user.timezone or "UTC"
    measured_at = local_datetime(day, times[0] if times else MEASUREMENT_HOUR, tz)
    exists = await session.scalar(
        select(Measurement.id).where(
            Measurement.user_id == user.id,
            Measurement.type == MeasurementType(mtype),
            Measurement.measured_at == measured_at,
            Measurement.source == IMPORTED,
        )
    )
    if exists is not None:
        result.duplicates += 1
        return
    await repo.add_measurement(
        session,
        user.id,
        type=mtype,
        value=value,
        unit=unit,
        measured_at=measured_at,
        source=IMPORTED,
        raw={"line": row.line_no},
    )
    result.counts["measurements"] += 1


async def _import_lab(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    day, rest = _first_date(row.fields)
    marker: str | None = None
    value: float | None = None
    unit: str | None = None
    ref_low: float | None = None
    ref_high: float | None = None
    flag: str | None = None
    for token in rest:
        ref = _REF_RE.search(token)
        if ref is not None:
            ref_low = _num(ref.group(1)) if ref.group(1) else None
            ref_high = _num(ref.group(2)) if ref.group(2) else None
        elif value is None and re.fullmatch(r"-?\d+(?:[.,]\d+)?", token):
            value = _num(token)
        elif marker is None:
            marker = token[:120]
        elif unit is None and value is not None and token.lower() not in {"high", "low", "normal"}:
            unit = token[:32]
        elif token.lower() in {"high", "low", "normal", "h", "l"}:
            flag = token.lower()
    if marker is None or value is None:
        raise RowError("lab needs a marker name and a number")
    existing = await repo.list_labs(session, user.id, marker=marker, limit=50)
    if any(lab.taken_at == day for lab in existing):
        result.duplicates += 1
        return
    await repo.add_labs(
        session,
        user.id,
        taken_at=day,
        markers=[
            LabMarker(
                marker=marker, value=value, unit=unit, ref_low=ref_low, ref_high=ref_high, flag=flag
            )
        ],
        source=IMPORTED,
        raw_ref={"line": row.line_no},
    )
    result.counts["labs"] += 1


# ------------------------------------------------------------------------- notes / protocol


async def _import_note(
    session: AsyncSession, user: User, row: Row, *, now: datetime, result: ImportResult
) -> None:
    if not row.fields:
        raise RowError("empty note")
    kind_text = row.fields[0].lower()
    kinds = {k.value for k in NoteKind}
    if kind_text in kinds and len(row.fields) > 1:
        kind, text = kind_text, " | ".join(row.fields[1:])
    else:
        kind, text = "preference", " | ".join(row.fields)
    write = await notes_mod.add_note(session, user, kind, text, 0.7, now=now)
    if write.created:
        result.counts["notes"] += 1
    else:
        result.duplicates += 1


def parse_protocol(row: Row) -> tuple[date, Macros, str | None]:
    day, rest = _first_date(row.fields)
    macros: Macros | None = None
    words: list[str] = []
    for value in rest:
        if is_kv(value):
            macros = macros_from_kv(parse_kv(value))
        else:
            words.append(value)
    if macros is None or macros.kcal <= 0:
        raise RowError("protocol needs kcal=… p=… f=… c=… fiber=…")
    return day, macros, (" | ".join(words) or None)


async def _apply_protocols(
    session: AsyncSession,
    user: User,
    protocols: Iterable[tuple[date, Macros, str | None]],
    *,
    now: datetime,
    result: ImportResult,
) -> None:
    latest = max(protocols, key=lambda p: p[0], default=None)
    if latest is None:
        return
    if await repo.get_active_protocol(session, user.id) is not None:
        result.skipped.append(
            "protocol: kept the existing active protocol (import does not override)"
        )
        return
    day, macros, rationale = latest
    await repo.set_active_protocol(
        session,
        user.id,
        kcal=macros.kcal,
        protein_g=macros.protein_g,
        fat_g=macros.fat_g,
        carbs_g=macros.carbs_g,
        fiber_g=macros.fiber_g,
        rationale=f"{IMPORTED} {day.isoformat()}" + (f": {rationale}" if rationale else ""),
        now=now,
    )
    result.counts["protocol"] += 1


# ------------------------------------------------------------------------------- entry point


async def import_history(
    session: AsyncSession, user: User, text: str, *, now: datetime | None = None
) -> ImportResult:
    """Parse ``text`` and write every row it can; never raises on a bad line. Flushes only."""
    now = now or datetime.now(UTC)
    rows, skipped = parse_rows(text)
    result = ImportResult(skipped=skipped)
    protocols: list[tuple[date, Macros, str | None]] = []
    for row in rows:
        try:
            if row.kind == "meal":
                await _import_meal(session, user, row, now=now, result=result)
            elif row.kind == "workout":
                await _import_workout(session, user, row, now=now, result=result)
            elif row.kind == "sleep":
                await _import_sleep(session, user, row, now=now, result=result)
            elif row.kind == "measurement":
                await _import_measurement(session, user, row, now=now, result=result)
            elif row.kind == "lab":
                await _import_lab(session, user, row, now=now, result=result)
            elif row.kind == "note":
                await _import_note(session, user, row, now=now, result=result)
            elif row.kind == "protocol":
                protocols.append(parse_protocol(row))
        except RowError as exc:
            result.skipped.append(f"line {row.line_no} ({row.kind}): {exc}")
        except (ValueError, TypeError) as exc:
            result.skipped.append(f"line {row.line_no} ({row.kind}): {exc}")
    await _apply_protocols(session, user, protocols, now=now, result=result)
    await session.flush()
    log.info(
        "history_imported",
        user_id=user.id,
        counts=result.counts,
        duplicates=result.duplicates,
        skipped=len(result.skipped),
    )
    return result
