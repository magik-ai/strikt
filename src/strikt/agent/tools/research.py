"""Research tool: web_research via a separate LLM call with server-side web_search/web_fetch (PLAN §6.4).

Owner: research build agent. Every handler below is a stub the build stage replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult


async def web_research(ctx: ToolContext, args: schemas.WebResearchInput) -> ToolResult:
    raise NotImplementedError("implemented by build stage")
