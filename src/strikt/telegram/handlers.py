"""Telegram handlers, independent of aiogram's network layer (PLAN §7, §11; brief §2, §5.7).

``bot.py`` turns every aiogram update into an :class:`InboundMessage` / :class:`CallbackInbound`
and calls :func:`handle_message` / :func:`handle_callback`. Everything below works against the
``Messenger``, ``Downloader`` and ``Transcriber`` protocols, so the end-to-end tests drive these
functions with fakes and never touch Telegram.

What happens to a message:

1. Album parts (``media_group_id``) are gathered by ``AlbumCollector``; one ``InboundMessage``
   with every photo continues, the others stop.
2. Commands: ``/start [code]`` (invite-only), ``/today``, ``/forget_me``, ``/invite`` (admins).
   Unknown users get one line (``err.not_allowed``) and nothing else. Updates from anything but
   a private chat (a group the bot was added to, a channel) are dropped before that: the coach
   never rebinds ``user.chat_id`` to a group, so the pinned card, proactive nudges and health
   data can only ever land in the user's own chat.
3. Bring-your-own-key, before anything else touches the text: a message carrying an Anthropic
   key (``sk-ant-…``) is checked with one cheap call, stored encrypted, deleted from the chat and
   answered with code-rendered copy — it is never a conversation turn. Then the user's own
   ``LLM`` is resolved (``LLMResolver.for_user``); a user without a key gets the key walkthrough
   (``key.needed``, or ``key.help`` when they asked about the key) and no model call.
4. Everything else becomes an ``Incoming`` (largest photo, image/PDF documents through
   ``media.py``, voice/audio through the ``Transcriber``, forwarded origin, links) and runs
   through ``agent.loop.run_turn`` under the chat's ``PerChatQueue`` lock with a typing heartbeat.
   The loop refreshes the pinned card when the state changed; profile-changing tools reschedule
   the user's proactive jobs.

Callback buttons: slot → ``update_meal`` directly (no model call), undo → ``undo_last`` /
``delete_meal``, recalculate / close → a synthetic user message through the agent (so the
Reflexion check runs), ``forget:yes`` → ``privacy.delete_everything``.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog

from strikt.agent.loop import TurnDeps, TurnResult, run_turn
from strikt.agent.tools.registry import ToolContext
from strikt.core.clock import coaching_today, ensure_utc
from strikt.core.types import Attachment, Button, Incoming
from strikt.db import repo
from strikt.db.models import SecretService, User, UserStatus
from strikt.events import DayStateChanged
from strikt.keycheck import check_secret
from strikt.privacy import delete_everything
from strikt.telegram.copy import detect_lang, resolve_lang, t
from strikt.telegram.keyboards import (
    Callback,
    forget_confirm,
    language_picker,
    parse_callback,
)
from strikt.telegram.keys import (
    extract_key,
    extract_openai_key,
    looks_like_usda_key,
    mentions_key,
)
from strikt.telegram.media import MediaError, MediaTooLargeError, prepare_document, prepare_image
from strikt.telegram.voice import TranscriptionError

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.agent.client import KeyCheck, KeyValidator, LLMClient, LLMResolver
    from strikt.agent.tools.registry import Registry
    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import Profile
    from strikt.events import EventBus
    from strikt.proactive.types import StateProvider
    from strikt.telegram.daycard import DayCard
    from strikt.telegram.media import AlbumCollector, Downloader
    from strikt.telegram.messenger import Messenger
    from strikt.telegram.queue import PerChatQueue
    from strikt.telegram.voice import Transcriber, TranscriberResolver

log = structlog.get_logger(__name__)

MediaKind = Literal["photo", "document", "voice", "audio", "video_note"]
AUDIO_KINDS: frozenset[str] = frozenset({"voice", "audio", "video_note"})
#: Tools after which the user's proactive jobs are recomputed (wake/bed times, timezone, style).
PROFILE_TOOLS: frozenset[str] = frozenset(
    {"update_profile", "finish_onboarding", "set_coaching_intensity"}
)
COMMANDS: frozenset[str] = frozenset({"start", "today", "forget_me", "invite"})
START_TEXT = "/start"
HEARTBEAT_S = 4.0

_COMMAND_RE = re.compile(r"^/([A-Za-z0-9_]{1,32})(?:@\w+)?(?:\s+(.*))?$", re.DOTALL)
_LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


# ---------------------------------------------------------------------------------- inbound


@dataclass(frozen=True)
class MediaRef:
    """One downloadable file on a Telegram message."""

    kind: MediaKind
    file_id: str
    mime: str | None = None
    filename: str | None = None
    size: int | None = None


PRIVATE_CHAT = "private"


def chat_type_of(chat: Any) -> str:
    """``chat.type`` as a plain string (aiogram may hand over a ``str`` enum)."""
    value = getattr(chat, "type", None)
    if value is None:
        return PRIVATE_CHAT
    return str(getattr(value, "value", value))


def is_private_chat(chat_type: str, chat_id: int, telegram_id: int) -> bool:
    """A private chat with the sender: Telegram's ``chat.type`` says so and the chat id is the
    sender's id (belt and braces: a group id is negative and never equals a user id)."""
    return chat_type == PRIVATE_CHAT and chat_id == telegram_id


