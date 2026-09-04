"""The optional keys a user hands over in the chat: OpenAI for voice, USDA for the food database.

Same contract as the Anthropic key and for the same reasons: the message is deleted first, one
cheap request separates a typo from a working key, the value is encrypted at rest, only the last
four characters are ever shown, and it never becomes a conversation turn. What differs is that
both are optional, so a key the service rejects and a user who changes their mind both have to
leave the chat in a sane state.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import FakeLLM
from strikt.agent.tools import build_registry
from strikt.agent.tools.registry import ToolContext
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import ConversationTurn, SecretService, User, UserSecret, UserStatus
from strikt.telegram import handlers as handlers_mod
from strikt.telegram.copy import t
from strikt.telegram.handlers import handle_message
from strikt.telegram.keys import extract_key, extract_openai_key, looks_like_usda_key
from strikt.telegram.messenger import FakeMessenger
from strikt.telegram.voice import (
    NullTranscriber,
    OpenAITranscriber,
    TranscriberFactory,
)
from tests.conftest import CHAT_ID
from tests.test_byok_core import KEY
from tests.test_byok_handlers import Byok, byok  # noqa: F401 - fixture
from tests.test_handlers_e2e import NEW_ID, msg

OPENAI_KEY = "sk-proj-" + "o" * 40
USDA_KEY = "U" * 40


def _verdict(value: str) -> Any:
    async def check(service: str, key: str, **kwargs: Any) -> str:
        return value

    return check


# ------------------------------------------------------------------------------- what is a key


def test_the_three_key_shapes_do_not_collide() -> None:
    anthropic_key = "sk-ant-api03-" + "a" * 30
    assert extract_key(anthropic_key) == anthropic_key
    assert extract_openai_key(anthropic_key) is None, "an Anthropic key is never an OpenAI one"
    assert extract_openai_key(OPENAI_KEY) == OPENAI_KEY
    assert extract_key(OPENAI_KEY) is None
    assert looks_like_usda_key(USDA_KEY)
    # a USDA key is forty plain characters, which is why it is only read when asked for
    assert not looks_like_usda_key("no thanks")
    assert not looks_like_usda_key(OPENAI_KEY)


def test_a_mistyped_anthropic_key_is_not_filed_as_an_openai_one() -> None:
    """Filing it under OpenAI means the user is told their key does not work while the coach
    keeps asking for one. The Anthropic path at least says what is actually wrong."""
    for typo in ("sk-antapi03-" + "a" * 30, "sk-ANT-api03-" + "a" * 30):
        assert extract_key(typo), typo
        assert extract_openai_key(typo) is None, typo
    for real in ("sk-proj-" + "o" * 40, "sk-svcacct-" + "o" * 40):
        assert extract_openai_key(real) == real


# ------------------------------------------------------------------------------ pasting a key


async def test_an_openai_key_is_stored_deleted_and_never_a_turn(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers_mod, "check_secret", _verdict("valid"))

    await handle_message(byok.deps, msg(OPENAI_KEY, message_id=200))

    assert messenger.deletes == [(CHAT_ID, 200)]
    assert messenger.texts(CHAT_ID) == [t("ru", "secret.saved", last4=OPENAI_KEY[-4:])]
    assert fake_llm.calls == [], "a pasted key is never a turn"
    stored = await repo.get_user_secret(session, user.id, SecretService.openai, byok.cipher)
    assert stored == OPENAI_KEY
    row = (await session.scalars(select(UserSecret))).one()
    assert row.last4 == OPENAI_KEY[-4:] and row.key_enc != OPENAI_KEY
    turns = (await session.scalars(select(ConversationTurn))).all()
    assert all(OPENAI_KEY not in (turn.text or "") for turn in turns)


async def test_a_key_the_service_rejects_is_not_stored(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message still leaves the chat first: a wrong key is as private as a right one."""
    monkeypatch.setattr(handlers_mod, "check_secret", _verdict("invalid"))

    await handle_message(byok.deps, msg(OPENAI_KEY, message_id=201))

    assert messenger.deletes == [(CHAT_ID, 201)]
    assert messenger.texts(CHAT_ID) == [t("ru", "secret.invalid")]
    assert await repo.get_user_secret(session, user.id, "openai", byok.cipher) is None


