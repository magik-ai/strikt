"""Training tools: log_workout, log_sleep (PLAN §6.4, brief §3.4-3.5).

``log_workout`` stores the session (deduplicated by provider id, or by sport + start within ten
minutes for screenshots) and returns the comparison facts the reply comments on: duration, avg
HR, kcal, density (kcal per minute), zone split, the last session of the same sport and the
30-day average — plus the bedtime link when a session ends close to the agreed bedtime.
``log_sleep`` returns onset versus bedtime and wake versus the anchor.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

from strikt.agent.tools.common import (
    clock_diff_min,
    fail,
    hhmm,
    minutes_between,
    ok,
    rnd,
    to_utc,
)
from strikt.core.clock import ensure_utc, to_local
from strikt.db import repo
from strikt.db.models import DataSource, Workout

if TYPE_CHECKING:
    from datetime import datetime

    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)

DUPLICATE_WINDOW_MIN = 10
BEDTIME_LINK_MIN = 90
ZONE_KEYS: tuple[str, ...] = ("z0", "z1", "z2", "z3", "z4", "z5")


def normalize_sport(sport: str) -> str:
    """Lower-case, single-spaced WHOOP-style sport names (``sport_name`` is free text)."""
    return " ".join(sport.strip().lower().replace("_", " ").split()) or "activity"


def density(kcal: float | None, duration_min: float | None) -> float | None:
    if not kcal or not duration_min or duration_min <= 0:
        return None
    return kcal / duration_min


def zone_split(zones: dict[str, Any] | None) -> dict[str, Any] | None:
    """Minutes and percentages per zone, plus the share of z0+z1 (rest / very light)."""
    if not zones:
        return None
    minutes = {k: float(v) for k, v in zones.items() if v is not None}
    total = sum(minutes.values())
    if total <= 0:
        return None
    pct = {k: rnd(minutes[k] / total * 100, 0) for k in ZONE_KEYS if k in minutes}
    low = sum(minutes.get(k, 0.0) for k in ("z0", "z1"))
    return {
        "minutes": {k: rnd(v, 0) for k, v in minutes.items()},
        "pct": pct,
        "low_pct": rnd(low / total * 100, 0),
    }


def _workout_facts(row: Workout, tz: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "sport": row.sport,
        "start": hhmm(row.started_at, tz),
        "date": to_local(row.started_at, tz).date().isoformat(),
        "duration_min": rnd(row.duration_min, 0),
        "strain": rnd(row.strain),
        "kcal": rnd(row.kcal, 0),
        "avg_hr": row.avg_hr,
        "max_hr": row.max_hr,
        "density_kcal_per_min": rnd(density(row.kcal, row.duration_min), 2),
    }
    if row.ended_at is not None:
        out["end"] = hhmm(row.ended_at, tz)
    split = zone_split(row.zones_min)
    if split is not None:
        out["zones"] = split
    if row.distance_m:
        out["distance_km"] = rnd(row.distance_m / 1000, 2)
    return out


async def _find_duplicate(ctx: ToolContext, sport: str, started_at: datetime) -> Workout | None:
    """Same sport starting within ±10 minutes: a screenshot re-sent, not a second session."""
    lo = started_at - timedelta(minutes=DUPLICATE_WINDOW_MIN)
    hi = started_at + timedelta(minutes=DUPLICATE_WINDOW_MIN)
    stmt = (
        select(Workout)
        .where(
            Workout.user_id == ctx.user_id,
            func.lower(Workout.sport) == sport,
            Workout.started_at >= lo,
            Workout.started_at <= hi,
        )
        .order_by(Workout.id.desc())
        .limit(1)
    )
    return (await ctx.session.scalars(stmt)).first()


def _bedtime_link(ctx: ToolContext, ended_at: datetime | None) -> str | None:
    if ended_at is None or ctx.profile is None or ctx.profile.bed_time is None:
        return None
    local_end = to_local(ended_at, ctx.tz)
    gap = -clock_diff_min(local_end.time(), ctx.profile.bed_time)
    if 0 <= gap <= BEDTIME_LINK_MIN:
        return (
            f"ended {local_end:%H:%M}, {gap} min before the {ctx.profile.bed_time:%H:%M} bedtime"
            " — late intense training pushes sleep onset back"
        )
    if gap < 0 and gap > -240:
        return f"ended {local_end:%H:%M}, after the {ctx.profile.bed_time:%H:%M} bedtime"
    return None


async def log_workout(ctx: ToolContext, args: schemas.LogWorkoutInput) -> ToolResult:
    sport = normalize_sport(args.sport)
    started_at = to_utc(args.started_at, ctx.tz)
    ended_at = to_utc(args.ended_at, ctx.tz) if args.ended_at is not None else None
    duration = args.duration_min
    if duration is None and ended_at is not None:
        duration = minutes_between(started_at, ended_at)
    if duration is not None and duration <= 0:
        return fail("log_workout: duration must be positive")
    if ended_at is None and duration is not None:
        ended_at = started_at + timedelta(minutes=duration)
    zones = args.zones_min.model_dump(exclude_none=True) if args.zones_min else None
    now = ctx.clock.now()

    external_id = args.external_id
    source = DataSource(args.source)
    duplicate: Workout | None = None
    if external_id is None:
        duplicate = await _find_duplicate(ctx, sport, started_at)
        if duplicate is not None:
            external_id = duplicate.external_id
            source = duplicate.source
    if external_id is None and duplicate is not None:
        # a previous manual row without a provider id: give both the same synthetic id
        external_id = f"manual:{started_at:%Y%m%d%H%M}:{sport}"
        duplicate.external_id = external_id
        duplicate.source = source
        await ctx.session.flush()

    row, created = await repo.upsert_workout_by_external(
        ctx.session,
        ctx.user_id,
        source=source,
        external_id=external_id,
        sport=sport,
        started_at=started_at,
        now=now,
        ended_at=ended_at,
        duration_min=duration,
        strain=args.strain,
        kcal=args.kcal,
        avg_hr=args.avg_hr,
        max_hr=args.max_hr,
        zones_min=zones,
        distance_m=args.distance_m,
        raw={"tool": "log_workout"},
        note=args.note,
    )

    previous = await repo.last_same_sport(
        ctx.session, ctx.user_id, sport, before=started_at, exclude_id=row.id
    )
    avg_sport = await repo.avg_30d(ctx.session, ctx.user_id, now=now, sport=sport)
    avg_all = await repo.avg_30d(ctx.session, ctx.user_id, now=now)

    result: dict[str, Any] = {
        "workout": _workout_facts(row, ctx.tz),
        "created": created,
        "duplicate_of": None if created else row.id,
    }
    if previous is not None:
        prev = _workout_facts(previous, ctx.tz)
        result["last_same_sport"] = prev
        deltas: dict[str, Any] = {}
        for key in ("duration_min", "kcal", "avg_hr", "strain", "density_kcal_per_min"):
            a, b = result["workout"].get(key), prev.get(key)
            if a is not None and b is not None:
                deltas[key] = rnd(float(a) - float(b), 2 if key.startswith("density") else 1)
        result["vs_last"] = deltas
    result["avg_30d_same_sport"] = {
        "sessions": avg_sport.count,
        "duration_min": rnd(avg_sport.duration_min, 0),
        "strain": rnd(avg_sport.strain),
        "kcal": rnd(avg_sport.kcal, 0),
        "avg_hr": rnd(avg_sport.avg_hr, 0),
        "density_kcal_per_min": rnd(density(avg_sport.kcal, avg_sport.duration_min), 2),
    }
    result["avg_30d_all"] = {"sessions": avg_all.count, "strain": rnd(avg_all.strain)}
    link = _bedtime_link(ctx, row.ended_at)
    if link:
        result["bedtime_link"] = link
    if any(word in sport for word in ("strength", "weight", "lift", "power", "functional")):
        result["note"] = "heavy strength work legitimately shows low strain; judge by load, not HR"
    log.info("workout_logged", user_id=ctx.user_id, workout_id=row.id, created=created)
    return ok(result)


async def log_sleep(ctx: ToolContext, args: schemas.LogSleepInput) -> ToolResult:
    started_at = to_utc(args.started_at, ctx.tz)
    ended_at = to_utc(args.ended_at, ctx.tz)
    if ended_at <= started_at:
        return fail("log_sleep: ended_at must be after started_at")
    in_bed = (
        args.in_bed_min if args.in_bed_min is not None else minutes_between(started_at, ended_at)
    )
    asleep = args.asleep_min
    if asleep is None and args.stages_min is not None:
        stages = args.stages_min.model_dump(exclude_none=True)
        awake = float(stages.pop("awake", 0.0) or 0.0)
        total = sum(float(v) for v in stages.values())
        asleep = total if total > 0 else max(0.0, in_bed - awake)
    now = ctx.clock.now()
    row, created = await repo.upsert_sleep_by_external(
        ctx.session,
        ctx.user_id,
        source=DataSource(args.source),
        external_id=args.external_id,
        started_at=started_at,
        ended_at=ended_at,
        now=now,
        in_bed_min=in_bed,
        asleep_min=asleep,
        performance_pct=args.performance_pct,
        stages_min=args.stages_min.model_dump(exclude_none=True) if args.stages_min else None,
        respiratory_rate=args.respiratory_rate,
        disturbances=args.disturbances,
        raw={"tool": "log_sleep"},
    )
    local_start = to_local(ensure_utc(row.started_at), ctx.tz)
    local_end = to_local(ensure_utc(row.ended_at), ctx.tz)
    result: dict[str, Any] = {
        "sleep_id": row.id,
        "created": created,
        "night_of": local_end.date().isoformat(),
        "onset": local_start.strftime("%H:%M"),
        "wake": local_end.strftime("%H:%M"),
        "in_bed_min": rnd(row.in_bed_min, 0),
        "asleep_min": rnd(row.asleep_min, 0),
        "asleep_h": rnd((row.asleep_min or 0) / 60, 1) if row.asleep_min else None,
        "performance_pct": rnd(row.performance_pct, 0),
        "disturbances": row.disturbances,
    }
    profile = ctx.profile
    if profile is not None and profile.bed_time is not None:
        diff = clock_diff_min(local_start.time(), profile.bed_time)
        result["bedtime_target"] = profile.bed_time.strftime("%H:%M")
        result["onset_vs_bedtime_min"] = diff
    if profile is not None and profile.wake_time is not None:
        diff = clock_diff_min(local_end.time(), profile.wake_time)
        result["wake_target"] = profile.wake_time.strftime("%H:%M")
        result["wake_vs_anchor_min"] = diff
    log.info("sleep_logged", user_id=ctx.user_id, sleep_id=row.id, created=created)
    return ok(result)
