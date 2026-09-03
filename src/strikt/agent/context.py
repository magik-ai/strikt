"""Context assembly (PLAN §6.2): the infinite-memory contract, ordered stable → volatile.

Request shape (research/02 §7, shared/prompt-caching.md):

- ``tools``     registry definitions, sorted, strict, byte-stable (cached with system[0]);
- ``system[0]`` the static coach prompt, ``cache_control {ephemeral, ttl 1h}``;
- ``system[1]`` the profile block — profile + active protocol + active notes rendered
  deterministically (sorted keys, no timestamps, no ids that change between turns) plus the
  onboarding prompt and checklist while onboarding is unfinished — ``cache_control {ephemeral}``;
- ``messages``  the stored turns verbatim — at least ``settings.context_max_turns`` of them,
  trimmed with hysteresis (``HISTORY_SLACK``): the window's first row moves only every
  ``HISTORY_SLACK`` new rows, so between trims the history is a byte-stable prefix and the
  explicit ``cache_control`` on its last block is actually *read* (a plain sliding window would
  change ``messages[0]`` every turn and write the whole history at 1.25× each time) — under
  ``settings.context_max_tokens`` (≈ 4 chars/token for ASCII, 2.5 for Cyrillic); then the
  current user message:
  a ``<context>`` text block (local now, today's day state, yesterday's close line, the week
  summary, retrieved history when the text asks about the past, pending reminders, the open
  proactive send being answered), then images/documents, then the user's text.

The LLM wrapper adds the top-level automatic ``cache_control`` for the conversation tail
(``LLM.message(cache_tail=True)``). Nothing in ``system`` depends on the clock: the same inputs
render the same bytes (tested), so the 1h and 5m entries are actually read. Caveat: the API
invalidates the messages tier whenever an image enters or leaves the prompt, so the history
entry pays off for text-only bursts; a photo turn re-writes it once.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from strikt.core.clock import coaching_today, ensure_utc, local_day_bounds, to_local
from strikt.db import repo
from strikt.db.models import (
    MeasurementType,
    ProactiveSend,
    Profile,
    Protocol,
    SummaryKind,
    User,
    UserStatus,
)
from strikt.memory.daystate import DayStateBuilder, render_context, yesterday_close_line
from strikt.memory.notes import active_notes, render_notes_block
from strikt.memory.periods import find_period
from strikt.memory.retrieval import render_rows, search_history
from strikt.onboarding.checklist import Facts, facts_for, render_state
from strikt.telegram.copy import resolve_lang, weekday_name

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.agent.tools.registry import Registry
    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.core.types import Attachment, DayState, Incoming
    from strikt.proactive.types import StateProvider

log = structlog.get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CHARS_PER_TOKEN = 4
#: Cyrillic (and other non-ASCII) tokenises much denser than English prose.
CHARS_PER_TOKEN_NON_ASCII = 2.5
#: History trim hysteresis in rows: the window start moves only every this many new rows.
HISTORY_SLACK = 16
CONTEXT_HISTORY_TOKENS = 1500
CONTEXT_SUMMARY_CHARS = 700
CONTEXT_DAY_SUMMARY_CHARS = 280
CONTEXT_DAY_SUMMARIES = 3
MAX_REMINDERS = 8
TURN_BUDGET_WARN_TOKENS = 60_000

CACHE_1H: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
CACHE_5M: dict[str, str] = {"type": "ephemeral"}

#: Profile columns never rendered: volatile timestamps, the FK, the relationship.
_PROFILE_SKIP = frozenset({"user_id", "updated_at", "onboarding_done_at", "user"})

_PAST_QUESTION = re.compile(
    r"(?i)(\bwhat did i\b|\bhow (?:much|many|often|did)\b|\blast (?:time|week|month|year|\w+day)\b"
    r"|\byesterday\b|\bago\b|\btrend\b|\bhistory\b|\bearlier\b|\bbefore\b|\bprevious\b"
    r"|\bthis (?:week|month)\b|\baverage\b|\bstreak\b|\bwhen did\b|\bremember\b"
    r"|что я ел|что ела|что было|сколько (?:раз|было|я)|когда (?:я|мы|был|ел|последн)"
    r"|вчера|позавчера|назад|на прошл|в прошл|за (?:неделю|месяц|год)|тренд|динамик|истори"
    r"|раньше|последний раз|в среднем|средн|помнишь|напомни что|мы (?:решили|договор|обсужд))"
)


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """A prompt file from ``agent/prompts`` (cached: the bytes must never vary per request)."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def estimate_tokens(value: Any) -> int:
    """≈ 4 chars per token for ASCII and 2.5 for non-ASCII (Cyrillic) over the JSON rendering.

    A budget, not a count: the Sonnet 5 tokenizer is not public. English prose lands near
    4 chars/token; Russian near 2.5–3, so a flat 4 would undercount a Russian history by ~40 %.
    """
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ascii_chars = len(text) - non_ascii
    return math.ceil(ascii_chars / CHARS_PER_TOKEN + non_ascii / CHARS_PER_TOKEN_NON_ASCII)


