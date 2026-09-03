"""Body tools: log_measurement, ingest_lab_report (PLAN §6.4, brief §3.2 weight/waist rules).

Owner: body/labs build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def log_measurement(ctx: ToolContext, args: schemas.LogMeasurementInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def ingest_lab_report(ctx: ToolContext, args: schemas.IngestLabReportInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
