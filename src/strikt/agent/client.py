"""LLM wrapper around ``AsyncAnthropic`` (PLAN §6.1) plus a scripted ``FakeLLM`` for tests.

Every call goes through ``LLM.message``: it sets adaptive thinking and the effort for the
purpose, forwards the caller's cache-controlled system blocks, adds top-level automatic caching
for the conversation tail, records token usage (with cost) and normalises the response into an
``LLMResult`` whose content blocks are plain dicts (ready to store and to send back).

Bring-your-own-key: one ``LLM`` per API key. ``LLMFactory.for_user`` hands out the client that
bills the right key for a user — the user's own key (``LLM_KEY_MODE=user``, the default; the
server key serves only admins) or the server key for everyone (``LLM_KEY_MODE=server``) — and
``None`` when the user has no key yet, so nothing is ever billed to the operator for a keyless
user. ``AnthropicKeyValidator`` is the one cheap call that checks a pasted key before it is
stored. ``FakeLLM`` / ``FakeLLMFactory`` / ``FakeKeyValidator`` are the test doubles.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import anthropic
import structlog
from anthropic import AsyncAnthropic

from strikt.agent.usage import LLMUsage, compute_cost, usage_from_message
from strikt.core.clock import Clock, SystemClock
from strikt.db.models import UsagePurpose

if TYPE_CHECKING:
    from anthropic.types import MessageParam, TextBlockParam, ToolChoiceParam, ToolUnionParam
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from strikt.config import Effort, Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import User

log = structlog.get_logger(__name__)

STOP_END_TURN = "end_turn"
STOP_TOOL_USE = "tool_use"
STOP_MAX_TOKENS = "max_tokens"
STOP_PAUSE_TURN = "pause_turn"
STOP_REFUSAL = "refusal"
STOP_CONTEXT_EXCEEDED = "model_context_window_exceeded"

#: Clients kept alive per process (one per distinct API key, least recently used evicted).
MAX_CLIENTS = 64
#: Wall-clock cap for the one call that checks a pasted key (no SDK retries on top).
KEY_CHECK_TIMEOUT_S = 10.0


class LLMError(RuntimeError):
    """The API call failed after the SDK's own retries. ``retryable`` hints the caller."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class LLMAuthError(LLMError):
    """Anthropic rejected the API key (401/403). Never retried: the user must send a new key."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, retryable=False, status=status)


class LLMCreditError(LLMError):
    """The key is fine, its Anthropic account is out of credit. Anthropic answers 400 and the
    message text is the only thing that separates this from a malformed request, so the caller
    can tell the user to top up Billing instead of blaming the model."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message, retryable=False, status=status)


#: Substrings Anthropic uses in the 400 it returns for an empty balance.
CREDIT_MARKERS: tuple[str, ...] = ("credit balance is too low", "insufficient credit")


def is_credit_error(message: str | None) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in CREDIT_MARKERS)


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class RefusalInfo:
    category: str | None
    explanation: str | None


@dataclass
class LLMResult:
    """A normalised response. ``content`` blocks are dicts in API shape."""

    content: list[dict[str, Any]]
    stop_reason: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    refusal: RefusalInfo | None = None
    request_id: str | None = None
    cost_usd: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(
            str(b.get("text", "")) for b in self.content if b.get("type") == "text"
        ).strip()

    @property
    def tool_uses(self) -> list[ToolUse]:
        return [
            ToolUse(id=str(b["id"]), name=str(b["name"]), input=dict(b.get("input") or {}))
            for b in self.content
            if b.get("type") == "tool_use"
        ]

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == STOP_TOOL_USE

    @property
    def truncated(self) -> bool:
        return self.stop_reason in {STOP_MAX_TOKENS, STOP_CONTEXT_EXCEEDED}

    @property
    def paused(self) -> bool:
        return self.stop_reason == STOP_PAUSE_TURN

    @property
    def refused(self) -> bool:
        return self.stop_reason == STOP_REFUSAL

    def json(self) -> Any:
        """Parse the first text block as JSON (structured outputs put JSON there)."""
        for block in self.content:
            if block.get("type") == "text":
                return json.loads(str(block.get("text", "")))
        raise ValueError("no text block to parse")

    def assistant_message(self) -> dict[str, Any]:
        """The ``{"role": "assistant", ...}`` message to append when continuing the loop."""
        return {"role": "assistant", "content": self.content}


class UsageRecorder(Protocol):
    async def record(
        self, *, user_id: int | None, model: str, purpose: str, usage: LLMUsage, cost_usd: float
    ) -> None: ...


