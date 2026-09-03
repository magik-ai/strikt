"""Food tools: search_food, log_meal, update_meal, delete_meal, undo_last (PLAN §5, §6.4).

Owner: nutrition build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def search_food(ctx: ToolContext, args: schemas.SearchFoodInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def log_meal(ctx: ToolContext, args: schemas.LogMealInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def update_meal(ctx: ToolContext, args: schemas.UpdateMealInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def delete_meal(ctx: ToolContext, args: schemas.DeleteMealInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def undo_last(ctx: ToolContext, args: schemas.UndoLastInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
