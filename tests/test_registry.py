"""Tool definitions: sorted, strict, additionalProperties false recursively, complete."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from strikt.agent.tools import Registry, Tool, ToolContext, ToolResult, build_registry
from strikt.agent.tools.registry import strict_schema
from strikt.agent.tools.schemas import SCHEMAS, TOOL_NAMES

FORBIDDEN_KEYS = {
    "title",
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
}


def _walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for key, value in node.items():
            if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                for sub in value.values():
                    found.extend(_walk(sub))
            else:
                found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def test_every_plan_tool_is_registered(registry: Registry) -> None:
    assert registry.names() == sorted(TOOL_NAMES)
    assert set(SCHEMAS) == set(TOOL_NAMES)
    assert len(registry) == 27


def test_definitions_are_sorted_strict_and_closed(registry: Registry) -> None:
    defs = registry.definitions()
    assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
    for definition in defs:
        assert definition["strict"] is True
        assert definition["description"].strip()
        schema = definition["input_schema"]
        assert schema["type"] == "object"
        for node in _walk(schema):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False, (definition["name"], node)
                assert "required" in node
            assert not (FORBIDDEN_KEYS & node.keys()), (definition["name"], node)
            if "format" in node:
                assert node["format"] in {"date-time", "date", "time", "uri", "email", "uuid"}


def test_definitions_are_byte_stable() -> None:
    a = json.dumps(build_registry().definitions(), sort_keys=False, ensure_ascii=False)
    b = json.dumps(build_registry().definitions(), sort_keys=False, ensure_ascii=False)
    assert a == b


def test_definition_copies_do_not_leak(registry: Registry) -> None:
    defs = registry.definitions()
    defs[0]["name"] = "hacked"
    assert registry.definitions()[0]["name"] != "hacked"


def test_strict_schema_strips_constraints_and_titles() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int = Field(ge=0, le=10, description="a number")

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = Field(min_length=1, max_length=5)
        items: list[Inner] = Field(min_length=2)
        maybe: Inner | None = None

    schema = strict_schema(Outer)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["name", "items"]
    assert "minLength" not in schema["properties"]["name"]
    assert "minItems" not in schema["properties"]["items"]
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False
    assert "minimum" not in inner["properties"]["value"]
    assert inner["properties"]["value"]["description"] == "a number"
    assert "title" not in schema


async def test_dispatch_validates_input_and_reports_stubs(
    registry: Registry, tool_ctx: ToolContext
) -> None:
    bad = await registry.dispatch(tool_ctx, "log_meal", {"items": "not a list"})
    assert bad.is_error and "invalid input for log_meal" in str(bad.content)
    unknown = await registry.dispatch(tool_ctx, "nope", {})
    assert unknown.is_error and "unknown tool" in str(unknown.content)
    stub = await registry.dispatch(tool_ctx, "undo_last", {})
    assert stub.is_error and "not available yet" in str(stub.content)
    extra = await registry.dispatch(tool_ctx, "undo_last", {"surprise": 1})
    assert extra.is_error and "extra" in str(extra.content).lower()


async def test_dispatch_runs_registered_handler(tool_ctx: ToolContext) -> None:
    class EchoInput(BaseModel):
        """Echo the text back."""

        model_config = ConfigDict(extra="forbid")
        text: str

    async def echo(ctx: ToolContext, args: EchoInput) -> ToolResult:
        return ToolResult(content=f"{ctx.user.id}:{args.text}")

    registry = Registry()
    registry.register(Tool.from_model("echo", EchoInput, echo))
    result = await registry.dispatch(tool_ctx, "echo", {"text": "hi"})
    assert not result.is_error and result.content == f"{tool_ctx.user.id}:hi"
    assert registry.definitions()[0]["description"] == "Echo the text back."


def test_tool_context_helpers(tool_ctx: ToolContext) -> None:
    assert tool_ctx.tz == "Asia/Dubai" and tool_ctx.lang == "ru"
    assert tool_ctx.local_date.isoformat() == "2026-09-03"
    assert tool_ctx.service("llm") is tool_ctx.services["llm"]