def history_window(total_rows: int, max_turns: int, slack: int = HISTORY_SLACK) -> int:
    """How many of the newest rows to keep so the window's first row is stable across turns.

    Under ``max_turns`` everything is kept. Above it the cut point advances in steps of
    ``slack`` rows, so the window holds between ``max_turns`` and ``max_turns + slack - 1`` rows
    and its prefix (hence the cache entry) survives ``slack`` consecutive turns.
    """
    if total_rows <= max_turns:
        return total_rows
    start = ((total_rows - max_turns) // max(1, slack)) * max(1, slack)
    return total_rows - start


def looks_like_past_question(text: str | None, *, now_local: datetime, lang: str | None) -> bool:
    """True when the message asks about the past (a period expression or a history keyword)."""
    if not text:
        return False
    if find_period(text, now_local=now_local, lang=resolve_lang(lang)) is not None:
        return True
    return bool(_PAST_QUESTION.search(text))


@dataclass
class ContextBundle:
    """What one ``messages.create`` call needs, plus the estimated token budget per section."""

    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    budget: dict[str, int] = field(default_factory=dict)
    onboarding: bool = False
    answered_send: ProactiveSend | None = None

    @property
    def total_tokens(self) -> int:
        return int(self.budget.get("total", 0))


# ------------------------------------------------------------------------------ profile block


def _scalar(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, datetime):
        return ensure_utc(value).strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list | tuple):
        return ", ".join(_scalar(v) for v in value) or "-"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).split())


def render_profile_block(
    user: User,
    profile: Profile | None,
    protocol: Protocol | None,
    notes_block: str = "",
) -> str:
    """Deterministic text: sorted profile keys, the active protocol, the notes. No clock."""
    lines: list[str] = [
        "<profile>",
        f"language: {user.language or 'en'}",
        f"timezone: {user.timezone or 'UTC'}",
    ]
    if profile is None:
        lines.append("(no profile yet — onboarding has not started)")
    else:
        mapper = profile.__table__.columns
        rendered: dict[str, str] = {}
        for column in mapper:
            key = column.name
            if key in _PROFILE_SKIP:
                continue
            value = getattr(profile, key, None)
            if value is None or value in ([], ""):
                continue
            rendered[key] = _scalar(value)
        rendered["onboarding_done"] = "yes" if profile.onboarding_done_at is not None else "no"
        lines += [f"{key}: {rendered[key]}" for key in sorted(rendered)]
    lines.append("</profile>")

    if protocol is None:
        lines.append("<protocol>none yet — propose one in onboarding step 8</protocol>")
    else:
        lines += [
            f"<protocol version={protocol.version}>",
            (
                f"kcal {protocol.kcal:g} | P {protocol.protein_g:g} g | C {protocol.carbs_g:g} g"
                f" | F {protocol.fat_g:g} g | fiber {protocol.fiber_g:g} g"
            ),
        ]
        if protocol.rationale:
            lines.append(f"rationale: {' '.join(protocol.rationale.split())}")
        lines.append("</protocol>")

    lines.append("<notes>")
    lines.append(notes_block if notes_block else "(none yet)")
    lines.append("</notes>")
    return "\n".join(lines)


