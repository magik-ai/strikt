"""The turn loop (PLAN §6.3): one user message in, one reply (and its side effects) out.

Order of operations in ``run_turn``:

1. persist the user turn (media blocks verbatim; stubbed to ``[image: <sha256>]`` at the end);
2. build the context (``agent/context.py``) — before the ladder is reset, so the block can say
   which proactive message the user is answering;
3. publish ``UserReplied`` and mark open proactive sends as answered;
4. call the model with tools until ``end_turn``: tool calls of one round are executed and their
   results returned in **one** user message (``is_error`` on failure); ``pause_turn`` is re-sent
   as is; ``max_tokens`` gets one continuation; a ``refusal`` becomes an honest one-liner;
   at most ``settings.max_tool_rounds`` rounds;
5. Reflexion verify (``agent/verify.py``) — only after a state-changing tool or a recalculation
   request, and only rewrites on a real mismatch;
6. persist the assistant turn (final text only — intermediate tool rounds are not history, the
   day state carries the ids), publish ``DayStateChanged`` when a state-changing tool ran,
   refresh the Today card when a refresher is wired, pick the keyboard, commit.

Tool calls in one round run sequentially by default: the handlers share the turn's
``AsyncSession`` and SQLAlchemy forbids concurrent operations on one session. Set
``TurnDeps.parallel_tools=True`` to run them with ``asyncio.gather`` when the registry's handlers
open their own sessions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from strikt.agent.client import LLMError, LLMResult, ToolUse
from strikt.agent.context import ContextBundle, build_context, user_blocks
from strikt.agent.tools.registry import ToolContext
from strikt.agent.usage import LLMUsage
from strikt.agent.verify import STATE_CHANGING_TOOLS, should_verify, verify_reply
from strikt.core.clock import ensure_utc, to_local
from strikt.core.types import Button, Outgoing
from strikt.db import repo
from strikt.db.models import MealSlot, TurnRole
from strikt.events import DayStateChanged, UserReplied
from strikt.memory.daystate import DayStateBuilder
from strikt.telegram.copy import resolve_lang, t
from strikt.telegram.keyboards import day_actions, meal_actions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.agent.client import LLMClient
    from strikt.agent.tools.registry import Registry
    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.core.types import Attachment, DayState, Incoming
    from strikt.db.models import Profile, Protocol as ProtocolRow, User
    from strikt.events import EventBus
    from strikt.proactive.types import StateProvider

log = structlog.get_logger(__name__)

EVENING_HOUR = 20
MEAL_TOOLS: frozenset[str] = frozenset({"log_meal", "update_meal", "delete_meal", "undo_last"})
CONTINUE_TEXT = "Continue exactly where you stopped. Do not repeat what you already wrote."

# Code-rendered fallbacks the model cannot write for us (wish: move to telegram/copy.py).
_COPY: dict[str, dict[str, str]] = {
    "en": {
        "refused": "I can't help with that one. Send the next meal or ask something else.",
        "too_many_steps": "Too many steps for one message; what I logged so far stands. Send the rest again in smaller pieces.",
    },
    "ru": {
        "refused": "С этим не помогу. Пришли следующий приём еды или спроси о другом.",
        "too_many_steps": "Слишком много шагов на одно сообщение; записанное сохранено. Пришли остальное ещё раз, частями.",
    },
}


def _copy(lang: str | None, key: str) -> str:
    return _COPY[resolve_lang(lang)][key]


class CardRefresher(Protocol):
    """Implemented by ``telegram.daycard.DayCard``; optional in ``TurnDeps``."""

    async def refresh(self, session: AsyncSession, user: User, state: DayState) -> int | None: ...


@dataclass
class TurnDeps:
    """Everything one turn needs. Built per message by the Telegram handler."""

    session: AsyncSession
    user: User
    llm: LLMClient
    registry: Registry
    clock: Clock
    settings: Settings
    bus: EventBus | None = None
    state_provider: StateProvider | None = None
    services: dict[str, Any] = field(default_factory=dict)
    card: CardRefresher | None = None
    commit: bool = True
    parallel_tools: bool = False

    def provider(self) -> StateProvider:
        if self.state_provider is None:
            self.state_provider = DayStateBuilder(self.clock, self.settings)
        return self.state_provider


@dataclass
class TurnResult:
    outgoings: list[Outgoing]
    tools_used: list[str]
    state_changed: bool
    turn_id: int
    usage: LLMUsage = field(default_factory=LLMUsage)
    assistant_turn_id: int | None = None
    cost_usd: float = 0.0
    text: str = ""
    rounds: int = 0
    refused: bool = False
    error: str | None = None
    budget: dict[str, int] = field(default_factory=dict)


# ------------------------------------------------------------------------------------ helpers

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADER = re.compile(r"(?m)^#{1,6}\s+")


def to_telegram_html(text: str) -> str:
    """Escape for Telegram HTML and keep the two markdown marks the model tends to use."""
    escaped = html.escape(text.strip(), quote=False)
    escaped = _HEADER.sub("", escaped)
    escaped = _BOLD.sub(r"<b>\1</b>", escaped)
    return _CODE.sub(r"<code>\1</code>", escaped)


def stub_media_blocks(
    blocks: Sequence[dict[str, Any]], attachments: Sequence[Attachment]
) -> list[dict[str, Any]]:
    """Replace image/document blocks with ``[image: <sha256>]`` text (the DB never keeps bytes)."""
    by_data = {a.bytes_b64: a for a in attachments if a.bytes_b64}
    out: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind not in {"image", "document"}:
            out.append(dict(block))
            continue
        data = str((block.get("source") or {}).get("data") or "")
        att = by_data.get(data)
        digest = att.sha256 if att is not None and att.sha256 else _sha256_b64(data)
        out.append({"type": "text", "text": f"[{kind}: {digest}]"})
    return out


def _sha256_b64(data: str) -> str:
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raw = data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _tool_result_block(
    use: ToolUse, content: str | list[dict[str, Any]], is_error: bool
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": use.id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


async def execute_tools(
    deps: TurnDeps, ctx: ToolContext, uses: Sequence[ToolUse]
) -> list[dict[str, Any]]:
    """Run every tool call of one round; results in call order, failures as ``is_error``."""

    async def one(use: ToolUse) -> dict[str, Any]:
        try:
            result = await deps.registry.dispatch(ctx, use.name, use.input)
        except Exception as exc:  # dispatch never raises, but a handler bug must not kill the turn
            log.exception("tool_dispatch_crashed", tool=use.name)
            return _tool_result_block(use, f"{use.name} failed: {type(exc).__name__}: {exc}", True)
        log.info("tool_ran", tool=use.name, is_error=result.is_error, user_id=ctx.user_id)
        return _tool_result_block(use, result.content, result.is_error)

    if deps.parallel_tools and len(uses) > 1:
        return list(await asyncio.gather(*(one(use) for use in uses)))
    return [await one(use) for use in uses]


async def _keyboard(
    deps: TurnDeps, user: User, tools_used: Sequence[str], state: DayState | None
) -> list[list[Button]] | None:
    """Slot picker + undo after an unslotted meal, meal actions after a meal change, day actions
    in the evening while the day is open, else nothing."""
    lang = user.language
    if MEAL_TOOLS.intersection(tools_used):
        meal = await repo.last_meal(deps.session, user.id)
        if meal is not None:
            ask_slot = "log_meal" in tools_used and meal.slot == MealSlot.unknown
            return meal_actions(meal.id, lang, ask_slot=ask_slot)
    local_now = to_local(deps.clock.now(), user.timezone or "UTC")
    if local_now.hour >= EVENING_HOUR and (state is None or not state.closed):
        return day_actions(lang)
    return None


# --------------------------------------------------------------------------------- the loop


@dataclass
class _LoopOutcome:
    text: str
    tools_used: list[str]
    usage: LLMUsage
    cost_usd: float
    rounds: int
    refused: bool = False


async def _model_loop(
    deps: TurnDeps, user: User, bundle: ContextBundle, ctx: ToolContext
) -> _LoopOutcome:
    messages: list[dict[str, Any]] = [dict(m) for m in bundle.messages]
    tools_used: list[str] = []
    usage = LLMUsage()
    cost = 0.0
    rounds = 0
    calls = 0
    continued = False
    text_parts: list[str] = []
    max_rounds = int(getattr(deps.settings, "max_tool_rounds", 12))
    max_calls = max_rounds * 2 + 2  # pause_turn / continuation re-sends never loop forever

    while True:
        calls += 1
        if calls > max_calls:
            log.warning("turn_call_cap", user_id=user.id, calls=calls)
            text_parts.append(_copy(user.language, "too_many_steps"))
            break
        result: LLMResult = await deps.llm.message(
            purpose="turn",
            system=bundle.system,
            messages=messages,
            tools=bundle.tools,
            user_id=user.id,
        )
        usage = usage + result.usage
        cost += result.cost_usd

        if result.refused:
            log.warning(
                "turn_refused",
                user_id=user.id,
                explanation=result.refusal and result.refusal.explanation,
            )
            return _LoopOutcome(
                _copy(user.language, "refused"), tools_used, usage, cost, rounds, True
            )

        if result.paused:
            messages.append(result.assistant_message())
            continue

        uses = result.tool_uses
        if result.wants_tools and uses:
            if result.text:
                text_parts.append(result.text)
            if rounds >= max_rounds:
                log.warning("turn_round_cap", user_id=user.id, rounds=rounds)
                text_parts.append(_copy(user.language, "too_many_steps"))
                break
            rounds += 1
            messages.append(result.assistant_message())
            results = await execute_tools(deps, ctx, uses)
            tools_used += [use.name for use in uses]
            messages.append({"role": "user", "content": results})
            continue

        if result.truncated and not continued:
            continued = True
            text_parts.append(result.text)
            messages.append(
                result.assistant_message()
                if result.content
                else {
                    "role": "assistant",
                    "content": [{"type": "text", "text": result.text or "…"}],
                }
            )
            messages.append({"role": "user", "content": [{"type": "text", "text": CONTINUE_TEXT}]})
            continue

        text_parts.append(result.text)
        break

    text = "\n".join(part.strip() for part in text_parts if part and part.strip()).strip()
    return _LoopOutcome(text, tools_used, usage, cost, rounds)


# ------------------------------------------------------------------------------------ run


async def run_turn(deps: TurnDeps, incoming: Incoming) -> TurnResult:
    """Process one inbound message end to end (see the module docstring)."""
    session = deps.session
    user = deps.user
    now = ensure_utc(deps.clock.now())
    tz = user.timezone or "UTC"
    today = to_local(now, tz).date()
    lang = user.language

    profile: Profile | None = await repo.get_profile(session, user.id)
    protocol: ProtocolRow | None = await repo.get_active_protocol(session, user.id)

    own_blocks = user_blocks(incoming)
    user_turn = await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=own_blocks,
        now=now,
        telegram_message_id=incoming.message_id,
    )
    await repo.touch_last_seen(session, user.id, now)

    bundle = await build_context(
        session,
        user,
        incoming,
        clock=deps.clock,
        settings=deps.settings,
        state_provider=deps.provider(),
        registry=deps.registry,
        profile=profile,
        protocol=protocol,
        exclude_turn_id=user_turn.id,
    )

    if deps.bus is not None:
        await deps.bus.publish(UserReplied(user_id=user.id, occurred_at=now, turn_id=user_turn.id))
    await repo.mark_responded(session, user.id, at=now, turn_id=user_turn.id)

    services = {
        "llm": deps.llm,
        "bus": deps.bus,
        "state_provider": deps.provider(),
        **deps.services,
    }
    ctx = ToolContext(
        session=session,
        user=user,
        profile=profile,
        protocol=protocol,
        clock=deps.clock,
        settings=deps.settings,
        services=services,
        incoming=incoming,
    )

    error: str | None = None
    try:
        outcome = await _model_loop(deps, user, bundle, ctx)
    except LLMError as exc:
        log.error("turn_llm_failed", user_id=user.id, error=str(exc), retryable=exc.retryable)
        error = str(exc)
        outcome = _LoopOutcome(t(lang, "err.llm_down"), [], LLMUsage(), 0.0, 0)

    tools_used = outcome.tools_used
    state_changed = bool(STATE_CHANGING_TOOLS.intersection(tools_used))
    state: DayState | None = None
    if state_changed or should_verify(tools_used, incoming):
        try:
            state = await deps.provider().day_state(session, user, today)
        except Exception as exc:
            log.warning("turn_day_state_failed", user_id=user.id, error=repr(exc))

    text = outcome.text
    if error is None and not outcome.refused:
        text = await verify_reply(deps, user, text, tools_used, incoming, state=state)
    if not text.strip():
        text = t(lang, "err.unknown")

    assistant_turn_id: int | None = None
    if error is None:
        assistant_turn = await repo.add_turn(
            session,
            user.id,
            role=TurnRole.assistant,
            content=[{"type": "text", "text": text}],
            now=ensure_utc(deps.clock.now()),
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            cache_read_tokens=outcome.usage.cache_read_tokens,
            cache_write_tokens=outcome.usage.cache_write_tokens,
        )
        assistant_turn_id = assistant_turn.id

    user_turn.content = stub_media_blocks(own_blocks, incoming.attachments)
    await session.flush()

    if state_changed:
        if deps.bus is not None:
            await deps.bus.publish(
                DayStateChanged(
                    user_id=user.id,
                    occurred_at=ensure_utc(deps.clock.now()),
                    date=today,
                    reason=",".join(sorted(set(tools_used) & STATE_CHANGING_TOOLS)),
                )
            )
        if deps.card is not None and state is not None:
            try:
                await deps.card.refresh(session, user, state)
            except Exception as exc:
                log.warning("daycard_refresh_failed", user_id=user.id, error=repr(exc))

    keyboard = await _keyboard(deps, user, tools_used, state)
    outgoing = Outgoing(text=to_telegram_html(text), keyboard=keyboard, reply_to=None)

    if deps.commit:
        await session.commit()

    log.info(
        "turn_done",
        user_id=user.id,
        tools=tools_used,
        rounds=outcome.rounds,
        state_changed=state_changed,
        refused=outcome.refused,
        error=error,
        input_tokens=outcome.usage.total_input,
        output_tokens=outcome.usage.output_tokens,
        cost_usd=round(outcome.cost_usd, 6),
    )
    return TurnResult(
        outgoings=[outgoing],
        tools_used=tools_used,
        state_changed=state_changed,
        turn_id=user_turn.id,
        usage=outcome.usage,
        assistant_turn_id=assistant_turn_id,
        cost_usd=outcome.cost_usd,
        text=text,
        rounds=outcome.rounds,
        refused=outcome.refused,
        error=error,
        budget=bundle.budget,
    )
