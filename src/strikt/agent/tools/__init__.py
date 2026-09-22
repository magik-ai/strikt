"""Tool package: ``build_registry()`` wires every schema in PLAN §6.4 to its handler.

**Two tiers.** Twenty-nine tool schemas are 9.8k tokens on every request, and tool-selection
accuracy falls away once a model is choosing among thirty of them (Anthropic, *Effective context
engineering for AI agents*; the tool-search docs put the drop at 30-50 tools). Most of that
catalogue is cold: ``update_profile`` alone is 1.6k tokens and is used about once a week.

So a turn starts with ``CORE_TOOL_NAMES`` - the daily loop (food, the day, training, history,
research) plus ``load_tools`` - and the model calls ``load_tools`` when the user asks for
something the core set cannot do. The turn loop then re-sends the same turn with the full
catalogue. Two stable tool sets means two cache entries, not a new prefix per turn: the core set
is byte-identical on every ordinary turn, so the 1h cache entry is actually read.
"""

from __future__ import annotations

from strikt.agent.tools import schemas
from strikt.agent.tools.registry import Handler, Registry, Tool, ToolContext, ToolResult

__all__ = [
    "CORE_TOOL_NAMES",
    "ONBOARDING_TOOL_NAMES",
    "Handler",
    "Registry",
    "Tool",
    "ToolContext",
    "ToolResult",
    "build_registry",
    "tool_names_for",
]

#: The tools of the daily loop, loaded on every turn. Everything else arrives through
#: ``load_tools``. Ordered as the day is, not by module.
CORE_TOOL_NAMES: tuple[str, ...] = (
    "search_food",
    "log_meal",
    "update_meal",
    "delete_meal",
    "log_workout",
    "log_sleep",
    "get_day_state",
    "get_history",
    "web_research",
    "write_note",
    "close_day",
    "load_tools",
)
#: While onboarding is unfinished the checklist tools are the daily loop, so they join the core
#: set instead of costing a ``load_tools`` round trip on every answer.
ONBOARDING_TOOL_NAMES: tuple[str, ...] = (
    "update_profile",
    "update_protocol",
    "finish_onboarding",
    "import_history",
    # The checklist asks for these by name (prompts/onboarding.md): the weight baseline, the
    # integrations and the optional keys. Without them the interview would need a load_tools
    # round trip on almost every answer, and finish_onboarding would fail on a missing weight.
    "log_measurement",
    "connect_integration",
    "request_key",
)


def tool_names_for(*, onboarding_done: bool, full: bool = False) -> tuple[str, ...]:
    """The tool set for one request: the core loop, the core loop while onboarding, or all."""
    if full:
        return schemas.TOOL_NAMES
    names = CORE_TOOL_NAMES if onboarding_done else CORE_TOOL_NAMES + ONBOARDING_TOOL_NAMES
    return tuple(sorted(names))


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
        "request_key": profile.request_key,
        "set_coaching_intensity": profile.set_coaching_intensity,
        "finish_onboarding": profile.finish_onboarding,
        "import_history": profile.import_history,
        "load_tools": state.load_tools,
    }


def build_registry() -> Registry:
    """A registry with every tool from PLAN §6.4 (checked against ``schemas.TOOL_NAMES``)."""
    handlers = _handlers()
    missing = set(schemas.TOOL_NAMES) - set(handlers)
    extra = set(handlers) - set(schemas.TOOL_NAMES)
    if missing or extra:
        raise RuntimeError(f"tool wiring mismatch: missing={sorted(missing)} extra={sorted(extra)}")
    registry = Registry()
    for name in schemas.TOOL_NAMES:
        registry.register(Tool.from_model(name, schemas.SCHEMAS[name], handlers[name]))
    return registry
