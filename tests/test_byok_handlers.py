"""Bring-your-own-key through the handlers, the proactive engine and the nightly job, with a
real ``LLMFactory`` (mode rules against the DB) whose clients are one ``FakeLLM``.

Covers: a pasted key is checked, stored encrypted, deleted from the chat, never a turn, never
logged; an invalid key (fake validator, and the real validator on a fake Anthropic client
raising ``AuthenticationError``); a keyless user gets the walkthrough and no model call (text,
photo, voice, buttons, ``key.help`` when asking about the key); ``/start`` for a new user ends
with the key line and the interview starts once the key is in; the admin fallback; server mode;
a key rejected mid-turn; the engine, the decider and the nightly summary skip keyless users;
``/forget_me`` takes the key with it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import anthropic
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from strikt import app as app_mod
from strikt.agent.client import (
    AnthropicKeyValidator,
    FakeKeyValidator,
    FakeLLM,
    KeyValidator,
    LLMAuthError,
    LLMClient,
    LLMFactory,
    MemoryUsageRecorder,
)
from strikt.agent.proactive_decide import LLMDecider
from strikt.agent.tools import build_registry
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher, generate_key
from strikt.db.models import ConversationTurn, SummaryKind, User, UserStatus
from strikt.events import EventBus
from strikt.memory.daystate import DayStateBuilder
from strikt.proactive.engine import ProactiveEngine
from strikt.telegram.copy import t
from strikt.telegram.daycard import DayCard
from strikt.telegram.handlers import AppDeps, InboundMessage, handle_callback, handle_message
from strikt.telegram.media import AlbumCollector
from strikt.telegram.messenger import FakeMessenger
from strikt.telegram.queue import PerChatQueue
from tests.conftest import CHAT_ID, TELEGRAM_ID
from tests.test_agentcore_decider import fire, ladder
from tests.test_byok_core import KEY, OTHER_KEY, SERVER_KEY, StubAnthropic, _response
from tests.test_handlers_e2e import (
    ADMIN_ID,
    NEW_ID,
    PHOTO_ID,
    VOICE_ID,
    FakeDownloader,
    FakePlanner,
    FakeTranscriber,
    MediaRef,
    cb,
    jpeg_bytes,
    last_user_text,
    msg,
)
from tests.test_proactive_helpers import TODAY, DbStateProvider, FakeDecider, at_local, make_sender


@dataclass
class Byok:
    deps: AppDeps
    settings: Settings
    cipher: TokenCipher
    factory: LLMFactory
    validator: KeyValidator
    built: list[str]
    downloader: FakeDownloader
    transcriber: FakeTranscriber
    planner: FakePlanner


def make_byok(
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    *,
    validator: KeyValidator | None = None,
    **overrides: Any,
) -> Byok:
    values: dict[str, Any] = {
        "token_encryption_key": generate_key(),
        "allowed_telegram_ids": [TELEGRAM_ID, ADMIN_ID, NEW_ID],
        "admin_telegram_ids": [ADMIN_ID],
    }
    values.update(overrides)
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    built: list[str] = []

    def build(key: str) -> LLMClient:
        built.append(key)
        return fake_llm

    factory = LLMFactory(settings, MemoryUsageRecorder(), cipher, build=build)
    validator = validator or FakeKeyValidator()
    downloader = FakeDownloader(files={PHOTO_ID: jpeg_bytes(), VOICE_ID: b"OggS" * 8})
    transcriber = FakeTranscriber()
    planner = FakePlanner()
    albums: AlbumCollector[InboundMessage] = AlbumCollector(debounce_s=0.05)
    sessions = app_mod.make_session_factory(engine)
    deps = AppDeps(
        settings=settings,
        sessions=sessions,
        clock=clock,
        llm_factory=factory,
        key_validator=validator,
        cipher=cipher,
        registry=build_registry(),
        messenger=messenger,
        bus=EventBus(),
        state_provider=DayStateBuilder(clock, settings),
        transcriber=transcriber,
        downloader=downloader,
        albums=albums,
        queue=PerChatQueue(),
        card=DayCard(messenger, clock),
        scheduler=planner,
    )
    return Byok(deps, settings, cipher, factory, validator, built, downloader, transcriber, planner)


@pytest.fixture
def byok(
    engine: AsyncEngine, clock: FakeClock, fake_llm: FakeLLM, messenger: FakeMessenger
) -> Byok:
    return make_byok(engine, clock, fake_llm, messenger)


async def _stored_key(session: AsyncSession, user_id: int, cipher: TokenCipher) -> str | None:
    session.expire_all()
    return await repo.get_llm_key(session, user_id, cipher)


async def _turn_texts(session: AsyncSession) -> list[str]:
    session.expire_all()
    return [str(turn.content) for turn in (await session.scalars(select(ConversationTurn))).all()]


# ------------------------------------------------------------------------- the key message


async def test_key_message_is_checked_stored_deleted_and_never_a_turn(
    byok: Byok, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    with capture_logs() as logs:
        await handle_message(byok.deps, msg(f"вот ключ: {KEY}", message_id=100))
    assert isinstance(byok.validator, FakeKeyValidator) and byok.validator.checked == [KEY]
    assert await _stored_key(session, user.id, byok.cipher) == KEY
    await session.refresh(user)
    assert user.llm_key_enc is not None and KEY not in user.llm_key_enc
    assert user.llm_key_last4 == "WXYZ"
    assert messenger.deletes == [(CHAT_ID, 100)]
    assert messenger.texts(CHAT_ID) == [t("ru", "key.saved", last4="WXYZ")]
    assert fake_llm.calls == [] and byok.built == []  # an active user: nothing else happens
    assert await _turn_texts(session) == []  # the key is not conversation history
    assert KEY not in repr(logs) and "WXYZ" in repr(logs)  # only the last four are logged
    # from now on the user's own key bills the turns; the message with the key is history
    fake_llm.queue(FakeLLM.text("Понял."))
    await handle_message(byok.deps, msg("привет", message_id=101))
    assert messenger.texts(CHAT_ID)[-1] == "Понял." and byok.built == [KEY]
    assert all(KEY not in text for text in await _turn_texts(session))


async def test_new_key_replaces_the_old_one(
    byok: Byok, messenger: FakeMessenger, user: User, session: AsyncSession
) -> None:
    await handle_message(byok.deps, msg(KEY, message_id=100))
    await handle_message(byok.deps, msg(OTHER_KEY, message_id=101))
    assert await _stored_key(session, user.id, byok.cipher) == OTHER_KEY
    assert messenger.texts(CHAT_ID)[-1] == t("ru", "key.saved", last4="1234")
    assert messenger.deletes == [(CHAT_ID, 100), (CHAT_ID, 101)]


async def test_key_saved_when_telegram_refuses_to_delete(
    byok: Byok, messenger: FakeMessenger, user: User, session: AsyncSession
) -> None:
    messenger.undeletable.add(100)
    await handle_message(byok.deps, msg(KEY, message_id=100))
    assert await _stored_key(session, user.id, byok.cipher) == KEY
    assert messenger.texts(CHAT_ID) == [t("ru", "key.saved_keep", last4="WXYZ")]


async def test_unchecked_key_is_saved_with_a_note(
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
) -> None:
    b = make_byok(engine, clock, fake_llm, messenger, validator=FakeKeyValidator("unknown"))
    await handle_message(b.deps, msg(KEY, message_id=100, language_code="en"))
    assert await _stored_key(session, user.id, b.cipher) == KEY
    assert messenger.texts(CHAT_ID) == [
        t("ru", "key.saved", last4="WXYZ") + "\n" + t("ru", "key.unchecked")
    ]


async def test_invalid_key_is_rejected_deleted_and_not_stored(
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
) -> None:
    b = make_byok(engine, clock, fake_llm, messenger, validator=FakeKeyValidator("invalid"))
    await handle_message(b.deps, msg(KEY, message_id=100))
    assert await _stored_key(session, user.id, b.cipher) is None
    assert messenger.deletes == [(CHAT_ID, 100)]
    assert messenger.texts(CHAT_ID) == [t("ru", "key.invalid")]
    assert fake_llm.calls == [] and await _turn_texts(session) == []


async def test_real_validator_on_a_fake_anthropic_client_raising_authentication_error(
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
) -> None:
    seen: list[str] = []

    def make_client(key: str) -> Any:
        seen.append(key)
        return StubAnthropic(
            anthropic.AuthenticationError("invalid x-api-key", response=_response(401), body=None)
        )

    settings = Settings(_env_file=None, token_encryption_key=generate_key())  # type: ignore[call-arg]
    validator = AnthropicKeyValidator(settings, make_client=make_client)
    b = make_byok(engine, clock, fake_llm, messenger, validator=validator)
    await handle_message(b.deps, msg(KEY, message_id=100))
    assert seen == [KEY]
    assert messenger.texts(CHAT_ID) == [t("ru", "key.invalid")]
    assert await _stored_key(session, user.id, b.cipher) is None


# ----------------------------------------------------------------------- keyless in user mode


async def test_keyless_user_gets_the_walkthrough_and_no_model_call(
    byok: Byok, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    await handle_message(byok.deps, msg("омлет из трёх яиц"))
    assert messenger.texts(CHAT_ID) == [t("ru", "key.needed")]
    # asking about the key gets the same steps under a different first line
    await handle_message(byok.deps, msg("какой ключ нужен?", message_id=101))
    assert messenger.texts(CHAT_ID)[-1] == t("ru", "key.help")
    # a photo is not downloaded, a voice note not transcribed, for a keyless user
    await handle_message(byok.deps, msg(media=[MediaRef("photo", PHOTO_ID)], message_id=102))
    await handle_message(
        byok.deps, msg(media=[MediaRef("voice", VOICE_ID, mime="audio/ogg")], message_id=103)
    )
    assert byok.downloader.calls == [] and byok.transcriber.calls == []
    assert fake_llm.calls == [] and byok.built == []
    assert await _turn_texts(session) == []
    assert messenger.texts(CHAT_ID)[-2:] == [t("ru", "key.needed")] * 2


async def test_buttons_for_a_keyless_user_send_the_walkthrough(
    byok: Byok, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    """The language button is the one button a user without a key can reach; it ends in the
    walkthrough, and nothing is sent to a model on the way."""
    await handle_callback(byok.deps, cb("lang:ru"))
    assert messenger.callbacks == [("cb-lang:ru", None)]
    assert messenger.texts(CHAT_ID) == [t("ru", "start.welcome"), t("ru", "key.needed")]
    assert fake_llm.calls == []


async def test_start_for_a_new_user_ends_with_the_key_line_then_the_interview_after_the_key(
    byok: Byok, messenger: FakeMessenger, fake_llm: FakeLLM, session: AsyncSession
) -> None:
    await handle_message(byok.deps, msg("/start", telegram_id=NEW_ID, language_code="en"))
    assert messenger.texts(NEW_ID) == [t("en", "lang.ask")]
    user = await repo.get_user_by_telegram_id(session, NEW_ID)
    assert user is not None and user.status == UserStatus.language
    # the language answer is what starts everything else
    await handle_message(byok.deps, msg("english", telegram_id=NEW_ID, message_id=100))
    texts = messenger.texts(NEW_ID)
    assert texts[-2:] == [t("en", "start.welcome"), t("en", "key.needed")]
    assert fake_llm.calls == []
    session.expunge_all()
    user = await repo.get_user_by_telegram_id(session, NEW_ID)
    assert user is not None and user.status == UserStatus.onboarding and user.language == "en"
    # a second /start before the key: the key line again, still no model call
    await handle_message(byok.deps, msg("/start", telegram_id=NEW_ID, message_id=101))
    assert messenger.texts(NEW_ID)[-1] == t("en", "key.needed") and fake_llm.calls == []
    # the key arrives: saved, deleted, and the interview's first question follows at once
    fake_llm.queue(FakeLLM.text("Your name?"))
    await handle_message(byok.deps, msg(KEY, telegram_id=NEW_ID, message_id=102))
    assert messenger.deletes == [(NEW_ID, 102)]
    assert messenger.texts(NEW_ID)[-2:] == [t("en", "key.saved", last4="WXYZ"), "Your name?"]
    assert len(fake_llm.calls) == 1 and last_user_text(fake_llm).endswith("/start")
    assert "onboarding" in fake_llm.calls[0]["system"][1]["text"].lower()
    assert byok.built == [KEY]
    assert all(KEY not in text for text in await _turn_texts(session))


async def test_admin_falls_back_to_the_server_key(
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
) -> None:
    b = make_byok(engine, clock, fake_llm, messenger, anthropic_api_key=SERVER_KEY)
    await repo.get_or_create_user(
        session, telegram_id=ADMIN_ID, chat_id=ADMIN_ID, now=clock.now(), status=UserStatus.active
    )
    await session.commit()
    fake_llm.queue(FakeLLM.text("ok"))
    await handle_message(b.deps, msg("hi", telegram_id=ADMIN_ID, language_code="en"))
    assert messenger.texts(ADMIN_ID) == ["ok"] and b.built == [SERVER_KEY]
    # the server key never serves a plain keyless user
    await handle_message(b.deps, msg("привет", message_id=101))
    assert messenger.texts(CHAT_ID) == [t("ru", "key.needed")]
    assert len(fake_llm.calls) == 1 and b.built == [SERVER_KEY]


async def test_server_mode_uses_the_server_key_for_everyone(
    engine: AsyncEngine, clock: FakeClock, fake_llm: FakeLLM, messenger: FakeMessenger, user: User
) -> None:
    b = make_byok(
        engine, clock, fake_llm, messenger, llm_key_mode="server", anthropic_api_key=SERVER_KEY
    )
    fake_llm.queue(FakeLLM.text("Понял."), FakeLLM.text("Your name?"))
    await handle_message(b.deps, msg("привет"))
    await handle_message(b.deps, msg("/start", telegram_id=NEW_ID, language_code="en"))
    await handle_message(b.deps, msg("english", telegram_id=NEW_ID, message_id=100))
    assert messenger.texts(CHAT_ID) == ["Понял."]
    # no key line in server mode: the language answer goes straight into the interview
    assert messenger.texts(NEW_ID) == [
        t("en", "lang.ask"),
        t("en", "start.welcome"),
        "Your name?",
    ]
    assert b.built == [SERVER_KEY] and len(fake_llm.calls) == 2


async def test_rejected_key_mid_turn_gets_key_rejected_and_no_retry(
    byok: Byok, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    await handle_message(byok.deps, msg(KEY, message_id=100))
    calls: list[str] = []

    async def rejecting(**kwargs: Any) -> Any:
        calls.append(str(kwargs["purpose"]))
        raise LLMAuthError("api key rejected (401)", status=401)

    fake_llm.message = rejecting  # type: ignore[method-assign]
    await handle_message(byok.deps, msg("омлет", message_id=101))
    assert calls == ["turn"]
    assert messenger.texts(CHAT_ID)[-1] == t("ru", "key.rejected")
    assert t("ru", "err.llm_down") not in messenger.texts(CHAT_ID)


# ------------------------------------------------------------- proactive, decider, nightly


async def test_proactive_engine_skips_a_keyless_user_before_deciding(
    byok: Byok,
    engine: AsyncEngine,
    clock: FakeClock,
    messenger: FakeMessenger,
    user: User,
    session: AsyncSession,
) -> None:
    decider = FakeDecider()
    eng = ProactiveEngine(
        app_mod.make_session_factory(engine),
        decider,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        byok.settings,
        llm_factory=byok.factory,
    )
    try:
        clock.set(at_local(TODAY, "11:05"))
        skipped = await eng.fire(user.id, "no_first_meal")
        assert skipped.status == "skipped" and skipped.reason == "llm_key_missing"
        assert decider.calls == [] and messenger.sent == []
        await repo.set_llm_key(session, user.id, KEY, byok.cipher, now=clock.now())
        await session.commit()
        sent = await eng.fire(user.id, "no_first_meal")
        assert sent.sent and len(decider.calls) == 1
    finally:
        eng.close()


async def test_llm_decider_is_silent_without_a_key(
    byok: Byok, fake_llm: FakeLLM, user: User, session: AsyncSession, clock: FakeClock
) -> None:
    decider = LLMDecider(byok.factory, byok.settings, clock=clock)
    decision = await decider.decide(session, user, fire(), ladder(2), None)
    assert decision.send is False and decision.reason == "llm_key_missing"
    assert fake_llm.calls == []
    await repo.set_llm_key(session, user.id, KEY, byok.cipher, now=clock.now())
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": "11:05. Nothing.", "reason": "x"}))
    decision = await decider.decide(session, user, fire(), ladder(2), None)
    assert decision.send is True and byok.built == [KEY]


async def test_nightly_summary_skips_a_keyless_user(
    byok: Byok,
    engine: AsyncEngine,
    clock: FakeClock,
    fake_llm: FakeLLM,
    user: User,
    session: AsyncSession,
) -> None:
    nightly = app_mod.make_nightly_summary(
        app_mod.make_session_factory(engine), byok.factory, clock
    )
    user_id = user.id
    yesterday = date(2026, 9, 2)
    with capture_logs() as logs:
        await nightly(user_id, yesterday)
    assert fake_llm.calls == []
    assert await repo.get_summary(session, user_id, SummaryKind.day, yesterday) is None
    assert any(entry["event"] == "llm_key_missing" for entry in logs)
    assert any(entry["event"] == "nightly_summary_skipped" for entry in logs)
    await repo.set_llm_key(session, user_id, KEY, byok.cipher, now=clock.now())
    await session.commit()
    fake_llm.queue(FakeLLM.text("not json"), FakeLLM.text("not json"))
    await nightly(user_id, yesterday)
    session.expire_all()
    assert await repo.get_summary(session, user_id, SummaryKind.day, yesterday) is not None
    assert [c["purpose"] for c in fake_llm.calls] == ["summary", "summary"]


# ------------------------------------------------------------------------------- /forget_me


async def test_forget_me_takes_the_key_and_says_so(
    byok: Byok, messenger: FakeMessenger, user: User, session: AsyncSession
) -> None:
    await handle_message(byok.deps, msg(KEY, message_id=100))
    assert await _stored_key(session, user.id, byok.cipher) == KEY
    await handle_message(byok.deps, msg("/forget_me", message_id=101))
    assert "API-ключ" in messenger.texts(CHAT_ID)[-1]
    await handle_callback(byok.deps, cb("forget:yes"))
    done = messenger.texts(CHAT_ID)[-1]
    assert done.startswith("Удалено строк:") and "API-ключ" in done
    session.expunge_all()
    assert await repo.get_user(session, user.id) is None