@dataclass
class InboundMessage:
    """A Telegram message reduced to what the handlers need (built by ``from_message``)."""

    telegram_id: int
    chat_id: int
    message_id: int
    received_at: datetime
    text: str | None = None
    language_code: str | None = None
    media: list[MediaRef] = field(default_factory=list)
    media_group_id: str | None = None
    forwarded_from: str | None = None
    command: str | None = None
    command_args: str | None = None
    chat_type: str = PRIVATE_CHAT

    @property
    def lang(self) -> str:
        return resolve_lang(self.language_code)

    @property
    def private(self) -> bool:
        return is_private_chat(self.chat_type, self.chat_id, self.telegram_id)


@dataclass(frozen=True)
class CallbackInbound:
    telegram_id: int
    chat_id: int
    message_id: int | None
    callback_id: str
    data: str | None
    language_code: str | None = None
    chat_type: str = PRIVATE_CHAT

    @property
    def private(self) -> bool:
        return is_private_chat(self.chat_type, self.chat_id, self.telegram_id)


def parse_command(text: str | None) -> tuple[str | None, str | None]:
    """``"/start@StriktBot ab12"`` → ``("start", "ab12")``; non-commands → ``(None, None)``."""
    if not text or not text.startswith("/"):
        return None, None
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None, None
    args = (match.group(2) or "").strip()
    return match.group(1).lower(), args or None


def _origin_name(message: Message) -> str | None:
    origin = message.forward_origin
    if origin is not None:
        sender_user = getattr(origin, "sender_user", None)
        if sender_user is not None:
            return str(sender_user.full_name)
        for attr in ("sender_user_name", "author_signature"):
            value = getattr(origin, attr, None)
            if value:
                return str(value)
        for attr in ("chat", "sender_chat"):
            chat = getattr(origin, attr, None)
            if chat is not None:
                return str(chat.title or chat.username or chat.id)
    if message.forward_sender_name:
        return str(message.forward_sender_name)
    if message.forward_from is not None:
        return str(message.forward_from.full_name)
    if message.forward_from_chat is not None:
        chat = message.forward_from_chat
        return str(chat.title or chat.username or chat.id)
    return None


def from_message(message: Message, *, received_at: datetime | None = None) -> InboundMessage:
    """Normalise an aiogram ``Message`` (text, caption, largest photo, document, voice, origin)."""
    sender = message.from_user
    telegram_id = sender.id if sender is not None else message.chat.id
    media: list[MediaRef] = []
    if message.photo:
        largest = message.photo[-1]
        media.append(MediaRef("photo", largest.file_id, mime="image/jpeg", size=largest.file_size))
    if message.document is not None:
        doc = message.document
        media.append(
            MediaRef(
                "document",
                doc.file_id,
                mime=doc.mime_type,
                filename=doc.file_name,
                size=doc.file_size,
            )
        )
    if message.voice is not None:
        media.append(
            MediaRef(
                "voice",
                message.voice.file_id,
                mime=message.voice.mime_type or "audio/ogg",
                size=message.voice.file_size,
            )
        )
    if message.audio is not None:
        media.append(
            MediaRef(
                "audio",
                message.audio.file_id,
                mime=message.audio.mime_type,
                filename=message.audio.file_name,
                size=message.audio.file_size,
            )
        )
    if message.video_note is not None:
        media.append(
            MediaRef(
                "video_note",
                message.video_note.file_id,
                mime="video/mp4",
                size=message.video_note.file_size,
            )
        )
    text = message.text if message.text is not None else message.caption
    command, args = parse_command(message.text) if not media else (None, None)
    when = received_at or (ensure_utc(message.date) if isinstance(message.date, datetime) else None)
    return InboundMessage(
        telegram_id=telegram_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        received_at=when or datetime.now(UTC),
        text=text,
        language_code=sender.language_code if sender is not None else None,
        media=media,
        media_group_id=message.media_group_id,
        forwarded_from=_origin_name(message),
        command=command,
        command_args=args,
        chat_type=chat_type_of(message.chat),
    )


def from_callback(query: CallbackQuery) -> CallbackInbound:
    message = query.message
    chat_id = message.chat.id if message is not None else query.from_user.id
    chat_type = chat_type_of(message.chat) if message is not None else PRIVATE_CHAT
    message_id = getattr(message, "message_id", None) if message is not None else None
    return CallbackInbound(
        telegram_id=query.from_user.id,
        chat_id=chat_id,
        message_id=int(message_id) if message_id is not None else None,
        callback_id=query.id,
        data=query.data,
        language_code=query.from_user.language_code,
        chat_type=chat_type,
    )


