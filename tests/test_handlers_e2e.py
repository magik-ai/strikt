"""End to end through ``telegram/handlers.py`` with FakeLLM + FakeMessenger (no aiogram network).

Covers: invite-only /start and the first onboarding turn, a photo that ends in ``log_meal`` and
a pinned card, slot / undo / recalc callbacks, voice with a fake transcriber (and the
``NullTranscriber`` fallback), documents, albums, forwards, links, /today, /invite, /forget_me.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.agent.client import FakeKeyValidator, FakeLLM, FakeLLMFactory
from strikt.agent.tools import build_registry
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher, generate_key
from strikt.db.engine import make_session_factory
from strikt.db.models import Meal, MealSlot, Profile, User, UserStatus
from strikt.events import EventBus
from strikt.memory.daystate import DayStateBuilder
from strikt.telegram.copy import t
from strikt.telegram.daycard import DayCard
from strikt.telegram.handlers import (
    AppDeps,
    CallbackInbound,
    InboundMessage,
    MediaRef,
    handle_callback,
    handle_message,
    parse_command,
)
from strikt.telegram.media import AlbumCollector, MediaTooLargeError
from strikt.telegram.messenger import FakeMessenger
from strikt.telegram.queue import PerChatQueue
from strikt.telegram.voice import NullTranscriber
from tests.conftest import CHAT_ID, NOW, TELEGRAM_ID

NEW_ID = 555_666_777
ADMIN_ID = 42
PHOTO_ID = "photo-1"
PDF_ID = "pdf-1"
VOICE_ID = "voice-1"


# ------------------------------------------------------------------------------------- fakes


@dataclass
class FakeDownloader:
    files: dict[str, bytes] = field(default_factory=dict)
    too_large: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    async def download(self, file_id: str) -> bytes:
        self.calls.append(file_id)
        if file_id in self.too_large:
            raise MediaTooLargeError(30_000_000, 20 * 1024 * 1024)
        return self.files[file_id]


@dataclass
class FakeTranscriber:
    text: str = "200 г творога и 160 г йогурта"
    calls: list[tuple[int, str | None, str | None]] = field(default_factory=list)

    async def transcribe(
        self, data: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> str:
        self.calls.append((len(data), mime, language_hint))
        return self.text


@dataclass
class FakePlanner:
    rescheduled: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    def reschedule_user(self, user: User, profile: Profile | None) -> list[str]:
        self.rescheduled.append(user.id)
        return [f"user:{user.id}:x"]

    def remove_user(self, user_id: int, *, keep_followups: bool = False) -> int:
        self.removed.append(user_id)
        return 1


def jpeg_bytes() -> bytes:
    out = BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(out, format="JPEG")
    return out.getvalue()


PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n2 0 obj << /Type /Page >> endobj\n%%EOF"
)


# ---------------------------------------------------------------------------------- fixtures


@pytest.fixture
def app_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        token_encryption_key=generate_key(),
        allowed_telegram_ids=[TELEGRAM_ID, ADMIN_ID],
        admin_telegram_ids=[ADMIN_ID],
    )


@pytest.fixture
def downloader() -> FakeDownloader:
    return FakeDownloader(files={PHOTO_ID: jpeg_bytes(), PDF_ID: PDF_BYTES, VOICE_ID: b"OggS" * 8})


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
def deps(
    engine: AsyncEngine,
    clock: FakeClock,
    app_settings: Settings,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
    downloader: FakeDownloader,
    transcriber: FakeTranscriber,
    planner: FakePlanner,
) -> AppDeps:
    sessions = make_session_factory(engine)
    albums: AlbumCollector[InboundMessage] = AlbumCollector(debounce_s=0.05)
    return AppDeps(
        settings=app_settings,
        sessions=sessions,
        clock=clock,
        # every user "has a key" here; tests/test_byok_handlers.py covers the key flow itself
        llm_factory=FakeLLMFactory(fake_llm),
        key_validator=FakeKeyValidator(),
        cipher=TokenCipher(app_settings.token_encryption_key.get_secret_value()),
        registry=build_registry(),
        messenger=messenger,
        bus=EventBus(),
        state_provider=DayStateBuilder(clock, app_settings),
        transcriber=transcriber,
        downloader=downloader,
        albums=albums,
        queue=PerChatQueue(),
        card=DayCard(messenger, clock),
        scheduler=planner,
    )


def msg(
    text: str | None = None,
    *,
    telegram_id: int = TELEGRAM_ID,
    chat_id: int | None = None,
    message_id: int = 100,
    media: list[MediaRef] | None = None,
    media_group_id: str | None = None,
    forwarded_from: str | None = None,
    language_code: str | None = "ru",
    received_at: datetime = NOW,
    chat_type: str = "private",
) -> InboundMessage:
    command, args = parse_command(text) if not media else (None, None)
    return InboundMessage(
        telegram_id=telegram_id,
        chat_id=chat_id if chat_id is not None else telegram_id,
        message_id=message_id,
        received_at=received_at,
        text=text,
        language_code=language_code,
        media=media or [],
        media_group_id=media_group_id,
        forwarded_from=forwarded_from,
        command=command,
        command_args=args,
        chat_type=chat_type,
    )


def cb(
    data: str,
    *,
    telegram_id: int = TELEGRAM_ID,
    message_id: int = 1001,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> CallbackInbound:
    return CallbackInbound(
        telegram_id=telegram_id,
        chat_id=chat_id if chat_id is not None else telegram_id,
        message_id=message_id,
        callback_id=f"cb-{data}",
        data=data,
        language_code="ru",
        chat_type=chat_type,
    )


def log_meal_script(fake_llm: FakeLLM, reply: str = "Омлет: 420 ккал / 30 Б / 5 У / 30 Ж.") -> None:
    fake_llm.queue(
        FakeLLM.tool_use(
            "log_meal",
            {
                "items": [
                    {"name": "omelette", "kcal": 420, "protein_g": 30, "carbs_g": 5, "fat_g": 30}
                ]
            },
        ),
        FakeLLM.text(reply),
    )


def last_user_text(fake_llm: FakeLLM, call: int = -1) -> str:
    content = fake_llm.calls[call]["messages"][-1]["content"]
    return "\n".join(str(b.get("text", "")) for b in content if b.get("type") == "text")


def photo_msg(**kwargs: Any) -> InboundMessage:
    return msg(media=[MediaRef("photo", PHOTO_ID, mime="image/jpeg")], **kwargs)


# ------------------------------------------------------------------------------ access control


async def test_unknown_user_gets_one_line_and_no_model_call(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM
) -> None:
    await handle_message(deps, msg("hello", telegram_id=999, language_code="en"))
    assert messenger.texts(999) == [t("en", "err.not_allowed")]
    assert fake_llm.calls == []


async def test_start_without_code_or_allowlist_is_rejected(
    deps: AppDeps, messenger: FakeMessenger, session: AsyncSession
) -> None:
    await handle_message(deps, msg("/start", telegram_id=NEW_ID, language_code="en"))
    assert messenger.texts(NEW_ID) == [t("en", "err.not_allowed")]
    await handle_message(deps, msg("/start nope", telegram_id=NEW_ID, language_code="en"))
    assert messenger.texts(NEW_ID)[-1] == t("en", "err.invite_invalid")
    assert await repo.get_user_by_telegram_id(session, NEW_ID) is None


async def test_start_with_invite_creates_user_and_agent_asks_first_question(
    deps: AppDeps,
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    session: AsyncSession,
    clock: FakeClock,
    planner: FakePlanner,
) -> None:
    invite = await repo.create_invite(session, now=clock.now(), code="WELCOME1")
    await session.commit()
    fake_llm.queue(FakeLLM.text("Как тебя зовут?"))

    await handle_message(deps, msg("/start WELCOME1", telegram_id=NEW_ID, language_code="ru"))

    user = await repo.get_user_by_telegram_id(session, NEW_ID)
    assert user is not None and user.status == UserStatus.onboarding and user.language == "ru"
    assert user.invite_code == "WELCOME1"
    await session.refresh(invite)
    assert invite.used_by == user.id and invite.used_at is not None
    texts = messenger.texts(NEW_ID)
    assert texts[0] == f"{t('ru', 'start.invite_ok')}\n{t('ru', 'start.welcome')}"
    assert texts[1] == "Как тебя зовут?"
    # the synthetic "/start" turn carried the onboarding prompt and checklist
    call = fake_llm.calls[0]
    assert call["purpose"] == "turn" and call["user_id"] == user.id
    assert "onboarding" in call["system"][1]["text"].lower()
    assert last_user_text(fake_llm).endswith("/start")

    # the first answer: the model saves the name, the profile appears, jobs get rescheduled
    fake_llm.queue(
        FakeLLM.tool_use("update_profile", {"fields": {"name": "Илья", "timezone": "Asia/Dubai"}}),
        FakeLLM.text("Записал. Какая цель?"),
    )
    await handle_message(deps, msg("Илья", telegram_id=NEW_ID, message_id=101))
    profile = await repo.get_profile(session, user.id)
    assert profile is not None and profile.name == "Илья"
    await session.refresh(user)
    assert user.timezone == "Asia/Dubai"
    assert messenger.texts(NEW_ID)[-1] == "Записал. Какая цель?"
    assert planner.rescheduled == [user.id]
    # a second /start resumes instead of re-creating
    fake_llm.queue(FakeLLM.text("Продолжаем: цель?"))
    await handle_message(deps, msg("/start", telegram_id=NEW_ID, message_id=102))
    assert messenger.texts(NEW_ID)[-2:] == [t("ru", "start.resume"), "Продолжаем: цель?"]


async def test_start_for_active_user_runs_a_turn_without_welcome(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    fake_llm.queue(FakeLLM.text("Вчера 1 910. Талия просрочена."))
    await handle_message(deps, msg("/start"))
    assert messenger.texts(CHAT_ID) == ["Вчера 1 910. Талия просрочена."]


# ------------------------------------------------------------------------------------ photos


async def test_photo_logs_meal_and_pins_refreshed_card(
    deps: AppDeps,
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    session: AsyncSession,
    downloader: FakeDownloader,
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())

    assert downloader.calls == [PHOTO_ID]
    meals = await repo.list_meals_for_date(session, user.id, NOW.date())
    assert len(meals) == 1 and meals[0].items[0].name == "omelette"
    # the model saw the image as a vision block before the text
    content = fake_llm.calls[0]["messages"][-1]["content"]
    assert [b["type"] for b in content][:2] == ["text", "image"]
    assert content[1]["source"]["media_type"] == "image/jpeg"
    # the pinned card carries the number, the reply carries the slot picker + undo
    assert len(messenger.pins) == 1
    card = next(m for m in messenger.sent if m.silent)
    assert "420" in card.text and "Сегодня" in card.text
    reply = messenger.sent[-1]
    assert reply.text == "Омлет: 420 ккал / 30 Б / 5 У / 30 Ж."
    assert reply.keyboard is not None
    labels = [b.text for row in reply.keyboard for b in row]
    slots = [t("ru", f"btn.{slot}") for slot in ("breakfast", "lunch", "dinner", "snack")]
    assert labels[:4] == slots and t("ru", "btn.undo") in labels
    # the persisted user turn keeps a hash stub, never the bytes
    turns = await repo.last_n_turns(session, user.id, 5)
    assert any("[image: " in str(turn.content) for turn in turns)


async def test_callback_slot_updates_meal_and_edits_card(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())
    meal = (await session.scalars(select(Meal))).one()
    edits_before = len(messenger.edits)

    await handle_callback(deps, cb(f"s:{meal.id}:lunch"))

    await session.refresh(meal)
    assert meal.slot == MealSlot.lunch
    assert messenger.callbacks[-1] == (f"cb-s:{meal.id}:lunch", t("ru", "btn.lunch"))
    assert len(messenger.edits) == edits_before + 1 and "обед" in messenger.edits[-1][2]
    assert len(fake_llm.calls) == 2  # no model call for a button


async def test_callback_undo_soft_deletes_and_refreshes(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())
    meal = (await session.scalars(select(Meal))).one()
    seen: list[str] = []

    async def on_changed(event: Any) -> None:
        seen.append(event.reason)

    from strikt.events import DayStateChanged

    deps.bus.subscribe(DayStateChanged, on_changed)
    await handle_callback(deps, cb(f"undo:{meal.id}"))
    await session.refresh(meal)
    assert meal.deleted_at is not None
    assert messenger.callbacks[-1][1] == t("ru", "btn.undo")
    assert "пока ничего не записано" in messenger.edits[-1][2]
    assert seen == ["undo"]


async def test_callback_recalc_and_close_run_synthetic_turns(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    fake_llm.queue(FakeLLM.text("Пересчитал: 0 ккал."))
    await handle_callback(deps, cb("recalc"))
    assert last_user_text(fake_llm).endswith(t("ru", "synthetic.recalc"))
    assert messenger.texts(CHAT_ID)[-1] == "Пересчитал: 0 ккал."
    fake_llm.queue(FakeLLM.text("Закрыт."))
    await handle_callback(deps, cb("close"))
    assert last_user_text(fake_llm).endswith(t("ru", "synthetic.close"))
    assert messenger.callbacks[-1][0] == "cb-close"


async def test_malformed_callback_is_answered_and_ignored(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    await handle_callback(deps, cb("garbage"))
    assert messenger.callbacks == [("cb-garbage", None)] and fake_llm.calls == []
    await handle_callback(deps, cb("recalc", telegram_id=999))
    assert messenger.callbacks[-1] == ("cb-recalc", None) and fake_llm.calls == []


# -------------------------------------------------------------------------------------- voice


async def test_voice_is_transcribed_into_the_turn(
    deps: AppDeps,
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    transcriber: FakeTranscriber,
) -> None:
    fake_llm.queue(FakeLLM.text("Творог + йогурт: 320 ккал."))
    await handle_message(deps, msg(media=[MediaRef("voice", VOICE_ID, mime="audio/ogg")]))
    assert transcriber.calls == [(32, "audio/ogg", "ru")]
    assert "[voice transcript] 200 г творога и 160 г йогурта" in last_user_text(fake_llm)
    assert messenger.texts(CHAT_ID) == ["Творог + йогурт: 320 ккал."]


async def test_null_transcriber_asks_for_text(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    deps.transcriber = NullTranscriber()
    await handle_message(deps, msg(media=[MediaRef("voice", VOICE_ID, mime="audio/ogg")]))
    assert messenger.texts(CHAT_ID) == [t("ru", "err.transcribe")]
    assert fake_llm.calls == []


# --------------------------------------------------------------------------- documents, albums


async def test_pdf_document_becomes_a_document_block(
    deps: AppDeps, fake_llm: FakeLLM, user: User
) -> None:
    fake_llm.queue(FakeLLM.text("Меню прочитал."))
    await handle_message(
        deps,
        msg(
            "menu",
            media=[MediaRef("document", PDF_ID, mime="application/pdf", filename="menu.pdf")],
        ),
    )
    content = fake_llm.calls[0]["messages"][-1]["content"]
    doc = next(b for b in content if b["type"] == "document")
    assert doc["source"]["media_type"] == "application/pdf" and doc["title"] == "menu.pdf"


async def test_heic_document_goes_through_the_photo_pipeline(
    deps: AppDeps, fake_llm: FakeLLM, user: User, downloader: FakeDownloader
) -> None:
    downloader.files["heic-1"] = jpeg_bytes()  # decodable bytes with a HEIC mime/name
    fake_llm.queue(FakeLLM.text("ok"))
    await handle_message(
        deps, msg(media=[MediaRef("document", "heic-1", mime="image/heic", filename="IMG.HEIC")])
    )
    content = fake_llm.calls[0]["messages"][-1]["content"]
    assert any(b["type"] == "image" for b in content)


async def test_unsupported_document_is_described_not_dropped(
    deps: AppDeps, fake_llm: FakeLLM, user: User, downloader: FakeDownloader
) -> None:
    downloader.files["zip-1"] = b"PK\x03\x04 not an image"
    fake_llm.queue(FakeLLM.text("Пришли PDF."))
    await handle_message(
        deps, msg(media=[MediaRef("document", "zip-1", mime="application/zip", filename="a.zip")])
    )
    assert "unsupported: application/zip" in last_user_text(fake_llm)


async def test_too_large_file_gets_the_limit_line(
    deps: AppDeps,
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    downloader: FakeDownloader,
) -> None:
    downloader.too_large.add(PHOTO_ID)
    await handle_message(deps, photo_msg())
    assert messenger.texts(CHAT_ID) == [t("ru", "err.too_large", mb=20)]
    assert fake_llm.calls == []


async def test_album_parts_become_one_turn_with_two_images(
    deps: AppDeps, fake_llm: FakeLLM, user: User, downloader: FakeDownloader
) -> None:
    downloader.files["photo-2"] = jpeg_bytes()
    fake_llm.queue(FakeLLM.text("Два блюда."))
    first = msg("cart", media=[MediaRef("photo", PHOTO_ID)], media_group_id="g1", message_id=200)
    second = msg(media=[MediaRef("photo", "photo-2")], media_group_id="g1", message_id=201)
    await asyncio.gather(handle_message(deps, first), handle_message(deps, second))
    assert len(fake_llm.calls) == 1
    content = fake_llm.calls[0]["messages"][-1]["content"]
    assert sum(b["type"] == "image" for b in content) == 2
    assert last_user_text(fake_llm).endswith("cart")


async def test_forward_origin_and_links_reach_the_model(
    deps: AppDeps, fake_llm: FakeLLM, user: User
) -> None:
    fake_llm.queue(FakeLLM.text("Смотрю меню."))
    await handle_message(
        deps, msg("вот меню https://kinoya.example/menu.pdf", forwarded_from="Kinoya Dubai")
    )
    text = last_user_text(fake_llm)
    assert "[forwarded from Kinoya Dubai]" in text
    assert "[link] https://kinoya.example/menu.pdf" in text


# --------------------------------------------------------------------------------- commands


async def test_today_reposts_and_pins_the_card(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    await handle_message(deps, msg("/today"))
    assert len(messenger.sent) == 1 and "Сегодня" in messenger.sent[0].text
    assert messenger.pins == [(CHAT_ID, messenger.sent[0].message_id)]
    assert fake_llm.calls == []
    await handle_message(deps, msg("/today", message_id=101))
    assert len(messenger.pins) == 2 and messenger.unpins == [
        (CHAT_ID, messenger.sent[0].message_id)
    ]


async def test_unknown_slash_command_goes_to_the_agent(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    fake_llm.queue(FakeLLM.text("Нет такой команды, но я понял."))
    await handle_message(deps, msg("/settings"))
    assert last_user_text(fake_llm).endswith("/settings")
    assert messenger.texts(CHAT_ID) == ["Нет такой команды, но я понял."]


async def test_invite_is_admin_only(
    deps: AppDeps, messenger: FakeMessenger, session: AsyncSession, clock: FakeClock, user: User
) -> None:
    await handle_message(deps, msg("/invite"))  # the seeded user is not an admin
    assert messenger.sent == []
    admin, _ = await repo.get_or_create_user(
        session, telegram_id=ADMIN_ID, chat_id=ADMIN_ID, now=clock.now(), status=UserStatus.active
    )
    await session.commit()
    await handle_message(deps, msg("/invite", telegram_id=ADMIN_ID, language_code="en"))
    text = messenger.texts(ADMIN_ID)[0]
    assert text.startswith("Invite code: <code>")
    code = text.split("<code>")[1].split("</code>")[0]
    invite = await repo.get_invite(session, code)
    assert invite is not None and invite.created_by == admin.id and invite.used_at is None


async def test_forget_me_flow_deletes_everything(
    deps: AppDeps,
    messenger: FakeMessenger,
    fake_llm: FakeLLM,
    user: User,
    session: AsyncSession,
    planner: FakePlanner,
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())
    card_id = messenger.pins[0][1]

    await handle_message(deps, msg("/forget_me", message_id=101))
    question = messenger.sent[-1]
    assert question.text == t("ru", "forget.question")
    assert question.keyboard is not None
    assert [b.callback_data for row in question.keyboard for b in row] == [
        "forget:yes",
        "forget:no",
    ]

    await handle_callback(deps, cb("forget:no"))
    assert messenger.texts(CHAT_ID)[-1] == t("ru", "forget.cancelled")
    assert await repo.get_user(session, user.id) is not None

    await handle_callback(deps, cb("forget:yes"))
    assert (CHAT_ID, card_id) in messenger.unpins
    done = messenger.texts(CHAT_ID)[-1]
    assert done.startswith("Удалено строк:") and "/start" in done
    rows = int(done.split(":")[1].split(".")[0])
    assert rows >= 6  # user, profile, protocol, day, meal, item, turns
    session.expunge_all()
    assert await repo.get_user(session, user.id) is None
    assert list((await session.scalars(select(Meal))).all()) == []
    assert planner.removed == [user.id]
    # gone means gone: the next message is treated as a stranger's
    await handle_message(deps, msg("hi", message_id=102))
    assert messenger.texts(CHAT_ID)[-1] == t("ru", "err.not_allowed")


# ------------------------------------------------------------------------------- resilience


async def test_model_crash_is_an_honest_line_not_a_traceback(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    # no scripted response: FakeLLM raises AssertionError, which is not an LLMError
    await handle_message(deps, msg("привет"))
    assert messenger.texts(CHAT_ID) == [t("ru", "err.unknown")]
    turns = await repo.last_n_turns(session, user.id, 5)
    assert turns == []  # the failed turn was rolled back


async def test_messages_in_one_chat_are_serialised(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    order: list[str] = []
    real_message = fake_llm.message

    async def slow_message(**kwargs: Any) -> Any:
        order.append("start:" + last_text(kwargs["messages"]))
        await asyncio.sleep(0.02)
        order.append("end:" + last_text(kwargs["messages"]))
        return await real_message(**kwargs)

    def last_text(messages: list[dict[str, Any]]) -> str:
        blocks = messages[-1]["content"]
        return str(blocks[-1]["text"])

    fake_llm.message = slow_message  # type: ignore[method-assign]
    fake_llm.queue(FakeLLM.text("one"), FakeLLM.text("two"))
    await asyncio.gather(
        handle_message(deps, msg("first", message_id=1)),
        handle_message(deps, msg("second", message_id=2)),
    )
    assert order == ["start:first", "end:first", "start:second", "end:second"]
    assert messenger.texts(CHAT_ID) == ["one", "two"]
    assert ("typing" in {a for _, a in messenger.actions}) or messenger.actions == []


# ------------------------------------------------------------------------------- group chats

GROUP_ID = -1_001_234_567_890


async def test_group_chat_updates_are_dropped_and_never_rebind_the_chat(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    from aiogram.types import Chat, Message as TgMessage, User as TgUser

    from strikt.telegram.handlers import from_message

    tg = TgMessage(
        message_id=5,
        date=NOW,
        chat=Chat(id=GROUP_ID, type="supergroup", title="friends"),
        from_user=TgUser(id=TELEGRAM_ID, is_bot=False, first_name="I", language_code="ru"),
        text="/start@StriktBot",
    )
    inbound = from_message(tg, received_at=NOW)
    assert inbound.chat_type == "supergroup" and not inbound.private
    await handle_message(deps, inbound)
    # nothing sent anywhere, no model call, chat_id untouched
    assert messenger.sent == [] and fake_llm.calls == []
    await session.refresh(user)
    assert user.chat_id == CHAT_ID
    # a plain group message and a stranger in the group are equally silent
    await handle_message(deps, msg("hello", chat_id=GROUP_ID, chat_type="supergroup"))
    await handle_message(deps, msg("hi", telegram_id=999, chat_id=GROUP_ID, chat_type="supergroup"))
    assert messenger.sent == [] and fake_llm.calls == []
    # a callback from a group is answered (the client stops spinning) and ignored
    await handle_callback(deps, cb("recalc", chat_id=GROUP_ID, chat_type="supergroup"))
    assert messenger.callbacks == [("cb-recalc", None)] and fake_llm.calls == []
    # a private-looking type with a foreign chat id is not private either
    assert not msg("x", chat_id=GROUP_ID).private
    # the private chat still works afterwards
    fake_llm.queue(FakeLLM.text("ok"))
    await handle_message(deps, msg("привет"))
    assert messenger.texts(CHAT_ID) == ["ok"]


# ------------------------------------------------------------------- callbacks under a running turn


async def test_callback_is_answered_before_waiting_for_a_busy_chat(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())
    meal = (await session.scalars(select(Meal))).one()
    release = asyncio.Event()

    async def long_turn() -> None:
        await release.wait()

    holder = asyncio.create_task(deps.queue.run(CHAT_ID, long_turn))
    await asyncio.sleep(0)
    assert deps.queue.busy(CHAT_ID)
    tap = asyncio.create_task(handle_callback(deps, cb(f"s:{meal.id}:lunch")))
    await asyncio.sleep(0.05)
    assert not tap.done()
    assert messenger.callbacks[-1] == (f"cb-s:{meal.id}:lunch", None)  # answered while waiting
    release.set()
    await asyncio.gather(holder, tap)
    await session.refresh(meal)
    assert meal.slot == MealSlot.lunch  # the action still ran, in order


async def test_second_undo_tap_is_a_quiet_no_op(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User, session: AsyncSession
) -> None:
    log_meal_script(fake_llm)
    await handle_message(deps, photo_msg())
    meal = (await session.scalars(select(Meal))).one()
    await handle_callback(deps, cb(f"undo:{meal.id}"))
    await handle_callback(deps, cb(f"undo:{meal.id}"))
    assert messenger.callbacks[-1][1] == t("ru", "btn.undo_done")
    assert t("ru", "err.unknown") not in messenger.texts(CHAT_ID)


# --------------------------------------------------------------------------- send fallbacks


async def test_send_escapes_only_on_parse_errors(
    deps: AppDeps, messenger: FakeMessenger, user: User
) -> None:
    from strikt.telegram.handlers import _send

    real_send = messenger.send
    failures: list[Exception] = [RuntimeError("Bad Request: can't parse entities: unclosed <b>")]

    async def flaky(chat_id: int, text: str, **kwargs: Any) -> int:
        if failures:
            raise failures.pop()
        return await real_send(chat_id, text, **kwargs)

    messenger.send = flaky  # type: ignore[method-assign]
    await _send(deps, CHAT_ID, "<b>x")
    assert messenger.texts(CHAT_ID)[-1] == "&lt;b&gt;x"
    failures.append(RuntimeError("Connection reset"))
    await _send(deps, CHAT_ID, "<b>fine</b>")
    assert messenger.texts(CHAT_ID)[-1] == "<b>fine</b>"  # a network blip keeps the markup


async def test_transcription_failure_has_its_own_copy(
    deps: AppDeps, messenger: FakeMessenger, fake_llm: FakeLLM, user: User
) -> None:
    from strikt.telegram.voice import TranscriptionError

    class Failing:
        async def transcribe(
            self, data: bytes, *, mime: str | None = None, language_hint: str | None = None
        ) -> str:
            raise TranscriptionError("upstream 500")

    deps.transcriber = Failing()  # type: ignore[assignment]
    await handle_message(deps, msg(media=[MediaRef("voice", VOICE_ID, mime="audio/ogg")]))
    assert messenger.texts(CHAT_ID) == [t("ru", "err.transcribe_failed")]
    assert fake_llm.calls == []