def is_onboarding(user: User, profile: Profile | None) -> bool:
    if user.status == UserStatus.onboarding:
        return True
    return profile is None or profile.onboarding_done_at is None


def render_onboarding_checklist(
    profile: Profile | None, lang: str | None = None, facts: Facts | None = None
) -> str:
    """Which of the ten steps are done and what each unfinished one is still missing.

    The text comes from ``strikt.onboarding.checklist``, which reads the profile columns rather
    than trusting ``onboarding_step`` alone, so a step the user answered out of order counts.
    """
    return render_state(profile, lang, facts)


def _merge_same_role(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The API wants user/assistant alternation; consecutive same-role turns are concatenated."""
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"] = [*merged[-1]["content"], *message["content"]]
        else:
            merged.append({"role": message["role"], "content": list(message["content"])})
    return merged


def _stored_content(content: Any) -> list[dict[str, Any]]:
    blocks = [dict(b) for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return [b for b in blocks if b.get("type") != "text" or str(b.get("text", "")).strip()]


async def history_messages(
    session: AsyncSession,
    user: User,
    settings: Settings,
    *,
    exclude_turn_id: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Last N turns within the token budget, oldest trimmed first, as alternating messages.

    ``exclude_turn_id`` is the current user turn (already persisted by the loop): it is sent as
    the current message, not as history.
    """
    max_turns = int(getattr(settings, "context_max_turns", 30))
    max_tokens = int(getattr(settings, "context_max_tokens", 40_000))
    rows = await repo.last_n_turns(session, user.id, max_turns + HISTORY_SLACK + 1)
    excluded = sum(1 for row in rows if row.id == exclude_turn_id)
    total = await repo.count_turns(session, user.id) - excluded
    rows = [row for row in rows if row.id != exclude_turn_id]
    rows = rows[-history_window(total, max_turns) :] if rows else rows
    kept: list[dict[str, Any]] = []
    used = 0
    for row in reversed(rows):  # newest first, stop when the budget is full
        content = _stored_content(row.content)
        if not content:
            continue
        cost = estimate_tokens(content)
        if kept and used + cost > max_tokens:
            break
        kept.append({"role": row.role.value, "content": content})
        used += cost
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return _merge_same_role(kept), used


# ------------------------------------------------------------------------------ user message


def attachment_blocks(attachments: list[Attachment]) -> tuple[list[dict[str, Any]], list[str]]:
    """Image/document blocks (before the text, per the vision guide) and text derived from
    voice transcripts, links and unreadable files."""
    media: list[dict[str, Any]] = []
    texts: list[str] = []
    image_no = 0
    for att in attachments:
        if att.kind == "image" and att.bytes_b64:
            image_no += 1
            if len([a for a in attachments if a.kind == "image" and a.bytes_b64]) > 1:
                media.append({"type": "text", "text": f"Image {image_no}:"})
            media.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime or "image/jpeg",
                        "data": att.bytes_b64,
                    },
                }
            )
        elif att.kind == "document" and att.bytes_b64:
            media.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime or "application/pdf",
                        "data": att.bytes_b64,
                    },
                    **({"title": att.filename} if att.filename else {}),
                }
            )
        elif att.kind == "voice":
            texts.append(
                f"[voice transcript] {att.text.strip()}" if att.text else "[voice: no transcript]"
            )
        elif att.kind == "link":
            label = att.file_id or att.filename or "link"
            body = f"\n{att.text.strip()}" if att.text else ""
            texts.append(f"[link] {label}{body}")
        elif att.text:
            texts.append(f"[{att.kind}] {att.text.strip()}")
        else:
            texts.append(f"[{att.kind}: could not be read]")
    return media, texts