class MemoryUsageRecorder:
    """Keeps every record in a list (tests, and the default when no DB is wired)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self, *, user_id: int | None, model: str, purpose: str, usage: LLMUsage, cost_usd: float
    ) -> None:
        self.records.append(
            {
                "user_id": user_id,
                "model": model,
                "purpose": purpose,
                "usage": usage,
                "cost_usd": cost_usd,
            }
        )


class DbUsageRecorder:
    """Aggregates into ``token_usage`` in its own short session (never in the turn's), keyed by
    the user's *local* date (the brief's "per user per day" is the user's day)."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._sessions = session_factory
        self._clock = clock or SystemClock()

    async def record(
        self, *, user_id: int | None, model: str, purpose: str, usage: LLMUsage, cost_usd: float
    ) -> None:
        if user_id is None:
            log.debug("usage_without_user", model=model, purpose=purpose, usage=usage)
            return
        from strikt.core.clock import to_local
        from strikt.db import repo

        async with self._sessions() as session, session.begin():
            user = await repo.get_user(session, user_id)
            tz = (user.timezone if user is not None else None) or "UTC"
            await repo.add_usage(
                session,
                user_id,
                day=to_local(self._clock.now(), tz).date(),
                model=model,
                purpose=UsagePurpose(purpose),
                input_tokens=usage.input_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost_usd,
            )


class LLMClient(Protocol):
    """What the turn loop, verify, proactive and summaries depend on (``LLM`` or ``FakeLLM``)."""

    async def message(
        self,
        *,
        purpose: UsagePurpose | str,
        system: str | Sequence[Mapping[str, Any]] | None,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        output_schema: Mapping[str, Any] | None = None,
        user_id: int | None = None,
        cache_tail: bool = True,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> LLMResult: ...


class LLM:
    """Real client. Retries are the SDK defaults (2 retries, exponential backoff).

    ``api_key`` is the key every call is billed to (a user's own key through ``LLMFactory``);
    without it the server key from settings is used."""

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        client: AsyncAnthropic | None = None,
        recorder: UsageRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or AsyncAnthropic(
            api_key=api_key or settings.server_api_key,
            timeout=settings.llm_timeout_s,
        )
        self._recorder: UsageRecorder = recorder or MemoryUsageRecorder()
        self.model = settings.model

    def _effort_for(self, purpose: str) -> Effort:
        s = self._settings
        return {
            "turn": s.effort_turn,
            "verify": s.effort_verify,
            "proactive": s.effort_proactive,
            "summary": s.effort_summary,
            "research": s.effort_research,
        }.get(purpose, s.effort_turn)

    def _max_tokens_for(self, purpose: str) -> int:
        s = self._settings
        return {
            "turn": s.max_tokens_turn,
            "verify": s.max_tokens_verify,
            "proactive": s.max_tokens_proactive,
            "summary": s.max_tokens_summary,
            "research": s.max_tokens_research,
        }.get(purpose, s.max_tokens_turn)

    async def message(
        self,
        *,
        purpose: UsagePurpose | str,
        system: str | Sequence[Mapping[str, Any]] | None,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        output_schema: Mapping[str, Any] | None = None,
        user_id: int | None = None,
        cache_tail: bool = True,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        purpose_str = str(UsagePurpose(purpose))
        output_config: dict[str, Any] = {"effort": effort or self._effort_for(purpose_str)}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": dict(output_schema)}

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self._max_tokens_for(purpose_str),
            "messages": cast("list[MessageParam]", [dict(m) for m in messages]),
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if system:
            kwargs["system"] = (
                system
                if isinstance(system, str)
                else cast("list[TextBlockParam]", [dict(b) for b in system])
            )
        if tools:
            kwargs["tools"] = cast("list[ToolUnionParam]", [dict(t) for t in tools])
        if tool_choice is not None:
            kwargs["tool_choice"] = cast("ToolChoiceParam", dict(tool_choice))
        if cache_tail:
            kwargs["cache_control"] = {"type": "ephemeral"}

        try:
            message = await self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}", retryable=True) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited", retryable=True, status=429) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            # the key this call was billed to is dead or forbidden: the user has to send a new one
            log.warning(
                "llm_key_rejected", purpose=purpose_str, user_id=user_id, status=exc.status_code
            )
            raise LLMAuthError(
                f"api key rejected ({exc.status_code})", status=exc.status_code
            ) from exc
        except anthropic.APIStatusError as exc:
            detail = f"api error {exc.status_code}: {exc.message}"
            if exc.status_code == 400 and is_credit_error(exc.message):
                log.warning("llm_no_credit", purpose=purpose_str, user_id=user_id)
                raise LLMCreditError(detail, status=exc.status_code) from exc
            raise LLMError(
                detail,
                retryable=exc.status_code >= 500,
                status=exc.status_code,
            ) from exc

        usage = usage_from_message(message)
        cost = compute_cost(self._settings.price_for(message.model), usage)
        result = LLMResult(
            content=[block.model_dump(mode="json", exclude_none=True) for block in message.content],
            stop_reason=str(message.stop_reason or STOP_END_TURN),
            usage=usage,
            model=message.model,
            request_id=getattr(message, "_request_id", None),
            cost_usd=cost,
        )
        if message.stop_reason == STOP_REFUSAL:
            details = message.stop_details
            result.refusal = RefusalInfo(
                category=getattr(details, "category", None),
                explanation=getattr(details, "explanation", None),
            )
            log.warning("llm_refusal", purpose=purpose_str, category=result.refusal.category)
        if result.truncated:
            log.warning("llm_truncated", purpose=purpose_str, stop_reason=result.stop_reason)

        try:
            await self._recorder.record(
                user_id=user_id,
                model=message.model,
                purpose=purpose_str,
                usage=usage,
                cost_usd=cost,
            )
        except Exception as exc:
            log.error("usage_record_failed", error=repr(exc))
        log.info(
            "llm_call",
            purpose=purpose_str,
            stop_reason=result.stop_reason,
            input_tokens=usage.input_tokens,
            cache_read=usage.cache_read_tokens,
            cache_write=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            web_searches=usage.web_search_requests,
            cost_usd=round(cost, 6),
            request_id=result.request_id,
        )
        return result


class FakeLLM:
    """Scripted responses in order; every call is recorded in ``calls`` for assertions."""

    def __init__(self, responses: Sequence[LLMResult] = ()) -> None:
        self.responses: list[LLMResult] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.model = "fake-model"

    def queue(self, *results: LLMResult) -> None:
        self.responses.extend(results)

    async def message(
        self,
        *,
        purpose: UsagePurpose | str,
        system: str | Sequence[Mapping[str, Any]] | None,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int | None = None,
        effort: Effort | None = None,
        output_schema: Mapping[str, Any] | None = None,
        user_id: int | None = None,
        cache_tail: bool = True,
        tool_choice: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        self.calls.append(
            {
                "purpose": str(purpose),
                "system": system,
                "messages": [dict(m) for m in messages],
                "tools": [dict(t) for t in tools] if tools else None,
                "max_tokens": max_tokens,
                "effort": effort,
                "output_schema": dict(output_schema) if output_schema else None,
                "user_id": user_id,
                "cache_tail": cache_tail,
                "tool_choice": dict(tool_choice) if tool_choice else None,
            }
        )
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self.responses.pop(0)

    # --- builders for scripts -------------------------------------------------------------
    @staticmethod
    def text(text: str, *, usage: LLMUsage | None = None) -> LLMResult:
        return LLMResult(
            content=[{"type": "text", "text": text}],
            stop_reason=STOP_END_TURN,
            usage=usage or LLMUsage(input_tokens=10, output_tokens=5),
            model="fake-model",
        )

    @staticmethod
    def tool_use(
        name: str,
        input: Mapping[str, Any],
        *,
        id: str = "toolu_fake_1",
        text: str | None = None,
    ) -> LLMResult:
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "tool_use", "id": id, "name": name, "input": dict(input)})
        return LLMResult(
            content=content,
            stop_reason=STOP_TOOL_USE,
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            model="fake-model",
        )

    @staticmethod
    def json_result(payload: Mapping[str, Any]) -> LLMResult:
        return FakeLLM.text(json.dumps(dict(payload), ensure_ascii=False))

    @staticmethod
    def refusal(explanation: str = "refused") -> LLMResult:
        return LLMResult(
            content=[],
            stop_reason=STOP_REFUSAL,
            usage=LLMUsage(input_tokens=10),
            model="fake-model",
            refusal=RefusalInfo(category=None, explanation=explanation),
        )