def merge_album(parts: Sequence[InboundMessage]) -> InboundMessage:
    """One logical message from the parts of a media group (first caption wins)."""
    first = parts[0]
    text = next((p.text for p in parts if p.text and p.text.strip()), None)
    forwarded = next((p.forwarded_from for p in parts if p.forwarded_from), None)
    media = [ref for part in parts for ref in part.media]
    return InboundMessage(
        telegram_id=first.telegram_id,
        chat_id=first.chat_id,
        message_id=first.message_id,
        received_at=first.received_at,
        text=text,
        language_code=first.language_code,
        media=media,
        media_group_id=None,
        forwarded_from=forwarded,
        chat_type=first.chat_type,
    )


# ------------------------------------------------------------------------------------- deps


class JobPlanner(Protocol):
    """The slice of ``ProactiveScheduler`` the handlers use."""

    def reschedule_user(self, user: User, profile: Profile | None) -> list[str]: ...

    def remove_user(self, user_id: int, *, keep_followups: bool = False) -> int: ...


SessionFactory = Callable[[], "AsyncSession"]


@dataclass
class AppDeps:
    """Everything the handlers need; built once by ``app.build_runtime`` (fakes in tests)."""

    settings: Settings
    sessions: SessionFactory
    clock: Clock
    llm_factory: LLMResolver
    registry: Registry
    messenger: Messenger
    bus: EventBus
    state_provider: StateProvider
    transcriber: Transcriber
    downloader: Downloader
    albums: AlbumCollector[InboundMessage]
    queue: PerChatQueue
    card: DayCard | None = None
    scheduler: JobPlanner | None = None
    integrations: Mapping[Any, Any] | None = None
    services: dict[str, Any] = field(default_factory=dict)
    #: Checks a pasted key with one cheap call; without one the key is stored unchecked.
    key_validator: KeyValidator | None = None
    #: Encrypts the stored key (``TOKEN_ENCRYPTION_KEY``); required to accept a key at all.
    cipher: TokenCipher | None = None
    #: Picks the transcriber for a user (their own OpenAI key first). Without one every
    #: voice note goes through ``transcriber``.
    transcribers: TranscriberResolver | None = None

    def tool_services(self) -> dict[str, Any]:
        """The service bag handed to tool handlers (``llm``/``bus`` are added by the loop)."""
        services: dict[str, Any] = {"messenger": self.messenger}
        if self.cipher is not None:
            # the food tools decrypt the user's own USDA key with it
            services["cipher"] = self.cipher
        if self.integrations is not None:
            services["integrations"] = self.integrations
        if self.card is not None:
            services["card"] = self.card
        services.update(self.services)
        return services


# ------------------------------------------------------------------------------------ helpers


def is_parse_error(exc: BaseException) -> bool:
    """Telegram rejected the HTML body (``can't parse entities``), as opposed to a network or
    rate-limit failure where the text itself is fine."""
    return "parse entities" in str(exc).lower()


async def _send(
    deps: AppDeps,
    chat_id: int,
    text: str,
    *,
    keyboard: Sequence[Sequence[Button]] | None = None,
    reply_to: int | None = None,
) -> int | None:
    """Send and never raise: a rejected HTML body is retried escaped (so the user never sees
    literal ``<b>`` tags after a transient error), any other failure is retried once as is."""
    try:
        return await deps.messenger.send(chat_id, text, keyboard=keyboard, reply_to=reply_to)
    except Exception as exc:
        parse_error = is_parse_error(exc)
        log.warning("send_failed_retrying", chat_id=chat_id, error=repr(exc), escaped=parse_error)
        retry_text = html.escape(text, quote=False) if parse_error else text
    try:
        return await deps.messenger.send(chat_id, retry_text, keyboard=keyboard, reply_to=reply_to)
    except Exception as exc:
        log.error("send_failed", chat_id=chat_id, error=repr(exc))
        return None


def _typing(deps: AppDeps, chat_id: int) -> Callable[[], Any]:
    async def beat() -> None:
        await deps.messenger.chat_action(chat_id, "typing")

    return beat


async def _load_user(deps: AppDeps, telegram_id: int) -> User | None:
    async with deps.sessions() as session:
        return await repo.get_user_by_telegram_id(session, telegram_id)


def _links(text: str | None) -> list[Attachment]:
    if not text:
        return []
    seen: list[str] = []
    for match in _LINK_RE.finditer(text):
        url = match.group(0).rstrip(".,;:)")
        if url not in seen:
            seen.append(url)
    return [Attachment(kind="link", file_id=url) for url in seen]


# --------------------------------------------------------------------------------- messages


async def handle_message(deps: AppDeps, inbound: InboundMessage) -> None:
    """Entry point for every non-callback update. Never raises."""
    if not inbound.private:
        log.info("update_ignored_not_private", chat_id=inbound.chat_id, chat_type=inbound.chat_type)
        return
    if inbound.media_group_id:
        key = f"{inbound.chat_id}:{inbound.media_group_id}"
        parts = await deps.albums.collect(key, inbound, order=inbound.message_id)
        if parts is None:
            return
        inbound = merge_album(parts)
    try:
        await deps.queue.run(
            inbound.chat_id,
            lambda: _dispatch_message(deps, inbound),
            heartbeat=_typing(deps, inbound.chat_id),
            heartbeat_interval=HEARTBEAT_S,
        )
    except Exception:
        log.exception("handle_message_failed", chat_id=inbound.chat_id)
        await _send(deps, inbound.chat_id, t(inbound.lang, "err.unknown"))