def user_blocks(incoming: Incoming) -> list[dict[str, Any]]:
    """The user's own content (what gets persisted): media first, then text."""
    media, texts = attachment_blocks(incoming.attachments)
    blocks = list(media)
    if incoming.forwarded_from:
        texts.insert(0, f"[forwarded from {incoming.forwarded_from}]")
    if incoming.text and incoming.text.strip():
        texts.append(incoming.text.strip())
    if texts:
        blocks.append({"type": "text", "text": "\n".join(texts)})
    if not blocks:
        blocks.append({"type": "text", "text": "[empty message]"})
    return blocks


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def open_proactive_send(
    session: AsyncSession, user: User, *, now: datetime
) -> ProactiveSend | None:
    """The latest unanswered proactive send of the local day (the message the user answers)."""
    day_start, _ = local_day_bounds(to_local(now, user.timezone).date(), user.timezone)
    sends = await repo.list_sends_since(session, user.id, since=day_start)
    open_sends = [s for s in sends if s.responded_at is None]
    return open_sends[-1] if open_sends else None


async def render_context_block(
    session: AsyncSession,
    user: User,
    incoming: Incoming,
    *,
    clock: Clock,
    state: DayState,
    answered_send: ProactiveSend | None,
) -> str:
    """The volatile ``<context>`` block that opens the current user message."""
    tz = user.timezone or "UTC"
    lang = resolve_lang(user.language)
    now_local = to_local(clock.now(), tz)
    parts: list[str] = [
        "<context>",
        f"now: {now_local:%Y-%m-%d %H:%M} ({weekday_name(lang, now_local.weekday())}) {tz}",
        "<day>",
        render_context(state, lang, tz=tz),
        "</day>",
    ]
    try:
        yesterday = await yesterday_close_line(session, user, state.date)
    except Exception as exc:
        log.warning("context_yesterday_failed", user_id=user.id, error=repr(exc))
        yesterday = None
    if yesterday:
        parts.append(f"<yesterday>{yesterday}</yesterday>")

    try:
        weeks = await repo.list_recent_summaries(session, user.id, SummaryKind.week, limit=1)
        days = await repo.list_recent_summaries(
            session, user.id, SummaryKind.day, limit=CONTEXT_DAY_SUMMARIES
        )
    except Exception as exc:
        log.warning("context_summaries_failed", user_id=user.id, error=repr(exc))
        weeks, days = [], []
    if weeks or days:
        parts.append("<summaries>")
        parts.extend(
            f"week {week.period_start.isoformat()}…{week.period_end.isoformat()}: "
            f"{_short(week.text, CONTEXT_SUMMARY_CHARS)}"
            for week in weeks
        )
        for day in sorted(days, key=lambda s: s.period_start):
            if day.period_start == state.date:
                continue
            parts.append(
                f"day {day.period_start.isoformat()}: {_short(day.text, CONTEXT_DAY_SUMMARY_CHARS)}"
            )
        parts.append("</summaries>")

    if looks_like_past_question(incoming.text, now_local=now_local, lang=lang):
        try:
            rows = await search_history(session, user, incoming.text or "", now_local=now_local)
            rendered = render_rows(rows, lang, tz=tz, max_tokens=CONTEXT_HISTORY_TOKENS)
        except Exception as exc:
            log.warning("context_history_failed", user_id=user.id, error=repr(exc))
            rendered = ""
        parts.append("<history>")
        parts.append(rendered or "(nothing matched — use get_history / search_history)")
        parts.append("</history>")

    try:
        reminders = await repo.pending_reminders(session, user.id)
    except Exception as exc:
        log.warning("context_reminders_failed", user_id=user.id, error=repr(exc))
        reminders = []
    if reminders:
        parts.append("<reminders>")
        parts.extend(
            f"- #{reminder.id} {to_local(reminder.due_at, tz):%Y-%m-%d %H:%M} "
            f"{reminder.kind}: {_short(reminder.text, 120)}"
            for reminder in reminders[:MAX_REMINDERS]
        )
        parts.append("</reminders>")

    if answered_send is not None:
        parts.append(
            f"<proactive>the user is answering your message of "
            f"{to_local(answered_send.sent_at, tz):%H:%M} (trigger {answered_send.trigger}, "
            f"ladder step {answered_send.step} of 4): {_short(answered_send.text, 300)}. "
            "Their reply resets the ladder; log the reason if they explain a gap.</proactive>"
        )
    parts.append("</context>")
    return "\n".join(parts)