# --------------------------------------------------------------------------- one LLM per key


class LLMResolver(Protocol):
    """Hands out the ``LLMClient`` that bills the right key for a user (``LLMFactory`` in the
    app, ``FakeLLMFactory`` in tests). ``None`` means the user has no usable key: the caller
    replies with the key walkthrough (a turn) or skips silently (proactive, summaries)."""

    def for_key(self, api_key: str) -> LLMClient: ...

    async def for_user(self, session: AsyncSession, user: User) -> LLMClient | None: ...


class LLMFactory:
    """One ``LLM`` per distinct API key (LRU, ``MAX_CLIENTS``), keyed by the key's sha256 so the
    key itself is never a dict key or a log field.

    ``for_user`` applies ``settings.llm_key_mode``:

    - ``user`` (default): the user's stored key; a user without one gets ``None`` — except an
      admin (``ADMIN_TELEGRAM_IDS``), who falls back to the server key when it is set, so the
      operator can use their own bot without pasting a key;
    - ``server``: the server key for everyone (private deployments).

    ``build`` is the seam for tests (``lambda key: fake_llm``); the default builds a real ``LLM``
    that records usage through ``recorder``."""

    def __init__(
        self,
        settings: Settings,
        recorder: UsageRecorder | None = None,
        cipher: TokenCipher | None = None,
        *,
        build: Callable[[str], LLMClient] | None = None,
    ) -> None:
        self._settings = settings
        self._recorder: UsageRecorder = recorder or MemoryUsageRecorder()
        self._cipher = cipher
        self._build: Callable[[str], LLMClient] = build or self._build_real
        self._clients: OrderedDict[str, LLMClient] = OrderedDict()

    def _build_real(self, api_key: str) -> LLMClient:
        return LLM(self._settings, api_key=api_key, recorder=self._recorder)

    @property
    def mode(self) -> str:
        return self._settings.llm_key_mode

    def __len__(self) -> int:
        return len(self._clients)

    def for_key(self, api_key: str) -> LLMClient:
        """The client for this key, built on first use and reused afterwards."""
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        client = self._clients.get(digest)
        if client is None:
            client = self._build(api_key)
            self._clients[digest] = client
            while len(self._clients) > MAX_CLIENTS:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(digest)
        return client

    def server(self) -> LLMClient | None:
        """The client on the operator's ``ANTHROPIC_API_KEY``, or None when it is not set."""
        key = self._settings.server_api_key
        return self.for_key(key) if key else None

    async def for_user(self, session: AsyncSession, user: User) -> LLMClient | None:
        if self._settings.llm_key_mode == "server":
            llm = self.server()
            if llm is None:
                log.error("llm_server_key_missing", user_id=user.id)
            return llm
        key = await self._user_key(session, user)
        if key:
            return self.for_key(key)
        if self._settings.is_admin(user.telegram_id):
            llm = self.server()
            if llm is not None:
                log.debug("llm_admin_fallback", user_id=user.id)
                return llm
        log.info("llm_key_missing", user_id=user.id, mode=self._settings.llm_key_mode)
        return None

    async def _user_key(self, session: AsyncSession, user: User) -> str | None:
        if self._cipher is None:
            if user.llm_key_enc:
                log.error("llm_key_no_cipher", user_id=user.id)
            return None
        from strikt.db import repo

        try:
            return await repo.get_llm_key(session, user.id, self._cipher)
        except ValueError as exc:
            # TOKEN_ENCRYPTION_KEY changed: the stored key is unreadable; ask for it again
            log.warning("llm_key_undecryptable", user_id=user.id, error=str(exc))
            return None