async def _dispatch_message(deps: AppDeps, inbound: InboundMessage) -> None:
    if inbound.command == "start":
        await handle_start(deps, inbound)
        return
    user = await _load_user(deps, inbound.telegram_id)
    if user is None:
        log.info("message_from_unknown_user", telegram_id=inbound.telegram_id)
        await _send(deps, inbound.chat_id, t(inbound.lang, "err.not_allowed"))
        return
    if (
        user.status == UserStatus.language
        and inbound.command is None
        # a pasted key is a key whichever question is on the table: taking it for the language
        # answer would leave the secret sitting in the chat, unchecked and unstored
        and extract_key(inbound.text) is None
        and extract_openai_key(inbound.text) is None
    ):
        await handle_language_message(deps, user, inbound)
        return
    if inbound.command == "today":
        await handle_today(deps, user.id)
    elif inbound.command == "forget_me":
        await handle_forget_me(deps, user)
    elif inbound.command == "invite":
        await handle_invite(deps, user, inbound)
    else:
        await handle_user_message(deps, user.id, inbound)


# ------------------------------------------------------------------------------------ /start


async def handle_start(deps: AppDeps, inbound: InboundMessage) -> None:
    """Invite-only entry: allowed ids or a valid code create the user, who is then asked which
    language to speak. Everything after that lives in ``_start_after_language``."""
    lang = inbound.lang
    now = ensure_utc(inbound.received_at)
    code = (inbound.command_args or "").strip() or None
    async with deps.sessions() as session:
        user = await repo.get_user_by_telegram_id(session, inbound.telegram_id)
        created = False
        if user is None:
            invite = None
            if not deps.settings.is_allowed(inbound.telegram_id):
                invite = await repo.get_invite(session, code) if code else None
                if invite is None or invite.used_at is not None:
                    key = "err.invite_invalid" if code else "err.not_allowed"
                    log.info(
                        "start_rejected", telegram_id=inbound.telegram_id, with_code=bool(code)
                    )
                    await _send(deps, inbound.chat_id, t(lang, key))
                    return
            user, created = await repo.get_or_create_user(
                session,
                telegram_id=inbound.telegram_id,
                chat_id=inbound.chat_id,
                now=now,
                language=inbound.language_code or "en",
                timezone="UTC",
                status=UserStatus.language,
                invite_code=invite.code if invite is not None else None,
            )
            if invite is not None:
                await repo.consume_invite(session, invite.code, used_by=user.id, now=now)
        else:
            if inbound.private:  # never rebind the coach's chat to a group
                user.chat_id = inbound.chat_id
            user.last_seen_at = now
        await session.commit()
        user_id = user.id
        status = user.status
        used_invite = (
            created and code is not None and not deps.settings.is_allowed(inbound.telegram_id)
        )
        user_lang = resolve_lang(user.language)

    if created:
        log.info("user_created", user_id=user_id, telegram_id=inbound.telegram_id)
    if status == UserStatus.language:
        # the language is the first thing asked and the only thing answered until it is set
        lines = [t(user_lang, "start.invite_ok")] if used_invite else []
        lines.append(t(user_lang, "lang.ask"))
        await _send(
            deps,
            inbound.chat_id,
            "\n\n".join(lines),
            keyboard=language_picker(user_lang),
        )
        return
    await _start_after_language(
        deps, user_id, inbound, welcome=False, resume=status == UserStatus.onboarding
    )


async def handle_language_message(deps: AppDeps, user: User, inbound: InboundMessage) -> None:
    """The typed answer to ``lang.ask``. A named language wins over the alphabet it is written
    in; anything the bot cannot read at all gets the question again."""
    chosen = detect_lang(inbound.text)
    if chosen is None:
        current = resolve_lang(user.language)
        await _send(
            deps, inbound.chat_id, t(current, "lang.ask"), keyboard=language_picker(current)
        )
        return
    await set_language(deps, user.id, chosen, inbound)


async def set_language(deps: AppDeps, user_id: int, language: str, inbound: InboundMessage) -> None:
    """Store the answer and carry on with /start in that language."""
    async with deps.sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            return
        user.language = language
        if user.status == UserStatus.language:
            user.status = UserStatus.onboarding
        await session.commit()
    log.info("language_set", user_id=user_id, language=language)
    await _start_after_language(deps, user_id, inbound, welcome=True, resume=False)


