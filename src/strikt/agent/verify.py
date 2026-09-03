"""Reflexion check before sending (PLAN §6.3; research/01 D1–D3: reflect on failure only).

The evaluator is deterministic and cheap: after a tool that changes today's numbers ran — or
when the user asked to recalculate — the day state is rebuilt from the database and every day
total the draft claims (``agent/numbers.py``) is compared with it (within 2 % or 5 kcal; 2 % or
1 g for grams). Only on a mismatch is the model called once more, with ``prompts/verify.md``,
the draft and the authoritative numbers, to rewrite the reply. No self-critique on success, no
second retry: one bounded trial protects latency (the paper's Ω = 1–3, τ-bench's lesson).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from strikt.agent.context import load_prompt
from strikt.agent.numbers import ClaimedTotals, extract_numbers
from strikt.core.clock import local_date
from strikt.memory.daystate import DayStateBuilder, render_context
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.agent.loop import TurnDeps
    from strikt.core.types import DayState, Incoming
    from strikt.db.models import User

log = structlog.get_logger(__name__)

#: Tools after which the draft's day totals are checked against the database.
VERIFY_TOOLS: frozenset[str] = frozenset(
    {"log_meal", "update_meal", "delete_meal", "undo_last", "get_day_state", "import_history"}
)
#: Tools that change today's numbers or state (``DayStateChanged`` is published after them).
STATE_CHANGING_TOOLS: frozenset[str] = frozenset(
    {
        "log_meal",
        "update_meal",
        "delete_meal",
        "undo_last",
        "log_workout",
        "log_sleep",
        "log_measurement",
        "set_day_flag",
        "set_day_plan",
        "close_day",
        "update_protocol",
        "import_history",
    }
)

KCAL_TOLERANCE_ABS = 5.0
GRAMS_TOLERANCE_ABS = 1.0
TOLERANCE_REL = 0.02

_RECALC = re.compile(
    r"(?i)(recalc|recalculate|recount|re-count|re-derive|redo the (?:total|math|numbers)"
    r"|doesn'?t add up|does not add up|check the (?:total|math|sum)|wrong total|total is wrong"
    r"|пересчит|перепровер|пересчёт|пересчет|не сходится|не сходятся|проверь (?:итог|сумму|цифры)"
    r"|ошиб\w* (?:в|с) (?:итог|сумм|цифр|подсч)|неправильн\w* (?:итог|сумм|подсч))"
)

_FALLBACK_LINE: dict[str, str] = {
    "en": "Database total: {kcal} kcal | P {p} | C {c} | F {f} | fiber {fib}.",
    "ru": "Итого по базе: {kcal} ккал | Б {p} | У {c} | Ж {f} | клетчатка {fib}.",
}

_FIELD_LABEL: dict[str, str] = {
    "kcal": "kcal",
    "protein_g": "protein g",
    "carbs_g": "carbs g",
    "fat_g": "fat g",
    "fiber_g": "fiber g",
}


@dataclass(frozen=True)
class Mismatch:
    field: str
    claimed: float
    actual: float

    def line(self) -> str:
        return f"{_FIELD_LABEL[self.field]}: your text says {self.claimed:g}, the log says {self.actual:g}"


def wants_recalculation(text: str | None) -> bool:
    """The user challenged a total or asked to recount (ru/en)."""
    return bool(text and _RECALC.search(text))


def should_verify(tools_used: list[str], incoming: Incoming) -> bool:
    return bool(VERIFY_TOOLS.intersection(tools_used)) or wants_recalculation(incoming.text)


def compare_totals(claimed: ClaimedTotals, state: DayState) -> list[Mismatch]:
    """Every claimed field outside tolerance (2 %, or 5 kcal / 1 g absolute, whichever is larger)."""
    totals = state.totals.macros
    actual = {
        "kcal": totals.kcal,
        "protein_g": totals.protein_g,
        "carbs_g": totals.carbs_g,
        "fat_g": totals.fat_g,
        "fiber_g": totals.fiber_g,
    }
    out: list[Mismatch] = []
    for name, value in claimed.items():
        truth = actual[name]
        absolute = KCAL_TOLERANCE_ABS if name == "kcal" else GRAMS_TOLERANCE_ABS
        tolerance = max(absolute, abs(truth) * TOLERANCE_REL)
        if abs(value - truth) > tolerance:
            out.append(Mismatch(field=name, claimed=value, actual=truth))
    return out


def _n(value: float) -> str:
    return str(round(value))


def fallback_line(state: DayState, lang: str | None) -> str:
    """Appended when the rewrite itself fails: the database numbers, plainly."""
    m = state.totals.macros
    return _FALLBACK_LINE[resolve_lang(lang)].format(
        kcal=_n(m.kcal), p=_n(m.protein_g), c=_n(m.carbs_g), f=_n(m.fat_g), fib=_n(m.fiber_g)
    )


async def verify_reply(
    deps: TurnDeps,
    user: User,
    draft_text: str,
    tools_used: list[str],
    incoming: Incoming,
    *,
    state: DayState | None = None,
) -> str:
    """Return the text to send: the draft when it matches the database, else a rewrite."""
    if not draft_text.strip() or not should_verify(tools_used, incoming):
        return draft_text
    claimed = extract_numbers(draft_text)
    if not claimed.any:
        return draft_text

    if state is None:
        provider = deps.state_provider or DayStateBuilder(deps.clock, deps.settings)
        state = await provider.day_state(
            deps.session, user, local_date(deps.clock, user.timezone or "UTC")
        )
    mismatches = compare_totals(claimed, state)
    if not mismatches:
        log.debug("verify_ok", user_id=user.id, claimed=claimed.items())
        return draft_text

    lang = resolve_lang(user.language)
    recalculation = wants_recalculation(incoming.text)
    log.info(
        "verify_mismatch",
        user_id=user.id,
        mismatches=[m.line() for m in mismatches],
        recalculation=recalculation,
    )
    prompt = (
        "<draft>\n"
        f"{draft_text}\n"
        "</draft>\n"
        "<day_state>\n"
        f"{render_context(state, lang, tz=user.timezone or 'UTC')}\n"
        "</day_state>\n"
        "<mismatches>\n" + "\n".join(f"- {m.line()}" for m in mismatches) + "\n</mismatches>\n"
        f"recalculation_requested: {'yes' if recalculation else 'no'}\n"
        f"reply_language: {'Russian' if lang == 'ru' else 'English'}"
    )
    try:
        result = await deps.llm.message(
            purpose="verify",
            system=load_prompt("verify"),
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            user_id=user.id,
            cache_tail=False,
        )
    except Exception as exc:
        log.warning("verify_call_failed", user_id=user.id, error=repr(exc))
        return f"{draft_text}\n\n{fallback_line(state, lang)}"
    rewritten = result.text.strip()
    if result.refused or not rewritten:
        log.warning("verify_no_rewrite", user_id=user.id, refused=result.refused)
        return f"{draft_text}\n\n{fallback_line(state, lang)}"
    still_wrong = compare_totals(extract_numbers(rewritten), state)
    if still_wrong:
        # One trial only (bounded Reflexion). Make the truth visible rather than loop.
        log.warning(
            "verify_still_wrong", user_id=user.id, mismatches=[m.line() for m in still_wrong]
        )
        return f"{rewritten}\n\n{fallback_line(state, lang)}"
    return rewritten