class FakeLLMFactory:
    """Every user gets the same ``FakeLLM`` (tests that are not about keys). ``llm=None`` makes
    every user keyless."""

    def __init__(self, llm: LLMClient | None) -> None:
        self.llm = llm
        self.resolved: list[int] = []

    def for_key(self, api_key: str) -> LLMClient:
        if self.llm is None:
            raise AssertionError("FakeLLMFactory has no LLM")
        return self.llm

    async def for_user(self, session: AsyncSession, user: User) -> LLMClient | None:
        self.resolved.append(user.id)
        return self.llm


# ------------------------------------------------------------------------- key validation

KeyCheck = Literal["valid", "invalid", "unknown"]


class KeyValidator(Protocol):
    async def check(self, api_key: str) -> KeyCheck: ...


class AnthropicKeyValidator:
    """One cheap authenticated call, ``GET /v1/models/{settings.model}``, on a client built from
    the pasted key with a ``KEY_CHECK_TIMEOUT_S`` timeout and no retries. 401/403 → ``invalid``;
    anything else that fails (network, 5xx, 429, a model the key cannot see) → ``unknown``: the
    key is stored anyway and verified by the user's next real call, which answers with
    ``key.rejected`` if it is bad."""

    def __init__(
        self, settings: Settings, *, make_client: Callable[[str], AsyncAnthropic] | None = None
    ) -> None:
        self._settings = settings
        self._make_client = make_client or self._default_client

    @staticmethod
    def _default_client(api_key: str) -> AsyncAnthropic:
        return AsyncAnthropic(api_key=api_key, timeout=KEY_CHECK_TIMEOUT_S, max_retries=0)

    async def check(self, api_key: str) -> KeyCheck:
        client = self._make_client(api_key)
        try:
            await client.models.retrieve(self._settings.model)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            log.info("llm_key_check_rejected", status=exc.status_code)
            return "invalid"
        except Exception as exc:
            log.warning("llm_key_check_failed", error=type(exc).__name__)
            return "unknown"
        return "valid"


class FakeKeyValidator:
    """Scripted: ``result`` for every key; every checked key is recorded."""

    def __init__(self, result: KeyCheck = "valid") -> None:
        self.result: KeyCheck = result
        self.checked: list[str] = []

    async def check(self, api_key: str) -> KeyCheck:
        self.checked.append(api_key)
        return self.result