async def _start_after_language(
    deps: AppDeps, user_id: int, inbound: InboundMessage, *, welcome: bool, resume: bool
) -> None:
    """What /start does once the language is known: the welcome line, then either the key
    walkthrough (no key stored) or the interview's first question. A keyless user never sees
    the resume line: there is nothing to resume until the key is in."""
    async with deps.sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            return
        lang = resolve_lang(user.language)
        llm = await deps.llm_factory.for_user(session, user)
    if welcome:
        await _send(deps, inbound.chat_id, t(lang, "start.welcome"))
    if llm is None:
        # bring-your-own-key: the interview starts once the key is in (``handle_key_message``)
        await _send(deps, inbound.chat_id, t(lang, "key.needed"))
        return
    if resume:
        await _send(deps, inbound.chat_id, t(lang, "start.resume"))
    synthetic = InboundMessage(
        telegram_id=inbound.telegram_id,
        chat_id=inbound.chat_id,
        message_id=inbound.message_id,
        received_at=ensure_utc(inbound.received_at),
        text=START_TEXT,
        language_code=inbound.language_code,
    )
    await handle_user_message(deps, user_id, synthetic)


# ------------------------------------------------------------------------------- /today etc.


async def handle_today(deps: AppDeps, user_id: int) -> None:
    async with deps.sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            return
        state = await deps.state_provider.day_state(
            session, user, await _today(deps, session, user)
        )
        if deps.card is not None:
            await deps.card.repost(session, user, state)
        else:
            from strikt.telegram.render import render_day_card

            await _send(deps, user.chat_id, render_day_card(state, user.language, user.timezone))
        await session.commit()


async def handle_forget_me(deps: AppDeps, user: User) -> None:
    lang = resolve_lang(user.language)
    await _send(deps, user.chat_id, t(lang, "forget.question"), keyboard=forget_confirm(lang))


async def handle_invite(deps: AppDeps, user: User, inbound: InboundMessage) -> None:
    """Admins mint a code; anyone else is ignored (the only admin command, PLAN §11)."""
    if not deps.settings.is_admin(inbound.telegram_id):
        log.info("invite_denied", telegram_id=inbound.telegram_id)
        return
    async with deps.sessions() as session:
        invite = await repo.create_invite(
            session, now=ensure_utc(inbound.received_at), created_by=user.id
        )
        await session.commit()
        code = invite.code
    lang = resolve_lang(user.language)
    await _send(
        deps, user.chat_id, t(lang, "invite.created", code=f"<code>{html.escape(code)}</code>")
    )
    log.info("invite_created", by=user.id)


# -------------------------------------------------------------------------------- the turn


async def build_incoming(
    deps: AppDeps,
    user: User,
    inbound: InboundMessage,
    transcriber: Transcriber | None = None,
) -> Incoming | None:
    """Download and prepare every attachment. ``None`` when the user was told what went wrong."""
    lang = resolve_lang(user.language)
    attachments: list[Attachment] = []
    silent_audio = False
    transcription_failed = False
    for ref in inbound.media:
        try:
            data = await deps.downloader.download(ref.file_id)
            if ref.kind == "photo":
                attachments.append(await prepare_image(data, ref.mime, ref.filename))
            elif ref.kind == "document":
                attachments.append(await prepare_document(data, ref.mime, ref.filename))
            else:
                transcript = await (transcriber or deps.transcriber).transcribe(
                    data, mime=ref.mime, language_hint=user.language
                )
                if not transcript.strip():
                    silent_audio = True
                    continue
                attachments.append(
                    Attachment(
                        kind="voice", file_id=ref.file_id, mime=ref.mime, text=transcript.strip()
                    )
                )
        except MediaTooLargeError as exc:
            await _send(deps, inbound.chat_id, t(lang, "err.too_large", mb=exc.limit_mb))
            return None
        except MediaError as exc:
            log.info("media_rejected", user_id=user.id, error=str(exc))
            await _send(deps, inbound.chat_id, t(lang, "err.media"))
            return None
        except TranscriptionError as exc:
            log.warning("transcription_failed", user_id=user.id, error=str(exc))
            silent_audio = True
            transcription_failed = True
    text = inbound.text.strip() if inbound.text and inbound.text.strip() else None
    if silent_audio and not attachments and text is None:
        key = "err.transcribe_failed" if transcription_failed else "err.transcribe"
        await _send(deps, inbound.chat_id, t(lang, key))
        return None
    attachments.extend(_links(text))
    return Incoming(
        user_id=user.id,
        chat_id=inbound.chat_id,
        message_id=inbound.message_id,
        text=text,
        attachments=attachments,
        forwarded_from=inbound.forwarded_from,
        received_at=ensure_utc(inbound.received_at),
    )