# --------------------------------------------------------------------------------- build


async def build_context(
    session: AsyncSession,
    user: User,
    incoming: Incoming,
    *,
    clock: Clock,
    settings: Settings,
    state_provider: StateProvider | None,
    registry: Registry,
    profile: Profile | None = None,
    protocol: Protocol | None = None,
    state: DayState | None = None,
    exclude_turn_id: int | None = None,
) -> ContextBundle:
    """Assemble system, messages and tools for one turn (see the module docstring).

    ``exclude_turn_id``: the already-persisted row of the current user message (see
    ``history_messages``).
    """
    tz = user.timezone or "UTC"
    now = ensure_utc(clock.now())
    if profile is None:
        profile = await repo.get_profile(session, user.id)
    # the coaching day, not the calendar date: before the rollover the food went to yesterday
    today = coaching_today(
        clock, tz, profile.bed_time if profile else None, profile.wake_time if profile else None
    )
    if protocol is None:
        protocol = await repo.get_active_protocol(session, user.id)
    if state is None:
        provider: StateProvider = state_provider or DayStateBuilder(clock, settings)
        state = await provider.day_state(session, user, today)

    notes = await active_notes(session, user, now=now)
    profile_text = render_profile_block(user, profile, protocol, render_notes_block(notes))
    onboarding = is_onboarding(user, profile)
    if onboarding:
        weighed = await repo.latest_by_type(session, user.id, MeasurementType.weight)
        facts = facts_for(user, protocol, has_weight=weighed is not None)
        profile_text = "\n\n".join(
            [
                profile_text,
                load_prompt("onboarding"),
                render_onboarding_checklist(profile, user.language, facts),
            ]
        )

    system: list[dict[str, Any]] = [
        {"type": "text", "text": load_prompt("coach"), "cache_control": dict(CACHE_1H)},
        {"type": "text", "text": profile_text, "cache_control": dict(CACHE_5M)},
    ]

    history, history_tokens = await history_messages(
        session, user, settings, exclude_turn_id=exclude_turn_id
    )
    if history:
        last_blocks = history[-1]["content"]
        last_blocks[-1] = {**last_blocks[-1], "cache_control": dict(CACHE_5M)}

    answered_send = await open_proactive_send(session, user, now=now)
    context_text = await render_context_block(
        session, user, incoming, clock=clock, state=state, answered_send=answered_send
    )
    own_blocks = user_blocks(incoming)
    current = {
        "role": "user",
        "content": [{"type": "text", "text": context_text}, *own_blocks],
    }
    messages = _merge_same_role([*history, current])
    tools = registry.definitions()

    budget = {
        "system_static": estimate_tokens(system[0]["text"]),
        "system_profile": estimate_tokens(profile_text),
        "tools": estimate_tokens(tools),
        "history": history_tokens,
        "context": estimate_tokens(context_text),
        "user": estimate_tokens([b for b in own_blocks if b.get("type") == "text"]),
        "images": sum(1 for b in own_blocks if b.get("type") in {"image", "document"}),
    }
    budget["total"] = sum(v for k, v in budget.items() if k != "images")
    if budget["total"] > TURN_BUDGET_WARN_TOKENS:
        log.warning("context_over_budget", user_id=user.id, **budget)
    else:
        log.debug("context_built", user_id=user.id, **budget)
    return ContextBundle(
        system=system,
        messages=messages,
        tools=tools,
        budget=budget,
        onboarding=onboarding,
        answered_send=answered_send,
    )
