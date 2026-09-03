"""Training tools: log_workout, log_sleep (PLAN §6.4, brief §3.4-3.5).

Owner: training/integrations build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def log_workout(ctx: ToolContext, args: schemas.LogWorkoutInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def log_sleep(ctx: ToolContext, args: schemas.LogSleepInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