async def handle_user_message(deps: AppDeps, user_id: int, inbound: InboundMessage) -> None:
    """A pasted key is stored (never a turn); a keyless user gets the walkthrough; otherwise
    prepare the ``Incoming`` and run the agent turn on the user's own LLM."""
    async with deps.sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            return
        key = extract_key(inbound.text)
        if key is not None:
            await handle_key_message(deps, session, user, inbound, key)
            return
        optional = extract_openai_key(inbound.text)
        if optional is not None:
            await handle_secret_message(deps, session, user, inbound, "openai", optional)
            return
        if user.awaiting_secret:
            pending = user.awaiting_secret
            candidate = (inbound.text or "").strip()
            if _looks_like_secret(pending, candidate):
                await handle_secret_message(deps, session, user, inbound, pending, candidate)
                return
            # they changed their mind, or answered something else: stop waiting and let the
            # message be an ordinary turn
            await repo.set_awaiting_secret(session, user.id, None)
            await session.commit()
        llm = await resolve_llm(deps, session, user, inbound.text)
        if llm is None:
            return
        speech = None
        if deps.transcribers is not None:
            speech = await deps.transcribers.for_user(session, user)
        incoming = await build_incoming(deps, user, inbound, speech)
        if incoming is None:
            return
        await run_agent_turn(deps, session, user, incoming, llm)


async def resolve_llm(
    deps: AppDeps, session: AsyncSession, user: User, text: str | None
) -> LLMClient | None:
    """The LLM billed to this user's key, or None after sending the key walkthrough (``user``
    mode, no key stored): ``key.help`` when the message asks about the key, else ``key.needed``.
    Nothing is downloaded, transcribed or sent to a model for a keyless user."""
    llm = await deps.llm_factory.for_user(session, user)
    if llm is None:
        copy_key = "key.help" if mentions_key(text) else "key.needed"
        await _send(deps, user.chat_id, t(resolve_lang(user.language), copy_key))
    return llm


async def run_agent_turn(
    deps: AppDeps, session: AsyncSession, user: User, incoming: Incoming, llm: LLMClient
) -> TurnResult | None:
    """One ``run_turn`` on ``llm`` (the user's own key; the tools' ``services["llm"]`` is the
    same client, so ``close_day`` and ``web_research`` bill the same key); sends the replies and
    reschedules jobs."""
    turn_deps = TurnDeps(
        session=session,
        user=user,
        llm=llm,
        registry=deps.registry,
        clock=deps.clock,
        settings=deps.settings,
        bus=deps.bus,
        state_provider=deps.state_provider,
        services=deps.tool_services(),
        card=deps.card,
    )
    try:
        result = await run_turn(turn_deps, incoming)
    except Exception:
        log.exception("turn_failed", user_id=user.id)
        await session.rollback()
        await _send(deps, user.chat_id, t(resolve_lang(user.language), "err.unknown"))
        return None
    for outgoing in result.outgoings:
        await _send(
            deps,
            user.chat_id,
            outgoing.text,
            keyboard=outgoing.keyboard,
            reply_to=outgoing.reply_to,
        )
    if deps.scheduler is not None and PROFILE_TOOLS.intersection(result.tools_used):
        try:
            profile = await repo.get_profile(session, user.id)
            deps.scheduler.reschedule_user(user, profile)
        except Exception as exc:
            log.warning("reschedule_failed", user_id=user.id, error=repr(exc))
    return result


# -------------------------------------------------------------------------------- callbacks


async def handle_callback(deps: AppDeps, cb: CallbackInbound) -> None:
    """Inline buttons. Always answers the callback (clients spin until we do).

    When the chat is busy with a running turn the callback is answered *before* waiting for the
    lock (Telegram expires an unanswered query in well under a minute; a photo turn can take
    longer); the action still runs in order and confirms itself through the card refresh."""
    if not cb.private:
        log.info("callback_ignored_not_private", chat_id=cb.chat_id, chat_type=cb.chat_type)
        await deps.messenger.answer_callback(cb.callback_id)
        return
    parsed = parse_callback(cb.data)
    user = await _load_user(deps, cb.telegram_id)
    if parsed is None or user is None:
        log.info("callback_ignored", data=cb.data, known_user=user is not None)
        await deps.messenger.answer_callback(cb.callback_id)
        return
    if deps.queue.busy(cb.chat_id):
        await deps.messenger.answer_callback(cb.callback_id)
    try:
        await deps.queue.run(
            cb.chat_id,
            lambda: _dispatch_callback(deps, user.id, cb, parsed),
            heartbeat=_typing(deps, cb.chat_id),
            heartbeat_interval=HEARTBEAT_S,
        )
    except Exception:
        log.exception("handle_callback_failed", chat_id=cb.chat_id, data=cb.data)
        await deps.messenger.answer_callback(cb.callback_id)
        await _send(deps, cb.chat_id, t(resolve_lang(user.language), "err.unknown"))


async def _dispatch_callback(
    deps: AppDeps, user_id: int, cb: CallbackInbound, parsed: Callback
) -> None:
    async with deps.sessions() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            await deps.messenger.answer_callback(cb.callback_id)
            return
        lang = resolve_lang(user.language)
        if parsed.kind == "lang" and parsed.language is not None:
            await deps.messenger.answer_callback(cb.callback_id)
            await set_language(
                deps,
                user.id,
                parsed.language,
                InboundMessage(
                    telegram_id=cb.telegram_id,
                    chat_id=cb.chat_id,
                    message_id=cb.message_id or 0,
                    received_at=ensure_utc(deps.clock.now()),
                ),
            )
        elif parsed.kind == "undo" and parsed.meal_id is not None:
            await _callback_undo(deps, session, user, cb, parsed.meal_id)
        else:  # forget: the only kind left, and parse_callback guarantees the answer
            await deps.messenger.answer_callback(cb.callback_id)
            if parsed.answer:
                await forget_user(deps, session, user)
            else:
                await _send(deps, cb.chat_id, t(lang, "forget.cancelled"))


