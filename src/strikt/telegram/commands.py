"""The bot's public surface: the three commands and the profile texts, in every language.

``setMyCommands`` is per language (Telegram picks the client's language, default = English);
``setMyDescription`` / ``setMyShortDescription`` come from ``telegram/copy.py`` so the copy has
one home. Limits (research/03 §1 item 15–16): command 1–32 ``[a-z0-9_]``, description 1–256,
short description ≤ 120, description ≤ 512.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand

from strikt.telegram.copy import DEFAULT_LANG, LANGUAGES, t

log = structlog.get_logger(__name__)

COMMAND_NAMES: tuple[str, ...] = ("start", "today", "forget_me")
MAX_SHORT_DESCRIPTION = 120
MAX_DESCRIPTION = 512
MAX_COMMAND_DESCRIPTION = 256
#: Telegram flood-controls ``setMyCommands``. Twenty languages are sixty calls, so they are
#: spaced out; the whole thing runs in the background and nothing waits for it.
PROFILE_PAUSE_S = 1.0


class BotProfileAPI(Protocol):
    """The slice of ``aiogram.Bot`` used here (so tests pass a recorder)."""

    async def set_my_commands(
        self, commands: list[BotCommand], *, language_code: str | None = None
    ) -> bool: ...

    async def set_my_description(
        self, description: str | None = None, *, language_code: str | None = None
    ) -> bool: ...

    async def set_my_short_description(
        self, short_description: str | None = None, *, language_code: str | None = None
    ) -> bool: ...


def bot_commands(lang: str | None) -> list[BotCommand]:
    return [
        BotCommand(command=name, description=t(lang, f"cmd.{name}")[:MAX_COMMAND_DESCRIPTION])
        for name in COMMAND_NAMES
    ]


def short_description(lang: str | None) -> str:
    return t(lang, "bot.short")[:MAX_SHORT_DESCRIPTION]


def description(lang: str | None) -> str:
    return t(lang, "bot.description")[:MAX_DESCRIPTION]


async def _patiently(call: Callable[[], Awaitable[bool]]) -> None:
    """One retry after the exact wait Telegram asks for. Anything else is the caller's problem."""
    try:
        await call()
    except TelegramRetryAfter as exc:
        log.info("bot_profile_throttled", seconds=exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        await call()


async def apply_bot_profile(bot: BotProfileAPI, *, pause_s: float = PROFILE_PAUSE_S) -> None:
    """Set commands and descriptions for every language (English is the default profile)."""
    for index, lang in enumerate(LANGUAGES):
        code = None if lang == DEFAULT_LANG else lang
        if index and pause_s:
            await asyncio.sleep(pause_s)
        await _patiently(
            lambda lang=lang, code=code: bot.set_my_commands(  # type: ignore[misc]
                bot_commands(lang), language_code=code
            )
        )
        await _patiently(
            lambda lang=lang, code=code: bot.set_my_short_description(  # type: ignore[misc]
                short_description(lang), language_code=code
            )
        )
        await _patiently(
            lambda lang=lang, code=code: bot.set_my_description(  # type: ignore[misc]
                description(lang), language_code=code
            )
        )
    log.info("bot_profile_applied", languages=list(LANGUAGES), commands=list(COMMAND_NAMES))
