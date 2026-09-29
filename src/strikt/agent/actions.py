"""What the tools of one turn actually did, kept in the history for the next turns.

The loop stores only the reply text of a turn; the tool calls and their results are not
history (PLAN §6.3). That left the coach blind to its own writes: two turns later it did not
know which item ids it had created, guessed one and corrected a cheeseburger from two weeks
earlier, said "added" about an omelette it never logged, and could not answer "what exactly did
you log" without calling a tool. One compact line per state-changing call, stored as a second
block of the assistant turn, fixes that for the price of a few dozen tokens.

The block is marked ``internal``: ``agent/context.py`` sends it to the model as ordinary text
(the marker key is stripped), the user never sees it, and ``strip_actions`` removes one the model
might echo into a reply.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from strikt.agent.verify import STATE_CHANGING_TOOLS

#: Writes that change no day but are worth remembering turn to turn.
EXTRA_ACTION_TOOLS = frozenset({"save_my_food"})
ACTIONS_OPEN = "<actions>"
ACTIONS_CLOSE = "</actions>"
#: Key on the stored block; ``context._stored_content`` strips it before the API sees it.
INTERNAL_KEY = "internal"
MAX_LINE = 1500
MAX_ERROR = 200

_ACTIONS_RE = re.compile(r"\s*<actions>.*?(?:</actions>|$)", re.DOTALL)


def _n(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _macros(m: Mapping[str, Any] | None) -> str:
    if not m:
        return "?"
    text = f"{_n(m.get('kcal', '?'))} kcal P{_n(m.get('P', '?'))} C{_n(m.get('C', '?'))}"
    text += f" F{_n(m.get('F', '?'))} fib{_n(m.get('fiber', '?'))}"
    return text


def _item(item: Mapping[str, Any]) -> str:
    grams = f" {_n(item['grams'])} g" if item.get("grams") is not None else ""
    flags = f" [{','.join(str(f) for f in item['flags'])}]" if item.get("flags") else ""
    return f"{item.get('name')} (item {item.get('id')}){grams} {_macros(item)}{flags}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def action_line(name: str, content: Any, is_error: bool) -> str | None:
    """One line for a state-changing call, ``None`` for a read-only one."""
    if name not in STATE_CHANGING_TOOLS and name not in EXTRA_ACTION_TOOLS:
        return None
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if is_error:
        return f"{name} FAILED: {_clip(raw, MAX_ERROR)}"
    try:
        payload = json.loads(raw)
    except ValueError:
        return _clip(f"{name}: {raw}", MAX_LINE)
    if not isinstance(payload, dict):
        return _clip(f"{name}: {raw}", MAX_LINE)
    if name == "set_day_food":
        parts = [f"set_day_food {payload.get('date')}: {payload.get('change') or ''}".rstrip()]
        for key in ("added", "removed", "changed"):
            values = payload.get(key) or []
            if values:
                parts.append(f"{key}: " + "; ".join(str(v) for v in values))
        if payload.get("unchanged"):
            parts.append(f"{payload['unchanged']} unchanged")
        line = " | ".join(parts)
    elif name == "save_my_food":
        line = f"save_my_food #{payload.get('saved_food_id')}: {payload.get('text')}"
    elif name == "log_meal":
        items = "; ".join(_item(i) for i in payload.get("items") or [])
        line = (
            f"log_meal: meal #{payload.get('meal_id')} {payload.get('date')} "
            f"{payload.get('slot')}: {items} = {_macros(payload.get('meal_total'))}"
        )
    elif name == "update_meal" and isinstance(payload.get("item"), dict):
        line = (
            f"update_meal: meal #{payload.get('meal_id')} {_item(payload['item'])}"
            f" (was {_macros(payload.get('before'))})"
        )
    elif name == "update_meal":
        items = "; ".join(_item(i) for i in payload.get("items") or [])
        line = (
            f"update_meal: meal #{payload.get('meal_id')} {payload.get('date')} "
            f"{payload.get('slot')}: {items}"
        )
    elif name in {"delete_meal", "undo_last"}:
        meal_id = payload.get("deleted_meal_id") or payload.get("undone_meal_id")
        removed = ", ".join(str(r) for r in payload.get("removed") or [])
        line = (
            f"{name}: meal #{meal_id} removed ({removed}) {_macros(payload.get('removed_total'))}"
        )
    else:
        body = {k: v for k, v in payload.items() if k not in {"day", "numbers"}}
        line = f"{name}: {json.dumps(body, ensure_ascii=False, default=str)}"
    day = payload.get("day")
    if isinstance(day, dict) and isinstance(day.get("totals"), dict):
        line += f" | day now {_macros(day['totals'])}"
    return _clip(line, MAX_LINE)


def actions_block(lines: Sequence[str]) -> dict[str, Any] | None:
    """The stored block for a turn's writes, or ``None`` when the turn wrote nothing."""
    if not lines:
        return None
    text = "\n".join(
        [
            ACTIONS_OPEN,
            "what the tools actually wrote this turn (not shown to the user):",
            *lines,
            ACTIONS_CLOSE,
        ]
    )
    return {"type": "text", "text": text, INTERNAL_KEY: True}


def strip_actions(text: str) -> str:
    """Drop an ``<actions>`` block the model copied into its reply."""
    return _ACTIONS_RE.sub("", text).strip()