def _tool_ctx(
    deps: AppDeps, session: AsyncSession, user: User, llm: LLMClient | None = None
) -> ToolContext:
    """A ``ToolContext`` for the undo button: a tool that never calls the model,
    so ``services["llm"]`` stays empty unless a caller resolved the user's client."""
    return ToolContext(
        session=session,
        user=user,
        profile=None,
        protocol=None,
        clock=deps.clock,
        settings=deps.settings,
        services={"llm": llm, "bus": deps.bus, **deps.tool_services()},
    )


async def _today(deps: AppDeps, session: AsyncSession, user: User) -> date:
    """The coaching day this user is in right now. ``log_meal`` dates food this way, so the card
    and the events have to agree with it or a 01:00 dinner lands on a day nothing shows."""
    profile = await repo.get_profile(session, user.id)
    return coaching_today(
        deps.clock,
        user.timezone or "UTC",
        profile.bed_time if profile is not None else None,
        profile.wake_time if profile is not None else None,
    )


async def _refresh_card(deps: AppDeps, session: AsyncSession, user: User) -> None:
    if deps.card is None:
        return
    try:
        state = await deps.state_provider.day_state(
            session, user, await _today(deps, session, user)
        )
        await deps.card.refresh(session, user, state)
    except Exception as exc:
        log.warning("daycard_refresh_failed", user_id=user.id, error=repr(exc))


async def _callback_undo(
    deps: AppDeps, session: AsyncSession, user: User, cb: CallbackInbound, meal_id: int
) -> None:
    ctx = _tool_ctx(deps, session, user)
    ctx.profile = await repo.get_profile(session, user.id)
    ctx.protocol = await repo.get_active_protocol(session, user.id)
    last = await repo.last_meal(session, user.id)
    if last is not None and last.id == meal_id:
        result = await deps.registry.dispatch(ctx, "undo_last", {})
    else:
        result = await deps.registry.dispatch(ctx, "delete_meal", {"meal_id": meal_id})
    lang = resolve_lang(user.language)
    if result.is_error:
        if "not found" in str(result.content) or "nothing to undo" in str(result.content):
            # a second tap, or Undo on a meal removed another way: an idempotent no-op
            await deps.messenger.answer_callback(cb.callback_id, t(lang, "btn.undo_done"))
            return
        log.warning("callback_undo_failed", user_id=user.id, meal_id=meal_id, error=result.content)
        await deps.messenger.answer_callback(cb.callback_id, t(lang, "err.unknown"))
        return
    now = ensure_utc(deps.clock.now())
    await _refresh_card(deps, session, user)
    await session.commit()
    await deps.bus.publish(
        DayStateChanged(
            user_id=user.id,
            occurred_at=now,
            date=await _today(deps, session, user),
            reason="undo",
        )
    )
    await deps.messenger.answer_callback(cb.callback_id, t(lang, "btn.undo"))


# --------------------------------------------------------------------------- the user's key


async def _delete_quietly(deps: AppDeps, chat_id: int, message_id: int) -> bool:
    try:
        return await deps.messenger.delete(chat_id, message_id)
    except Exception as exc:
        log.warning("key_message_delete_failed", chat_id=chat_id, error=type(exc).__name__)
        return False


