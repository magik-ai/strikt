"""Bring-your-own-key, the pieces: the settings modes, the encrypted key in ``users``
(migration 0002, repo roundtrip), ``LLMFactory`` caching and mode rules, the key validator
against a fake Anthropic client, key detection, log redaction and the code-rendered copy."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import (
    KEY_CHECK_TIMEOUT_S,
    LLM,
    MAX_CLIENTS,
    AnthropicKeyValidator,
    FakeKeyValidator,
    FakeLLM,
    FakeLLMFactory,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMFactory,
    MemoryUsageRecorder,
)
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher, generate_key
from strikt.db.models import User, UserStatus
from strikt.logging import mask_key_like, redact_secrets
from strikt.telegram.copy import STRINGS, t
from strikt.telegram.keys import extract_key, is_key_message, mentions_key
from tests.conftest import TELEGRAM_ID

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-ant-api03-" + "a" * 60 + "WXYZ"
OTHER_KEY = "sk-ant-api03-" + "b" * 60 + "1234"
SERVER_KEY = "sk-ant-api03-" + "s" * 60 + "SRVR"
ADMIN_ID = 42


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"token_encryption_key": generate_key()}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ------------------------------------------------------------------------------- settings


def test_user_mode_is_the_default_and_needs_no_server_key() -> None:
    s = make_settings(telegram_bot_token="t")
    assert s.llm_key_mode == "user" and s.server_api_key is None
    assert "ANTHROPIC_API_KEY" not in s.missing_for_runtime()
    assert make_settings(anthropic_api_key="  ").server_api_key is None
    assert make_settings(anthropic_api_key=SERVER_KEY).server_api_key == SERVER_KEY


def test_server_mode_requires_the_server_key() -> None:
    s = make_settings(telegram_bot_token="t", llm_key_mode="server")
    assert s.missing_for_runtime() == ["ANTHROPIC_API_KEY"]
    ok = make_settings(telegram_bot_token="t", llm_key_mode="server", anthropic_api_key=SERVER_KEY)
    assert ok.missing_for_runtime() == []


def test_env_example_documents_the_mode() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LLM_KEY_MODE=user" in text
    assert "ANTHROPIC_API_KEY=" in text and "REQUIRED. Claude API key." not in text


# ------------------------------------------------------------------------------ migration


def _alembic(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return config


def _user_columns(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("pragma table_info(users)")}


def test_revision_0002_adds_the_key_columns(tmp_path: Path) -> None:
    db = tmp_path / "byok.sqlite"
    config = _alembic(db)
    command.upgrade(config, "0001")
    before = _user_columns(db)
    assert not {"llm_key_enc", "llm_key_last4", "llm_key_set_at"} & before
    command.upgrade(config, "head")
    assert {"llm_key_enc", "llm_key_last4", "llm_key_set_at"} <= _user_columns(db)
    command.downgrade(config, "0001")
    assert _user_columns(db) == before


# ----------------------------------------------------------------------------------- repo


async def test_repo_roundtrip_stores_ciphertext_not_the_key(
    session: AsyncSession, user: User, cipher: TokenCipher, clock: FakeClock
) -> None:
    assert await repo.get_llm_key(session, user.id, cipher) is None
    last4 = await repo.set_llm_key(session, user.id, KEY, cipher, now=clock.now())
    await session.commit()
    assert last4 == "WXYZ"
    await session.refresh(user)
    assert user.llm_key_enc and KEY not in user.llm_key_enc and user.llm_key_enc != KEY
    assert user.llm_key_last4 == "WXYZ"
    assert user.llm_key_set_at is not None
    assert await repo.get_llm_key(session, user.id, cipher) == KEY
    # a new key replaces the old one
    assert await repo.set_llm_key(session, user.id, OTHER_KEY, cipher, now=clock.now()) == "1234"
    assert await repo.get_llm_key(session, user.id, cipher) == OTHER_KEY
    # cleared: gone, and clearing twice is a no-op
    assert await repo.clear_llm_key(session, user.id) is True
    assert await repo.get_llm_key(session, user.id, cipher) is None
    assert await repo.clear_llm_key(session, user.id) is False
    await session.refresh(user)
    assert user.llm_key_enc is None and user.llm_key_last4 is None and user.llm_key_set_at is None


async def test_repo_edge_cases(session: AsyncSession, user: User, cipher: TokenCipher) -> None:
    now = datetime(2026, 9, 3, 8, tzinfo=UTC)
    assert await repo.get_llm_key(session, 424242, cipher) is None
    with pytest.raises(ValueError, match="empty"):
        await repo.set_llm_key(session, user.id, "   ", cipher, now=now)
    with pytest.raises(ValueError, match="does not exist"):
        await repo.set_llm_key(session, 424242, KEY, cipher, now=now)
    await repo.set_llm_key(session, user.id, KEY, cipher, now=now)
    with pytest.raises(ValueError):  # TOKEN_ENCRYPTION_KEY rotated: the ciphertext is unreadable
        await repo.get_llm_key(session, user.id, TokenCipher(generate_key()))


# -------------------------------------------------------------------------------- factory


class _Builder:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def __call__(self, key: str) -> LLMClient:
        self.keys.append(key)
        return FakeLLM()


def test_for_key_caches_by_key_and_evicts_lru() -> None:
    builder = _Builder()
    factory = LLMFactory(make_settings(), MemoryUsageRecorder(), None, build=builder)
    first = factory.for_key(KEY)
    assert factory.for_key(KEY) is first
    assert factory.for_key(OTHER_KEY) is not first
    assert builder.keys == [KEY, OTHER_KEY] and len(factory) == 2
    for n in range(MAX_CLIENTS):  # push everything but KEY (touched again) out of the window
        factory.for_key(KEY)
        factory.for_key(f"sk-ant-api03-{n:0>60}")
    assert len(factory) == MAX_CLIENTS
    assert factory.for_key(KEY) is first  # kept: most recently used
    factory.for_key(OTHER_KEY)  # evicted: built again
    assert builder.keys.count(OTHER_KEY) == 2


def test_default_build_is_a_real_llm_on_that_key() -> None:
    settings = make_settings(anthropic_api_key=SERVER_KEY)
    factory = LLMFactory(settings, MemoryUsageRecorder(), None)
    llm = factory.for_key(KEY)
    assert isinstance(llm, LLM) and llm._client.api_key == KEY
    server = factory.server()
    assert isinstance(server, LLM) and server._client.api_key == SERVER_KEY
    assert LLMFactory(make_settings(), MemoryUsageRecorder(), None).server() is None


async def test_for_user_in_user_mode(
    session: AsyncSession, user: User, cipher: TokenCipher, clock: FakeClock
) -> None:
    builder = _Builder()
    settings = make_settings(admin_telegram_ids=[ADMIN_ID], anthropic_api_key=SERVER_KEY)
    factory = LLMFactory(settings, MemoryUsageRecorder(), cipher, build=builder)
    assert factory.mode == "user"
    # a plain user without a key: nothing, and nothing built on the server key
    assert await factory.for_user(session, user) is None and builder.keys == []
    # with a key: the client on that key
    await repo.set_llm_key(session, user.id, KEY, cipher, now=clock.now())
    own = await factory.for_user(session, user)
    assert own is not None and builder.keys == [KEY]
    assert await factory.for_user(session, user) is own
    # an admin without a key falls back to the server key
    admin, _ = await repo.get_or_create_user(
        session, telegram_id=ADMIN_ID, chat_id=ADMIN_ID, now=clock.now(), status=UserStatus.active
    )
    assert await factory.for_user(session, admin) is factory.server()
    assert builder.keys == [KEY, SERVER_KEY]
    # an admin with their own key uses it, not the server's
    await repo.set_llm_key(session, admin.id, OTHER_KEY, cipher, now=clock.now())
    assert await factory.for_user(session, admin) is factory.for_key(OTHER_KEY)


async def test_admin_without_server_key_is_keyless_too(
    session: AsyncSession, cipher: TokenCipher, clock: FakeClock
) -> None:
    factory = LLMFactory(
        make_settings(admin_telegram_ids=[ADMIN_ID]),
        MemoryUsageRecorder(),
        cipher,
        build=_Builder(),
    )
    admin, _ = await repo.get_or_create_user(
        session, telegram_id=ADMIN_ID, chat_id=ADMIN_ID, now=clock.now(), status=UserStatus.active
    )
    assert await factory.for_user(session, admin) is None


async def test_for_user_in_server_mode(
    session: AsyncSession, user: User, cipher: TokenCipher, clock: FakeClock
) -> None:
    builder = _Builder()
    settings = make_settings(llm_key_mode="server", anthropic_api_key=SERVER_KEY)
    factory = LLMFactory(settings, MemoryUsageRecorder(), cipher, build=builder)
    server = await factory.for_user(session, user)
    assert server is factory.server() and builder.keys == [SERVER_KEY]
    await repo.set_llm_key(session, user.id, KEY, cipher, now=clock.now())
    assert await factory.for_user(session, user) is server  # the user's key is not used
    assert builder.keys == [SERVER_KEY]
    broken = LLMFactory(make_settings(llm_key_mode="server"), MemoryUsageRecorder(), cipher)
    assert await broken.for_user(session, user) is None


async def test_unreadable_stored_key_counts_as_missing(
    session: AsyncSession, user: User, cipher: TokenCipher, clock: FakeClock
) -> None:
    await repo.set_llm_key(session, user.id, KEY, cipher, now=clock.now())
    rotated = LLMFactory(make_settings(), MemoryUsageRecorder(), TokenCipher(generate_key()))
    assert await rotated.for_user(session, user) is None
    no_cipher = LLMFactory(make_settings(), MemoryUsageRecorder(), None)
    assert await no_cipher.for_user(session, user) is None


async def test_fake_factory(session: AsyncSession, user: User) -> None:
    fake = FakeLLM()
    factory = FakeLLMFactory(fake)
    assert await factory.for_user(session, user) is fake and factory.for_key(KEY) is fake
    assert factory.resolved == [user.id]
    keyless = FakeLLMFactory(None)
    assert await keyless.for_user(session, user) is None
    with pytest.raises(AssertionError):
        keyless.for_key(KEY)


# ------------------------------------------------------------------------------ validator


def _response(status: int) -> httpx2.Response:
    request = httpx2.Request("GET", "https://api.anthropic.com/v1/models/claude-sonnet-5")
    return httpx2.Response(status, request=request, json={"error": {"message": "x"}})


class StubModels:
    def __init__(self, outcome: BaseException | None) -> None:
        self.outcome = outcome
        self.retrieved: list[str] = []

    async def retrieve(self, model_id: str, **kwargs: Any) -> dict[str, str]:
        self.retrieved.append(model_id)
        if self.outcome is not None:
            raise self.outcome
        return {"id": model_id}


class StubAnthropic:
    def __init__(self, outcome: BaseException | None = None) -> None:
        self.models = StubModels(outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, "valid"),
        (anthropic.AuthenticationError("bad key", response=_response(401), body=None), "invalid"),
        (
            anthropic.PermissionDeniedError("forbidden", response=_response(403), body=None),
            "invalid",
        ),
        (anthropic.NotFoundError("no model", response=_response(404), body=None), "unknown"),
        (anthropic.RateLimitError("slow", response=_response(429), body=None), "unknown"),
        (anthropic.APIConnectionError(request=_response(500).request), "unknown"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_validator_maps_the_sdk_errors(outcome: BaseException | None, expected: str) -> None:
    settings = make_settings()
    made: list[tuple[str, StubAnthropic]] = []

    def make_client(key: str) -> Any:
        stub = StubAnthropic(outcome)
        made.append((key, stub))
        return stub

    validator = AnthropicKeyValidator(settings, make_client=make_client)
    assert await validator.check(KEY) == expected
    assert made[0][0] == KEY and made[0][1].models.retrieved == [settings.model]


def test_default_validator_client_is_built_on_the_key_without_retries() -> None:
    client = AnthropicKeyValidator._default_client(KEY)
    assert client.api_key == KEY and client.max_retries == 0
    assert client.timeout == KEY_CHECK_TIMEOUT_S


async def test_fake_validator_records_keys() -> None:
    validator = FakeKeyValidator("invalid")
    assert await validator.check(KEY) == "invalid" and validator.checked == [KEY]


# --------------------------------------------------------------------- LLM: rejected key


class _RejectingMessages:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def create(self, **kwargs: Any) -> Any:
        raise self.exc


class _RejectingClient:
    def __init__(self, exc: BaseException) -> None:
        self.messages = _RejectingMessages(exc)


async def test_llm_maps_401_and_403_to_auth_error() -> None:
    settings = make_settings()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
    for status, exc_cls in (
        (401, anthropic.AuthenticationError),
        (403, anthropic.PermissionDeniedError),
    ):
        client = _RejectingClient(exc_cls("nope", response=_response(status), body=None))
        llm = LLM(settings, api_key=KEY, client=client)  # type: ignore[arg-type]
        with pytest.raises(LLMAuthError) as info:
            await llm.message(purpose="turn", system=None, messages=msgs, user_id=1)
        assert info.value.status == status and not info.value.retryable
        assert isinstance(info.value, LLMError)


# -------------------------------------------------------------------------- key detection


def test_extract_key_and_mentions() -> None:
    assert extract_key(f"here you go {KEY} thanks") == KEY
    assert extract_key(KEY) == KEY and is_key_message(f"\n{KEY}\n")
    assert extract_key("sk-ant-short") is None and extract_key(None) is None
    assert not is_key_message("omelette 3 eggs")
    for text in ("where do I get the key?", "API?", "какой ключ нужен", "ключа нет", "token"):
        assert mentions_key(text), text
    for text in ("omelette 3 eggs", "monkey business", "3 яйца", None, ""):
        assert not mentions_key(text), text


# ------------------------------------------------------------------------------ redaction


def test_key_like_strings_are_masked_wherever_they_appear() -> None:
    event: dict[str, Any] = {
        "event": f"user sent {KEY}",
        "text": KEY,
        "nested": {"list": [f"x {OTHER_KEY} y"], "error": ValueError(f"bad {KEY}")},
        "count": 3,
    }
    out = redact_secrets(None, "info", event)
    flat = repr(out)
    assert KEY not in flat and OTHER_KEY not in flat and "sk-ant-***" in flat
    assert out["count"] == 3 and out["event"] == "user sent sk-ant-***"
    assert mask_key_like("plain text") == "plain text"


# ----------------------------------------------------------------------------------- copy


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_key_copy_is_a_short_html_safe_walkthrough(lang: str) -> None:
    for key in (
        "key.needed",
        "key.help",
        "key.saved",
        "key.saved_keep",
        "key.unchecked",
        "key.invalid",
        "key.rejected",
    ):
        assert STRINGS[lang][key].strip(), (lang, key)
    for key in ("key.needed", "key.help"):
        text = t(lang, key)
        lines = text.split("\n")
        assert 5 <= len(lines) <= 12, (key, len(lines))
        assert all(len(line) <= 120 for line in lines), key
        assert not any(ch in text for ch in "<>&"), key
        assert "console.anthropic.com" in text and "Billing" in text and "sk-ant-" in text
        assert "/forget_me" in text and text.count("1.") == 1 and "4." in text
    saved = t(lang, "key.saved", last4="WXYZ")
    assert "WXYZ" in saved and "…WXYZ" in saved
    assert "WXYZ" in t(lang, "key.saved_keep", last4="WXYZ")
    invalid = t(lang, "key.invalid")
    assert ("step 3" in invalid or "шаг 3" in invalid) and "Billing" in invalid
    assert "console.anthropic.com" in t(lang, "key.rejected")
    api_key = "API key" if lang == "en" else "API-ключ"
    assert api_key in t(lang, "forget.question") and api_key in t(lang, "forget.done", rows=3)
    assert "Anthropic" in t(lang, "key.needed")
    assert TELEGRAM_ID  # the module is imported for the shared constants only
