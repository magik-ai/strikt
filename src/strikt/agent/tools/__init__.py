"""Tool package: ``build_registry()`` wires every schema in PLAN §6.4 to its handler."""

from __future__ import annotations

from strikt.agent.tools import schemas
from strikt.agent.tools.registry import (
    MAX_STRICT_TOOLS,
    Handler,
    Registry,
    Tool,
    ToolContext,
    ToolResult,
)

__all__ = [
    "MAX_STRICT_TOOLS",
    "NON_STRICT_TOOLS",
    "Handler",
    "Registry",
    "Tool",
    "ToolContext",
    "ToolResult",
    "build_registry",
]

#: The coach has more tools than Anthropic allows in strict mode, so the flag goes to the
#: schemas where it earns its place: nested objects, enums, many fields. These take one scalar
#: argument at most, and a malformed call to one of them comes back through
#: ``Registry.dispatch`` as a tool error the model can fix on the next round.
NON_STRICT_TOOLS: frozenset[str] = frozenset(
    {
        "cancel_reminder",
        "delete_meal",
        "finish_onboarding",
        "get_day_state",
        "import_history",
        "render_day_card",
        "retire_note",
        "undo_last",
    }
)


def _handlers() -> dict[str, Handler]:
    from strikt.agent.tools import body, food, memory, profile, research, state, training

    return {
        "search_food": food.search_food,
        "log_meal": food.log_meal,
        "update_meal": food.update_meal,
        "delete_meal": food.delete_meal,
        "undo_last": food.undo_last,
        "log_workout": training.log_workout,
        "log_sleep": training.log_sleep,
        "log_measurement": body.log_measurement,
        "ingest_lab_report": body.ingest_lab_report,
        "get_day_state": state.get_day_state,
        "get_history": memory.get_history,
        "search_history": memory.search_history,
        "update_profile": profile.update_profile,
        "update_protocol": profile.update_protocol,
        "set_reminder": memory.set_reminder,
        "cancel_reminder": memory.cancel_reminder,
        "write_note": memory.write_note,
        "retire_note": memory.retire_note,
        "set_day_flag": state.set_day_flag,
        "set_day_plan": state.set_day_plan,
        "close_day": state.close_day,
        "web_research": research.web_research,
        "render_day_card": state.render_day_card,
        "connect_integration": profile.connect_integration,
        "set_coaching_intensity": profile.set_coaching_intensity,
        "finish_onboarding": profile.finish_onboarding,
        "import_history": profile.import_history,
    }


def build_registry() -> Registry:
    """A registry with every tool from PLAN §6.4 (checked against ``schemas.TOOL_NAMES``)."""
    handlers = _handlers()
    missing = set(schemas.TOOL_NAMES) - set(handlers)
    extra = set(handlers) - set(schemas.TOOL_NAMES)
    if missing or extra:
        raise RuntimeError(f"tool wiring mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    strict_names = [name for name in schemas.TOOL_NAMES if name not in NON_STRICT_TOOLS]
    if len(strict_names) > MAX_STRICT_TOOLS:
        raise RuntimeError(
            f"{len(strict_names)} strict tools, Anthropic accepts {MAX_STRICT_TOOLS}: "
            f"add the simplest ones to NON_STRICT_TOOLS"
        )
    registry = Registry()
    for name in schemas.TOOL_NAMES:
        registry.register(
            Tool.from_model(
                name,
                schemas.SCHEMAS[name],
                handlers[name],
                strict=name not in NON_STRICT_TOOLS,
            )
        )
    return registry
