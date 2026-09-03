"""Profile tools: update_profile, update_protocol, set_coaching_intensity, finish_onboarding, connect_integration, import_history (PLAN §6.4, §10).

Owner: onboarding build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def update_profile(ctx: ToolContext, args: schemas.UpdateProfileInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def update_protocol(ctx: ToolContext, args: schemas.UpdateProtocolInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def set_coaching_intensity(
    ctx: ToolContext, args: schemas.SetCoachingIntensityInput
) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def finish_onboarding(ctx: ToolContext, args: schemas.FinishOnboardingInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def connect_integration(
    ctx: ToolContext, args: schemas.ConnectIntegrationInput
) -> ToolResult:
    raise NotImplementedError("implemented by build stage")


async def import_history(ctx: ToolContext, args: schemas.ImportHistoryInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