async def handle_key_message(
    deps: AppDeps, session: AsyncSession, user: User, inbound: InboundMessage, key: str
) -> None:
    """A pasted Anthropic key (bring-your-own-key).

    One cheap authenticated call checks it (``KeyValidator``): rejected → ``key.invalid`` and
    nothing stored; accepted, or unanswerable (network, 5xx: stored anyway, ``key.unchecked``
    says the next real call is the check) → Fernet-encrypted into ``users`` (a new key replaces
    the old one). The message that carried the key is deleted from the chat either way; when
    Telegram refuses, ``key.saved_keep`` asks the user to delete it. Never persisted as a turn,
    never logged (only the last four characters). A user still in onboarding then gets the
    interview's first question at once — the key was the missing piece after ``/start``."""
    lang = resolve_lang(user.language)
    # the secret leaves the chat first; the check may take up to KEY_CHECK_TIMEOUT_S
    deleted = await _delete_quietly(deps, inbound.chat_id, inbound.message_id)
    check: KeyCheck = "unknown"
    if deps.key_validator is not None:
        try:
            check = await deps.key_validator.check(key)
        except Exception as exc:
            log.warning("llm_key_check_crashed", user_id=user.id, error=type(exc).__name__)
    if check == "invalid":
        log.info("llm_key_invalid", user_id=user.id, message_deleted=deleted)
        await _send(deps, inbound.chat_id, t(lang, "key.invalid"))
        return
    if deps.cipher is None:
        log.error("llm_key_no_cipher", user_id=user.id)
        await _send(deps, inbound.chat_id, t(lang, "err.unknown"))
        return
    last4 = await repo.set_llm_key(
        session, user.id, key, deps.cipher, now=ensure_utc(inbound.received_at)
    )
    await session.commit()
    log.info(
        "llm_key_saved",
        user_id=user.id,
        last4=last4,
        checked=check == "valid",
        message_deleted=deleted,
    )
    lines = [t(lang, "key.saved" if deleted else "key.saved_keep", last4=last4)]
    if check == "unknown":
        lines.append(t(lang, "key.unchecked"))
    await _send(deps, inbound.chat_id, "\n".join(lines))

    if user.status == UserStatus.language:
        # the key arrived before the language answer; ask again, the interview waits for it
        await _send(deps, inbound.chat_id, t(lang, "lang.ask"), keyboard=language_picker(lang))
        return
    if user.status == UserStatus.onboarding:
        llm = await deps.llm_factory.for_user(session, user)
        if llm is None:
            return
        synthetic = InboundMessage(
            telegram_id=inbound.telegram_id,
            chat_id=inbound.chat_id,
            message_id=inbound.message_id,
            received_at=inbound.received_at,
            text=START_TEXT,
            language_code=inbound.language_code,
        )
        incoming = await build_incoming(deps, user, synthetic)
        if incoming is not None:
            await run_agent_turn(deps, session, user, incoming, llm)


def _looks_like_secret(service: str, text: str) -> bool:
    """Is this message the key the coach asked for, or the user changing their mind?

    Only USDA gets this far: an OpenAI key has a shape and is taken wherever it lands, before
    anything looks at ``awaiting_secret``.
    """
    if not text or text.startswith("/"):
        return False
    return service == SecretService.usda and looks_like_usda_key(text)


async def handle_secret_message(
    deps: AppDeps,
    session: AsyncSession,
    user: User,
    inbound: InboundMessage,
    service: str,
    key: str,
) -> None:
    """An optional third-party key pasted into the chat (OpenAI for voice, USDA for food).

    Same handling as the Anthropic key and for the same reasons: the message is deleted first,
    one cheap request tells a typo from a working key, the value is Fernet-encrypted into
    ``user_secrets``, and only the last four characters are ever shown or logged. A key the
    service rejects is not stored and the coach keeps waiting for another one.
    """
    lang = resolve_lang(user.language)
    deleted = await _delete_quietly(deps, inbound.chat_id, inbound.message_id)
    try:
        check = await check_secret(service, key)
    except Exception as exc:  # a check that crashes must not cost the user their key
        log.warning("secret_check_crashed", service=service, error=type(exc).__name__)
        check = "unknown"
    if check == "invalid":
        log.info("user_secret_invalid", user_id=user.id, service=service, message_deleted=deleted)
        await _send(deps, inbound.chat_id, t(lang, "secret.invalid"))
        return
    if deps.cipher is None:
        log.error("user_secret_no_cipher", user_id=user.id, service=service)
        await _send(deps, inbound.chat_id, t(lang, "err.unknown"))
        return
    last4 = await repo.set_user_secret(
        session, user.id, service, key, deps.cipher, now=ensure_utc(inbound.received_at)
    )
    await session.commit()
    log.info(
        "user_secret_saved",
        user_id=user.id,
        service=service,
        last4=last4,
        checked=check == "valid",
        message_deleted=deleted,
    )
    lines = [t(lang, "secret.saved" if deleted else "secret.saved_keep", last4=last4)]
    if check == "unknown":
        # stored, but nobody confirmed it works: say so, the way the Anthropic path does
        lines.append(t(lang, "secret.unchecked"))
    await _send(deps, inbound.chat_id, "\n\n".join(lines))


# ------------------------------------------------------------------------------- /forget_me


async def forget_user(deps: AppDeps, session: AsyncSession, user: User) -> dict[str, int]:
    """Unpin the card, hard-delete every row in one transaction, drop the jobs, say one line."""
    lang = resolve_lang(user.language)
    chat_id = user.chat_id
    user_id = user.id
    today = await _today(deps, session, user)
    for offset in (0, 1):  # today's card, or yesterday's when today has none yet
        day = await repo.get_day(session, user_id, today - timedelta(days=offset))
        if day is not None and day.card_message_id is not None:
            await deps.messenger.unpin(chat_id, day.card_message_id)
            break
    counts = await delete_everything(session, user_id)
    await session.commit()
    if deps.scheduler is not None:
        try:
            deps.scheduler.remove_user(user_id)
        except Exception as exc:
            log.warning("remove_jobs_failed", user_id=user_id, error=repr(exc))
    await _send(deps, chat_id, t(lang, "forget.done", rows=sum(counts.values())))
    return counts
