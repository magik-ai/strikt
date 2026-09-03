"""Tool registry: pydantic input models → strict Anthropic tool definitions → dispatch.

``Registry.definitions()`` is byte-stable (sorted by name, deterministic schema) so the tool
block never invalidates the prompt cache (see shared/prompt-caching.md).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.core.types import Incoming
    from strikt.db.models import Profile, Protocol, User

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ToolResult:
    """What a handler returns; ``content`` becomes the ``tool_result`` block content."""

    content: str | list[dict[str, Any]]
    is_error: bool = False

    @staticmethod
    def error(message: str) -> ToolResult:
        return ToolResult(content=message, is_error=True)


@dataclass
class ToolContext:
    """Everything a handler may need. ``services`` is a bag for llm/messenger/bus/cipher/etc."""

    session: AsyncSession
    user: User
    profile: Profile | None
    protocol: Protocol | None
    clock: Clock
    settings: Settings
    services: dict[str, Any] = field(default_factory=dict)
    incoming: Incoming | None = None

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def tz(self) -> str:
        return self.user.timezone or "UTC"

    @property
    def lang(self) -> str:
        return self.user.language or "en"

    @property
    def local_date(self) -> date:
        from strikt.core.clock import local_date

        return local_date(self.clock, self.tz)

    def service(self, name: str) -> Any:
        try:
            return self.services[name]
        except KeyError as exc:
            raise KeyError(f"service '{name}' is not wired into ToolContext") from exc


# The second argument is the validated instance of ``Tool.input_model``. It is typed ``Any`` (not
# ``BaseModel``) so handlers can declare their concrete input class without a variance error.
Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler

    @classmethod
    def from_model(
        cls,
        name: str,
        input_model: type[BaseModel],
        handler: Handler,
        description: str | None = None,
    ) -> Tool:
        """Use the input model's docstring as the tool description."""
        doc = description or inspect.cleandoc(input_model.__doc__ or "")
        if not doc:
            raise ValueError(f"tool {name}: input model {input_model.__name__} has no docstring")
        return cls(name=name, description=doc, input_model=input_model, handler=handler)

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": strict_schema(self.input_model),
            "strict": True,
        }


# ------------------------------------------------------------------------------ strict schema

_DROP_KEYS = frozenset(
    {
        "title",
        "examples",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)
_ALLOWED_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
_CHILD_MAPS = ("properties", "$defs", "definitions")


def _strictify(node: Any) -> Any:
    if isinstance(node, list):
        return [_strictify(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key == "format" and value not in _ALLOWED_FORMATS:
            continue
        if key == "minItems" and isinstance(value, int) and value > 1:
            continue
        if key in _CHILD_MAPS and isinstance(value, dict):
            out[key] = {name: _strictify(sub) for name, sub in value.items()}
            continue
        out[key] = _strictify(value)
    is_object = out.get("type") == "object" or "properties" in out
    if is_object:
        out["additionalProperties"] = False
        props = out.get("properties")
        if isinstance(props, dict):
            required = [name for name in out.get("required", []) if name in props]
            out["required"] = required
        else:
            out.setdefault("properties", {})
            out.setdefault("required", [])
    return out


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema for strict tool use: ``additionalProperties: false`` everywhere, unsupported
    keywords removed, property order preserved (pydantic emits fields in declaration order)."""
    schema: dict[str, Any] = model.model_json_schema(mode="validation")
    result = _strictify(schema)
    return dict(result)


# ------------------------------------------------------------------------------------ registry


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        self._definitions = None
        return tool

    def register_many(self, tools: Mapping[str, tuple[type[BaseModel], Handler]]) -> None:
        for name, (model, handler) in tools.items():
            self.register(Tool.from_model(name, model, handler))

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def definitions(self) -> list[dict[str, Any]]:
        """Anthropic tool dicts sorted by name; cached because the bytes must never change."""
        if self._definitions is None:
            self._definitions = [self._tools[name].definition() for name in self.names()]
        return [dict(d) for d in self._definitions]

    async def dispatch(
        self, ctx: ToolContext, name: str, tool_input: Mapping[str, Any]
    ) -> ToolResult:
        """Validate ``tool_input`` and run the handler. Never raises: failures are ``is_error``."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult.error(f"unknown tool: {name}")
        try:
            args = tool.input_model.model_validate(dict(tool_input))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            return ToolResult.error(f"invalid input for {name}: {problems}")
        try:
            return await tool.handler(ctx, args)
        except NotImplementedError as exc:
            log.warning("tool_not_implemented", tool=name)
            return ToolResult.error(f"{name} is not available yet: {exc}")
        except Exception as exc:
            log.exception("tool_failed", tool=name)
            return ToolResult.error(f"{name} failed: {type(exc).__name__}: {exc}")