async def test_a_key_the_service_cannot_answer_about_is_kept(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing a good key over somebody else's outage is worse than finding out later."""
    monkeypatch.setattr(handlers_mod, "check_secret", _verdict("unknown"))

    await handle_message(byok.deps, msg(OPENAI_KEY, message_id=202))

    assert await repo.get_user_secret(session, user.id, "openai", byok.cipher) == OPENAI_KEY
    assert t("ru", "secret.unchecked") in messenger.texts(CHAT_ID)[-1], "say it was not checked"


async def test_a_key_pasted_before_the_language_answer_is_still_a_key(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new user sits in ``UserStatus.language`` until they answer. A key arriving there used to
    be read as the language answer: never deleted, never checked, never stored, and the user
    silently pinned to English."""
    monkeypatch.setattr(handlers_mod, "check_secret", _verdict("valid"))
    await handle_message(byok.deps, msg("/start", telegram_id=NEW_ID, language_code="en"))
    new_user = await repo.get_user_by_telegram_id(session, NEW_ID)
    assert new_user is not None and new_user.status == UserStatus.language

    await handle_message(byok.deps, msg(OPENAI_KEY, telegram_id=NEW_ID, message_id=220))

    assert messenger.deletes == [(NEW_ID, 220)]
    stored = await repo.get_user_secret(session, new_user.id, "openai", byok.cipher)
    assert stored == OPENAI_KEY
    await session.refresh(new_user)
    assert new_user.status == UserStatus.language, "and the language is still an open question"


# --------------------------------------------------------------------------- asking for a key


async def test_request_key_makes_the_next_message_the_key(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
    clock: FakeClock,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USDA key matches nothing on sight, so it is only read while the coach is waiting."""
    monkeypatch.setattr(handlers_mod, "check_secret", _verdict("valid"))
    ctx = ToolContext(
        session=session,
        user=user,
        profile=None,
        protocol=None,
        clock=clock,
        settings=settings,
        services={},
    )
    result = await build_registry().dispatch(ctx, "request_key", {"service": "usda"})
    await session.commit()
    assert not result.is_error
    assert user.awaiting_secret == "usda"

    await handle_message(byok.deps, msg(USDA_KEY, message_id=210))

    assert messenger.deletes == [(CHAT_ID, 210)]
    assert await repo.get_user_secret(session, user.id, "usda", byok.cipher) == USDA_KEY
    await session.refresh(user)
    assert user.awaiting_secret is None


async def test_changing_your_mind_drops_the_wait_and_answers_normally(
    byok: Byok,  # noqa: F811
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    session: AsyncSession,
) -> None:
    """ "Not now" is not a USDA key. The wait ends and the message is an ordinary turn."""
    await handle_message(byok.deps, msg(KEY, message_id=1))  # the coach needs its own key
    await repo.set_awaiting_secret(session, user.id, "usda")
    await session.commit()
    messenger.sent.clear()
    fake_llm.queue(FakeLLM.text("Понял, пропускаем."))

    await handle_message(byok.deps, msg("не хочу, давай дальше", message_id=211))

    assert messenger.texts(CHAT_ID) == ["Понял, пропускаем."]
    assert len(fake_llm.calls) == 1
    await session.refresh(user)
    assert user.awaiting_secret is None
    assert await repo.get_user_secret(session, user.id, "usda", byok.cipher) is None


# ----------------------------------------------------------------------------- what it buys


async def test_the_transcriber_follows_the_users_own_key(
    session: AsyncSession, user: User, settings: Settings, cipher: TokenCipher, clock: FakeClock
) -> None:
    factory = TranscriberFactory(settings, cipher)
    assert isinstance(await factory.for_user(session, user), NullTranscriber)

    await repo.set_user_secret(session, user.id, "openai", OPENAI_KEY, cipher, now=clock.now())
    await session.commit()

    mine = await factory.for_user(session, user)
    assert isinstance(mine, OpenAITranscriber)
    assert await factory.for_user(session, user) is mine, "one client per key, not per voice note"


async def test_a_new_key_replaces_the_old_one(
    session: AsyncSession, user: User, cipher: TokenCipher, clock: FakeClock
) -> None:
    await repo.set_user_secret(session, user.id, "openai", OPENAI_KEY, cipher, now=clock.now())
    other = "sk-proj-" + "z" * 40
    last4 = await repo.set_user_secret(session, user.id, "openai", other, cipher, now=clock.now())
    await session.commit()

    assert last4 == other[-4:]
    rows = (await session.scalars(select(UserSecret))).all()
    assert len(rows) == 1
    assert await repo.get_user_secret(session, user.id, "openai", cipher) == other
    assert await repo.clear_user_secret(session, user.id, "openai") is True
    assert await repo.get_user_secret(session, user.id, "openai", cipher) is None
