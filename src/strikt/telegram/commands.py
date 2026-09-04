"""The bot's public surface: the three commands and the profile texts, in every language.

``setMyCommands`` is per language (Telegram picks the client's language, default = English);
``setMyDescription`` / ``setMyShortDescription`` come from ``telegram/copy.py`` so the copy has
one home. Limits (research/03 §1 item 15–16): command 1–32 ``[a-z0-9_]``, description 1–256,
short description ≤ 120, description ≤ 512.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BotCommand, BotDescription, BotShortDescription

from strikt.telegram.copy import DEFAULT_LANG, LANGUAGES, t

log = structlog.get_logger(__name__)

COMMAND_NAMES: tuple[str, ...] = ("start", "today", "forget_me")
MAX_SHORT_DESCRIPTION = 120
MAX_DESCRIPTION = 512
MAX_COMMAND_DESCRIPTION = 256
#: Telegram flood-controls ``setMyCommands``. Twenty languages would be sixty writes on every
#: boot, so each language is read first and written only where it differs - which on a restart
#: with unchanged copy is nowhere. The pause covers the reads and the occasional real write.
PROFILE_PAUSE_S = 0.2


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

    async def get_my_commands(self, *, language_code: str | None = None) -> list[BotCommand]: ...

    async def get_my_description(self, *, language_code: str | None = None) -> BotDescription: ...

    async def get_my_short_description(
        self, *, language_code: str | None = None
    ) -> BotShortDescription: ...


def bot_commands(lang: str | None) -> list[BotCommand]:
    return [
        BotCommand(command=name, description=t(lang, f"cmd.{name}")[:MAX_COMMAND_DESCRIPTION])
        for name in COMMAND_NAMES
    ]


def short_description(lang: str | None) -> str:
    return t(lang, "bot.short")[:MAX_SHORT_DESCRIPTION]


def description(lang: str | None) -> str:
    return t(lang, "bot.description")[:MAX_DESCRIPTION]


async def _patiently(call: Callable[[], Awaitable[Any]]) -> Any:
    """One retry after the exact wait Telegram asks for. Anything else is the caller's problem."""
    try:
        return await call()
    except TelegramRetryAfter as exc:
        log.info("bot_profile_throttled", seconds=exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        return await call()


async def _sync_language(bot: BotProfileAPI, lang: str, code: str | None) -> int:
    """Bring one language's profile in line, writing only what differs. Returns the writes made."""
    writes = 0
    wanted = bot_commands(lang)
    current = await _patiently(lambda: bot.get_my_commands(language_code=code))
    if [(c.command, c.description) for c in current] != [
        (c.command, c.description) for c in wanted
    ]:
        await _patiently(lambda: bot.set_my_commands(wanted, language_code=code))
        writes += 1

    short = short_description(lang)
    current_short = await _patiently(lambda: bot.get_my_short_description(language_code=code))
    if current_short.short_description != short:
        await _patiently(lambda: bot.set_my_short_description(short, language_code=code))
        writes += 1

    full = description(lang)
    current_full = await _patiently(lambda: bot.get_my_description(language_code=code))
    if current_full.description != full:
        await _patiently(lambda: bot.set_my_description(full, language_code=code))
        writes += 1
    return writes


async def apply_bot_profile(bot: BotProfileAPI, *, pause_s: float = PROFILE_PAUSE_S) -> None:
    """Set commands and descriptions for every language (English is the default profile)."""
    done: list[str] = []
    writes = 0
    for index, lang in enumerate(LANGUAGES):
        code = None if lang == DEFAULT_LANG else lang
        if index and pause_s:
            await asyncio.sleep(pause_s)
        try:
            writes += await _sync_language(bot, lang, code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # one language throttled twice must not cost the nineteen after it
            log.warning("bot_profile_language_failed", language=lang, error=repr(exc))
            continue
        done.append(lang)
    log.info("bot_profile_applied", languages=done, writes=writes, commands=list(COMMAND_NAMES))
