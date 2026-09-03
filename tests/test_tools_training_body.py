"""Training and body tools: log_workout comparisons, log_sleep, log_measurement, labs."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.tools import ToolContext, body, schemas, training
from strikt.core.types import LabMarker
from strikt.db import repo
from strikt.db.models import DataSource, DayFlag
from tests.test_memory_helpers import TODAY, at_local, seed_measurement, seed_workout

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def parsed(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(str(result.content))
    return data


# ------------------------------------------------------------------------------- log_workout


async def test_log_workout_compares_with_last_and_30d_average(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    for offset, (dur, kcal, hr) in enumerate([(45, 406, 130), (50, 420, 128)], start=1):
        await seed_workout(
            session,
            tool_ctx.user_id,
            TODAY - timedelta(days=offset * 3),
            "18:00",
            sport="run",
            duration_min=dur,
            kcal=kcal,
            avg_hr=hr,
        )
    args = schemas.LogWorkoutInput(
        sport="Run",
        started_at=datetime(2026, 9, 3, 7, 0),  # naive → Dubai local
        duration_min=94,
        kcal=361,
        avg_hr=104,
        strain=8.1,
        zones_min=schemas.ZoneMinutes(z0=55, z1=20, z2=15, z3=4),
        source="screenshot",
    )
    result = parsed(await training.log_workout(tool_ctx, args))
    w = result["workout"]
    assert w["sport"] == "run" and w["duration_min"] == 94 and w["start"] == "07:00"
    assert w["density_kcal_per_min"] == 3.84
    assert w["zones"]["low_pct"] == 80 and w["zones"]["pct"]["z0"] == 59
    assert result["last_same_sport"]["duration_min"] == 45
    assert result["vs_last"]["duration_min"] == 49 and result["vs_last"]["avg_hr"] == -26
    assert result["avg_30d_same_sport"]["sessions"] == 3
    assert result["avg_30d_same_sport"]["kcal"] == 396
    assert result["created"] is True


async def test_log_workout_dedupes_by_external_id_and_by_start_window(
    tool_ctx: ToolContext,
) -> None:
    args = schemas.LogWorkoutInput(
        sport="strength",
        started_at=datetime(2026, 9, 2, 18, 30, tzinfo=UTC),
        ended_at=datetime(2026, 9, 2, 19, 32, tzinfo=UTC),
        strain=9.4,
        source="whoop",
        external_id="w-1",
    )
    first = parsed(await training.log_workout(tool_ctx, args))
    second = parsed(await training.log_workout(tool_ctx, args))
    assert first["created"] and not second["created"]
    assert second["workout"]["id"] == first["workout"]["id"]
    assert first["workout"]["duration_min"] == 62
    assert "low strain" in first["note"]
    # a screenshot of the same session, five minutes off and without an id
    again = parsed(
        await training.log_workout(
            tool_ctx,
            schemas.LogWorkoutInput(
                sport="Strength",
                started_at=datetime(2026, 9, 2, 18, 35, tzinfo=UTC),
                duration_min=60,
                source="screenshot",
            ),
        )
    )
    assert again["created"] is False and again["workout"]["id"] == first["workout"]["id"]


async def test_log_workout_bedtime_link(tool_ctx: ToolContext) -> None:
    # bed_time 00:30 local; a run ending 23:44 local (19:44 UTC) is flagged
    result = parsed(
        await training.log_workout(
            tool_ctx,
            schemas.LogWorkoutInput(
                sport="run",
                started_at=datetime(2026, 9, 2, 19, 0, tzinfo=UTC),
                ended_at=datetime(2026, 9, 2, 19, 44, tzinfo=UTC),
                source="manual",
            ),
        )
    )
    assert "23:44" in result["bedtime_link"] and "46 min before" in result["bedtime_link"]


async def test_log_workout_rejects_nonpositive_duration(tool_ctx: ToolContext) -> None:
    result = await training.log_workout(
        tool_ctx,
        schemas.LogWorkoutInput(sport="run", started_at=NOW, duration_min=0, source="manual"),
    )
    assert result.is_error


# --------------------------------------------------------------------------------- log_sleep


async def test_log_sleep_onset_and_wake_versus_targets(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    args = schemas.LogSleepInput(
        started_at=datetime(2026, 9, 3, 1, 20),  # naive local; bedtime is 00:30 → +50
        ended_at=datetime(2026, 9, 3, 8, 50),  # wake anchor 08:00 → +50
        performance_pct=71,
        stages_min=schemas.SleepStages(light=200, deep=80, rem=90, awake=30),
        source="whoop",
        external_id="s-1",
    )
    result = parsed(await training.log_sleep(tool_ctx, args))
    assert result["onset"] == "01:20" and result["wake"] == "08:50"
    assert result["onset_vs_bedtime_min"] == 50 and result["wake_vs_anchor_min"] == 50
    assert result["asleep_min"] == 370 and result["in_bed_min"] == 450
    assert result["night_of"] == "2026-09-03"
    rows = await repo.list_sleep_range(
        session, tool_ctx.user_id, NOW - timedelta(days=1), NOW + timedelta(days=1)
    )
    assert len(rows) == 1 and rows[0].source == DataSource.whoop
    bad = await training.log_sleep(
        tool_ctx, schemas.LogSleepInput(started_at=NOW, ended_at=NOW - timedelta(hours=1))
    )
    assert bad.is_error


# --------------------------------------------------------------------------- log_measurement


async def test_log_measurement_weight_trend_water_note_and_cadence(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    for days_ago, value in ((10, 105.0), (9, 104.8), (3, 104.2), (2, 104.0)):
        await seed_measurement(
            session, tool_ctx.user_id, TODAY - timedelta(days=days_ago), "07:30", value=value
        )
    await repo.set_day_flag(
        session, tool_ctx.user_id, TODAY - timedelta(days=1), DayFlag.salty, True, now=NOW
    )
    result = parsed(
        await body.log_measurement(
            tool_ctx, schemas.LogMeasurementInput(type="weight", value=104.9, unit="kg")
        )
    )
    assert result["previous"]["value"] == 104.0 and result["previous"]["days_ago"] == 2
    assert result["delta_vs_previous"] == 0.9
    assert result["trend"]["avg_7d"] == 104.4 and result["trend"]["avg_prev_7d"] == 104.9
    assert result["trend"]["delta_7d"] == -0.5
    assert result["cadence_days"] == 7 and result["next_due"] == "2026-09-10"
    notes = " ".join(result["notes"])
    assert "water" in notes and "weekly, not daily" in notes


async def test_log_measurement_waist_kpi_progress(tool_ctx: ToolContext) -> None:
    result = parsed(
        await body.log_measurement(
            tool_ctx,
            schemas.LogMeasurementInput(
                type="waist", value=103, unit="cm", measured_at=at_local(TODAY, "07:00")
            ),
        )
    )
    assert result["kpi"] == {"unit": "cm", "good": 94, "to_good": 9, "excellent": 90}
    assert result["cadence_days"] == 14
    assert any("navel" in n for n in result["notes"])
    assert "previous" not in result


async def test_log_measurement_rejects_future_and_nonpositive(tool_ctx: ToolContext) -> None:
    future = await body.log_measurement(
        tool_ctx,
        schemas.LogMeasurementInput(
            type="weight", value=100, unit="kg", measured_at=NOW + timedelta(days=1)
        ),
    )
    assert future.is_error
    zero = await body.log_measurement(
        tool_ctx, schemas.LogMeasurementInput(type="weight", value=0, unit="kg")
    )
    assert zero.is_error


# --------------------------------------------------------------------------- ingest_lab_report


async def test_ingest_lab_report_stores_rows_and_flags_out_of_range(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    args = schemas.IngestLabReportInput(
        taken_at=date(2026, 6, 2),
        markers=[
            LabMarker(marker="LDL", value=3.9, unit="mmol/L", ref_low=0, ref_high=3.0),
            LabMarker(marker="HDL", value=1.2, unit="mmol/L", ref_low=1.0, ref_high=2.0),
            LabMarker(marker="Ferritin", value=15, unit="ng/mL", ref_low=30, ref_high=400),
            LabMarker(marker="ALT", value=52, unit="U/L", flag="high"),
        ],
        source="Medcare",
    )
    result = parsed(await body.ingest_lab_report(tool_ctx, args))
    assert result["stored"] == 4 and result["skipped_duplicates"] == 0
    flagged = {row["marker"]: row["flag"] for row in result["out_of_range"]}
    assert flagged == {"LDL": "high", "Ferritin": "low", "ALT": "high"}
    rows = await repo.list_labs(session, tool_ctx.user_id)
    assert len(rows) == 4 and rows[0].source == "Medcare"
    again = parsed(await body.ingest_lab_report(tool_ctx, args))
    assert again["stored"] == 0 and again["skipped_duplicates"] == 4
    empty = await body.ingest_lab_report(
        tool_ctx, schemas.IngestLabReportInput(taken_at=date(2026, 6, 2), markers=[])
    )
    assert empty.is_error
