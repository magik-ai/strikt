"""Body tools: log_measurement, ingest_lab_report (PLAN §6.4, brief §3.2 weight/waist rules).

Weight weekly, waist at the navel biweekly and fasted; the handler returns the previous reading,
the 7-day trend for weight (never a comment on a single reading) and the "that's water" note
when yesterday carried a salty/alcohol flag. Labs are stored as rows; the reply references a
marker only where it changes advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.tools.common import fail, local_day, ok, rnd, to_utc
from strikt.core.clock import ensure_utc
from strikt.core.types import LabMarker
from strikt.db import repo
from strikt.db.models import MeasurementType

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)

WATER_FLAGS: frozenset[str] = frozenset({"salty", "alcohol"})
CADENCE_ATTR: dict[str, str] = {"weight": "weight_cadence_days", "waist": "waist_cadence_days"}
CADENCE_DEFAULT: dict[str, int] = {"weight": 7, "waist": 14}


def _cadence(ctx: ToolContext, mtype: str) -> int | None:
    attr = CADENCE_ATTR.get(mtype)
    if attr is None:
        return None
    if ctx.profile is None:
        return CADENCE_DEFAULT[mtype]
    return int(getattr(ctx.profile, attr, CADENCE_DEFAULT[mtype]) or CADENCE_DEFAULT[mtype])


async def _water_note(ctx: ToolContext, day: date) -> str | None:
    """Brief §3.2: after a salty/alcohol day, tomorrow's weight is water."""
    yesterday = await repo.get_day(ctx.session, ctx.user_id, day - timedelta(days=1))
    if yesterday is None or not yesterday.flags:
        return None
    hits = sorted(WATER_FLAGS & {str(f) for f in yesterday.flags})
    if not hits:
        return None
    return f"yesterday was flagged {', '.join(hits)}: this reading is water, not fat"


async def _weight_trend(ctx: ToolContext, measured_at: Any) -> dict[str, Any]:
    """7-day average now vs the 7 days before; the reply talks trend, never one reading."""
    now = ensure_utc(measured_at)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)
    this_week = await repo.list_measurements_range(
        ctx.session, ctx.user_id, week_ago, now + timedelta(seconds=1), type="weight"
    )
    last_week = await repo.list_measurements_range(
        ctx.session, ctx.user_id, two_weeks_ago, week_ago, type="weight"
    )

    def avg(rows: list[Any]) -> float | None:
        return sum(r.value for r in rows) / len(rows) if rows else None

    a, b = avg(this_week), avg(last_week)
    out: dict[str, Any] = {
        "avg_7d": rnd(a, 1),
        "readings_7d": len(this_week),
        "avg_prev_7d": rnd(b, 1),
    }
    if a is not None and b is not None:
        out["delta_7d"] = rnd(a - b, 1)
    return out


