"""Day-state tools: get_day_state, set_day_flag, set_day_plan, close_day, render_day_card (PLAN §6.4, §7).

Owner: day-state/memory build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def get_day_state(ctx: ToolContext, args: schemas.GetDayStateInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def set_day_flag(ctx: ToolContext, args: schemas.SetDayFlagInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def set_day_plan(ctx: ToolContext, args: schemas.SetDayPlanInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def close_day(ctx: ToolContext, args: schemas.CloseDayInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def render_day_card(ctx: ToolContext, args: schemas.RenderDayCardInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
