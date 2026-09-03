"""``Messenger``: the only way code talks to Telegram, with an aiogram and a fake implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
)

from strikt.telegram.render import split_message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiogram import Bot

    from strikt.core.types import Button

log = structlog.get_logger(__name__)


class Messenger(Protocol):
    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> int:
        """Send HTML text (split at 4096 on line boundaries); returns the last message id."""
        ...

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
    ) -> bool:
        """Edit in place. False when Telegram says the message is unchanged or gone."""
        ...

    async def pin(self, chat_id: int, message_id: int) -> bool: ...

    async def unpin(self, chat_id: int, message_id: int) -> bool: ...

    async def delete(self, chat_id: int, message_id: int) -> bool: ...

    async def chat_action(self, chat_id: int, action: str = "typing") -> None: ...

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None: ...


def to_markup(keyboard: Sequence[Sequence[Button]] | None) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    rows = [
        [InlineKeyboardButton(text=b.text, callback_data=b.callback_data, url=b.url) for b in row]
        for row in keyboard
        if row
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class AiogramMessenger:
    """Real Telegram via aiogram ``Bot``. HTML parse mode, link previews off."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._no_preview = LinkPreviewOptions(is_disabled=True)

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> int:
        parts = split_message(text)
        last_id = 0
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            message = await self._bot.send_message(
                chat_id,
                part,
                parse_mode=ParseMode.HTML,
                reply_markup=to_markup(keyboard) if is_last else None,
                reply_to_message_id=reply_to if index == 0 else None,
                link_preview_options=self._no_preview,
                disable_notification=silent,
            )
            last_id = message.message_id
        return last_id

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
    ) -> bool:
        try:
            await self._bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=to_markup(keyboard),
                link_preview_options=self._no_preview,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message:
                return False
            log.warning("edit_failed", chat_id=chat_id, message_id=message_id, error=exc.message)
            return False
        return True

    async def pin(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        except TelegramBadRequest as exc:
            log.warning("pin_failed", chat_id=chat_id, error=exc.message)
            return False
        return True

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._bot.unpin_chat_message(chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            log.info("unpin_failed", chat_id=chat_id, error=exc.message)
            return False
        return True

    async def delete(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._bot.delete_message(chat_id, message_id)
        except TelegramBadRequest as exc:
            log.info("delete_failed", chat_id=chat_id, error=exc.message)
            return False
        return True

    async def chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self._bot.send_chat_action(chat_id, ChatAction(action))
        except TelegramBadRequest as exc:
            log.debug("chat_action_failed", chat_id=chat_id, error=exc.message)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self._bot.answer_callback_query(callback_id, text=text)
        except TelegramBadRequest as exc:
            log.debug("answer_callback_failed", error=exc.message)


@dataclass
class SentMessage:
    chat_id: int
    message_id: int
    text: str
    keyboard: list[list[Button]] | None = None
    reply_to: int | None = None
    silent: bool = False


@dataclass
class FakeMessenger:
    """Records every call. ``edit`` mimics Telegram: False when the text did not change."""

    sent: list[SentMessage] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    pins: list[tuple[int, int]] = field(default_factory=list)
    unpins: list[tuple[int, int]] = field(default_factory=list)
    deletes: list[tuple[int, int]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    callbacks: list[tuple[str, str | None]] = field(default_factory=list)
    next_message_id: int = 1000
    _texts: dict[tuple[int, int], str] = field(default_factory=dict)

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
        reply_to: int | None = None,
        silent: bool = False,
    ) -> int:
        last_id = 0
        for part in split_message(text):
            self.next_message_id += 1
            last_id = self.next_message_id
            self.sent.append(
                SentMessage(
                    chat_id=chat_id,
                    message_id=last_id,
                    text=part,
                    keyboard=[list(row) for row in keyboard] if keyboard else None,
                    reply_to=reply_to,
                    silent=silent,
                )
            )
            self._texts[(chat_id, last_id)] = part
        return last_id

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: Sequence[Sequence[Button]] | None = None,
    ) -> bool:
        key = (chat_id, message_id)
        if key not in self._texts or self._texts[key] == text:
            return False
        self._texts[key] = text
        self.edits.append((chat_id, message_id, text))
        return True

    async def pin(self, chat_id: int, message_id: int) -> bool:
        self.pins.append((chat_id, message_id))
        return (chat_id, message_id) in self._texts

    async def unpin(self, chat_id: int, message_id: int) -> bool:
        self.unpins.append((chat_id, message_id))
        return (chat_id, message_id) in self._texts

    async def delete(self, chat_id: int, message_id: int) -> bool:
        self.deletes.append((chat_id, message_id))
        return self._texts.pop((chat_id, message_id), None) is not None

    async def chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))

    # --- assertions helpers ---------------------------------------------------------------
    @property
    def last_text(self) -> str | None:
        return self.sent[-1].text if self.sent else None

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [m.text for m in self.sent if chat_id is None or m.chat_id == chat_id]

    def current_text(self, chat_id: int, message_id: int) -> str | None:
        return self._texts.get((chat_id, message_id))

    def as_dict(self) -> dict[str, Any]:
        return {"sent": len(self.sent), "edits": len(self.edits), "pins": len(self.pins)}
