"""Memory tools: get_history, search_history, write_note, retire_note, set_reminder, cancel_reminder (PLAN §6.2, §6.4).

Owner: memory build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def get_history(ctx: ToolContext, args: schemas.GetHistoryInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def search_history(ctx: ToolContext, args: schemas.SearchHistoryInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def write_note(ctx: ToolContext, args: schemas.WriteNoteInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def retire_note(ctx: ToolContext, args: schemas.RetireNoteInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def set_reminder(ctx: ToolContext, args: schemas.SetReminderInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def cancel_reminder(ctx: ToolContext, args: schemas.CancelReminderInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
