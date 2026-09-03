"""The model-backed proactive decider (PLAN §6.5, brief §7): writes the nudge, or stays silent.

``LLMDecider`` implements ``proactive.types.Decider``. The prompt is compact and cheap (effort
``low`` via the ``proactive`` purpose): ``prompts/proactive.md`` as the cached system block, then
one user message with the fire facts, the profile block, today's state, the last three day
summaries, relevant notes, the ladder state and response rate, and what was already sent today.
The answer is structured output ``{send, text, reason}``. Brief §7.4 is enforced in code as
well: emoji stripped, at most four lines / 350 characters (the Sunday ``weekly_review`` is the
brief's own exception — "the week in five lines" plus a pattern and an instruction — and gets
seven lines / 700 characters), never a step below the ladder's. A response cut off by the output
cap is reported as ``truncated`` rather than mistaken for bad JSON.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.context import CACHE_5M, load_prompt, render_profile_block
from strikt.core.clock import SystemClock, ensure_utc, local_day_bounds, to_local
from strikt.db import repo
from strikt.db.models import SummaryKind
from strikt.memory.daystate import render_context
from strikt.memory.notes import relevant_notes, render_notes_block
from strikt.proactive.types import LadderState, ProactiveDecision, TriggerFire
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.agent.client import LLMClient
    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.core.types import DayState
    from strikt.db.models import User

log = structlog.get_logger(__name__)

MAX_LINES = 4
MAX_CHARS = 350
#: Per-trigger (lines, chars) caps where the brief asks for more than the generic 2–4 lines.
TRIGGER_CAPS: dict[str, tuple[int, int]] = {"weekly_review": (7, 700)}
MIN_STEP = 1
MAX_STEP = 4
SUMMARY_CHARS = 300
NOTE_LIMIT = 6

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "send": {"type": "boolean"},
        "text": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["send", "text", "reason"],
    "additionalProperties": False,
}

_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # pictographs, emoticons, transport, symbols, extended-A
    "☀-➿"  # misc symbols, dingbats
    "⬀-⯿"  # arrows and misc symbols
    "⌀-⏿"  # misc technical (watches, hourglasses)
    "\U0001f1e6-\U0001f1ff"  # regional indicators
    "️‍⃣"  # variation selector, ZWJ, keycap
    "]+"
)
_MULTISPACE = re.compile(r"[ \t]{2,}")


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text)


def caps_for(trigger: str | None) -> tuple[int, int]:
    """``(max_lines, max_chars)`` for a trigger (``TRIGGER_CAPS`` or the §7.4 default)."""
    return TRIGGER_CAPS.get(trigger or "", (MAX_LINES, MAX_CHARS))


def sanitize_proactive_text(text: str, trigger: str | None = None) -> str:
    """Brief §7.4 in code: no emoji, ≤ 4 lines, ≤ 350 characters (per-trigger caps for the
    weekly review), no blank lines, trimmed."""
    max_lines, max_chars = caps_for(trigger)
    lines = [_MULTISPACE.sub(" ", strip_emoji(line)).strip() for line in text.splitlines()]
    lines = [line for line in lines if line][:max_lines]
    out = "\n".join(lines)
    if len(out) > max_chars:
        cut = out[:max_chars]
        # end on a sentence or, failing that, a word boundary
        for sep in (". ", "? ", "! ", "\n", " "):
            idx = cut.rfind(sep)
            if idx >= max_chars // 2:
                cut = cut[: idx + (1 if sep.strip() else 0)]
                break
        out = cut.rstrip()
    return out


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class LLMDecider:
    """Writes the proactive message with the model; every decision is fresh, never a template."""

    def __init__(self, llm: LLMClient, settings: Settings, *, clock: Clock | None = None) -> None:
        self._llm = llm
        self._settings = settings
        self._clock: Clock = clock or SystemClock()

    async def decide(
        self,
        session: AsyncSession,
        user: User,
        fire: TriggerFire,
        ladder: LadderState,
        state: DayState | None,
    ) -> ProactiveDecision:
        step = max(MIN_STEP, min(ladder.step, MAX_STEP))
        prompt = await self._render_prompt(session, user, fire, ladder, state, step)
        result = await self._llm.message(
            purpose="proactive",
            system=[
                {"type": "text", "text": load_prompt("proactive"), "cache_control": dict(CACHE_5M)}
            ],
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            output_schema=DECISION_SCHEMA,
            user_id=user.id,
            cache_tail=False,
        )
        if result.refused:
            log.warning("proactive_refused", user_id=user.id, trigger=fire.name)
            return ProactiveDecision(send=False, step=step, reason="refusal")
        if result.truncated:
            # thinking counts against max_tokens: a cut JSON is a cap problem, not bad output
            log.warning(
                "proactive_truncated",
                user_id=user.id,
                trigger=fire.name,
                stop_reason=result.stop_reason,
                max_tokens=self._settings.max_tokens_proactive,
            )
            return ProactiveDecision(send=False, step=step, reason="truncated")
        try:
            payload = result.json()
        except (ValueError, TypeError) as exc:
            log.warning(
                "proactive_invalid_json", user_id=user.id, trigger=fire.name, error=repr(exc)
            )
            return ProactiveDecision(send=False, step=step, reason="invalid_output")
        if not isinstance(payload, dict):
            return ProactiveDecision(send=False, step=step, reason="invalid_output")

        send = bool(payload.get("send"))
        text = sanitize_proactive_text(str(payload.get("text") or ""), fire.name)
        reason = _short(str(payload.get("reason") or ""), 200)
        if send and not text:
            log.info("proactive_empty_text", user_id=user.id, trigger=fire.name)
            return ProactiveDecision(send=False, step=step, reason=reason or "empty_text")
        log.info(
            "proactive_decided",
            user_id=user.id,
            trigger=fire.name,
            send=send,
            step=step,
            chars=len(text),
            reason=reason,
        )
        return ProactiveDecision(send=send, text=text if send else "", step=step, reason=reason)

    async def _render_prompt(
        self,
        session: AsyncSession,
        user: User,
        fire: TriggerFire,
        ladder: LadderState,
        state: DayState | None,
        step: int,
    ) -> str:
        tz = user.timezone or "UTC"
        lang = resolve_lang(user.language)
        now = ensure_utc(self._clock.now())
        local_now = fire.local_now if fire.local_now.tzinfo else to_local(now, tz)
        profile = await repo.get_profile(session, user.id)
        protocol = await repo.get_active_protocol(session, user.id)

        parts: list[str] = [
            "<fire>",
            f"trigger: {fire.name} (class {fire.klass}) window {fire.window_key}",
            f"local now: {local_now:%Y-%m-%d %H:%M} ({local_now:%a}) {tz}",
            f"facts: {_json(fire.facts)}",
        ]
        if fire.payload:
            parts.append(f"payload: {_short(_json(fire.payload), 600)}")
        parts.append("</fire>")
        parts += [
            "<ladder>",
            f"step: {step} of {MAX_STEP} (write at this step's voice)",
            f"sends today: {ladder.sends_today} of {ladder.cap_today}",
            f"intensity: {ladder.intensity}",
            "response rate for this trigger: "
            + (f"{ladder.response_rate:.0%}" if ladder.response_rate is not None else "unknown"),
            f"clean streak days: {ladder.clean_streak_days}",
            "</ladder>",
        ]
        parts.append(render_profile_block(user, profile, protocol, ""))
        if state is not None:
            parts += ["<day>", render_context(state, lang, tz=tz), "</day>"]
        else:
            parts.append("<day>unavailable</day>")

        summaries = await repo.list_recent_summaries(session, user.id, SummaryKind.day, limit=3)
        if summaries:
            parts.append("<recent_days>")
            for s in sorted(summaries, key=lambda row: row.period_start):
                parts.append(f"- {s.period_start.isoformat()}: {_short(s.text, SUMMARY_CHARS)}")
            parts.append("</recent_days>")

        query = " ".join([fire.name.replace("_", " "), _json(fire.facts)])
        notes = await relevant_notes(session, user, query, now=now, limit=NOTE_LIMIT)
        if notes:
            parts += ["<notes>", render_notes_block(notes), "</notes>"]

        sent_today = await self._sent_today(session, user.id, local_now, tz)
        if sent_today:
            parts.append("<sent_today>")
            parts += sent_today
            parts.append("</sent_today>")

        parts.append(
            f"Decide. Language: {'Russian' if lang == 'ru' else 'English'}. "
            'Return JSON {"send": bool, "text": str, "reason": str}.'
        )
        return "\n".join(parts)

    async def _sent_today(
        self, session: AsyncSession, user_id: int, local_now: datetime, tz: str
    ) -> list[str]:
        day_start, _ = local_day_bounds(local_now.date(), tz)
        try:
            sends = await repo.list_sends_since(session, user_id, since=day_start)
        except Exception as exc:
            log.warning("proactive_sends_failed", user_id=user_id, error=repr(exc))
            return []
        lines: list[str] = []
        for send in sends[-5:]:
            answered = "answered" if send.responded_at is not None else "unanswered"
            lines.append(
                f"- {to_local(send.sent_at, tz):%H:%M} {send.trigger} step {send.step} ({answered}):"
                f" {_short(send.text, 160)}"
            )
        return lines