async def log_measurement(ctx: ToolContext, args: schemas.LogMeasurementInput) -> ToolResult:
    if args.value <= 0:
        return fail("log_measurement: value must be positive")
    now = ctx.clock.now()
    measured_at = to_utc(args.measured_at, ctx.tz) if args.measured_at is not None else now
    if measured_at > now + timedelta(minutes=5):
        return fail("log_measurement: measured_at is in the future")
    mtype = MeasurementType(args.type)
    previous = await repo.latest_by_type(ctx.session, ctx.user_id, mtype)
    row = await repo.add_measurement(
        ctx.session,
        ctx.user_id,
        type=mtype,
        value=args.value,
        unit=args.unit,
        measured_at=measured_at,
        source=args.source or "manual",
        note=args.note,
    )
    day = local_day(measured_at, ctx.tz)
    result: dict[str, Any] = {
        "id": row.id,
        "type": mtype.value,
        "value": rnd(args.value, 2),
        "unit": args.unit,
        "date": day.isoformat(),
    }
    notes: list[str] = []
    if previous is not None and ensure_utc(previous.measured_at) <= measured_at:
        days_since = (measured_at - ensure_utc(previous.measured_at)).days
        result["previous"] = {
            "value": rnd(previous.value, 2),
            "unit": previous.unit,
            "date": local_day(previous.measured_at, ctx.tz).isoformat(),
            "days_ago": days_since,
        }
        if previous.unit == args.unit:
            result["delta_vs_previous"] = rnd(args.value - previous.value, 2)
        cadence = _cadence(ctx, mtype.value)
        if cadence is not None and 0 <= days_since < max(1, cadence // 2):
            what = "weekly, not daily" if mtype.value == "weight" else f"every {cadence} days"
            notes.append(f"{mtype.value} was measured {days_since} day(s) ago; cadence is {what}")
    cadence = _cadence(ctx, mtype.value)
    if cadence is not None:
        result["cadence_days"] = cadence
        result["next_due"] = (day + timedelta(days=cadence)).isoformat()
    if mtype is MeasurementType.weight:
        result["trend"] = await _weight_trend(ctx, measured_at)
        water = await _water_note(ctx, day)
        if water:
            notes.append(water)
    if mtype is MeasurementType.waist:
        notes.append("waist: at the navel, fasted, same time of day")
    profile = ctx.profile
    if (
        profile is not None
        and profile.primary_kpi is not None
        and profile.primary_kpi.value == mtype.value
    ):
        kpi: dict[str, Any] = {"unit": profile.kpi_unit}
        if profile.kpi_target_low is not None:
            kpi["good"] = rnd(profile.kpi_target_low, 1)
            kpi["to_good"] = rnd(args.value - profile.kpi_target_low, 1)
        if profile.kpi_target_high is not None:
            kpi["excellent"] = rnd(profile.kpi_target_high, 1)
        result["kpi"] = kpi
    if notes:
        result["notes"] = notes
    log.info("measurement_logged", user_id=ctx.user_id, type=mtype.value, id=row.id)
    return ok(result)


def _out_of_range(marker: LabMarker) -> str | None:
    if marker.flag and marker.flag.lower() not in {"normal", "ok", "in range", "n"}:
        return marker.flag
    if marker.ref_high is not None and marker.value > marker.ref_high:
        return "high"
    if marker.ref_low is not None and marker.value < marker.ref_low:
        return "low"
    return None


async def ingest_lab_report(ctx: ToolContext, args: schemas.IngestLabReportInput) -> ToolResult:
    if not args.markers:
        return fail("ingest_lab_report: no markers read")
    today = ctx.local_date
    if args.taken_at > today:
        return fail("ingest_lab_report: taken_at is in the future")
    existing = {
        (lab.marker.lower(), lab.taken_at)
        for lab in await repo.list_labs(ctx.session, ctx.user_id, limit=500)
    }
    fresh = [m for m in args.markers if (m.marker.lower(), args.taken_at) not in existing]
    skipped = len(args.markers) - len(fresh)
    rows = await repo.add_labs(
        ctx.session,
        ctx.user_id,
        taken_at=args.taken_at,
        markers=fresh,
        source=args.source or "photo",
        raw_ref={"message_id": ctx.incoming.message_id} if ctx.incoming else None,
    )
    flagged = [
        {
            "marker": m.marker,
            "value": rnd(m.value, 2),
            "unit": m.unit,
            "ref": [rnd(m.ref_low, 2), rnd(m.ref_high, 2)],
            "flag": flag,
        }
        for m in args.markers
        if (flag := _out_of_range(m)) is not None
    ]
    log.info("labs_ingested", user_id=ctx.user_id, stored=len(rows), skipped=skipped)
    return ok(
        {
            "taken_at": args.taken_at.isoformat(),
            "stored": len(rows),
            "skipped_duplicates": skipped,
            "out_of_range": flagged,
            "markers": [m.marker for m in args.markers],
        }
    )
