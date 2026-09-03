"""The bot's public surface: the three commands and the profile texts, in ru and en.

``setMyCommands`` is per language (Telegram picks the client's language, default = English);
``setMyDescription`` / ``setMyShortDescription`` come from ``telegram/copy.py`` so the copy has
one home. Limits (research/03 §1 item 15–16): command 1–32 ``[a-z0-9_]``, description 1–256,
short description ≤ 120, description ≤ 512.
"""

from __future__ import annotations

from typing import Protocol

import structlog
from aiogram.types import BotCommand

from strikt.telegram.copy import t

log = structlog.get_logger(__name__)

COMMAND_NAMES: tuple[str, ...] = ("start", "today", "forget_me")
LANGUAGES: tuple[str, ...] = ("en", "ru")
MAX_SHORT_DESCRIPTION = 120
MAX_DESCRIPTION = 512
MAX_COMMAND_DESCRIPTION = 256


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


async def apply_bot_profile(bot: BotProfileAPI) -> None:
    """Set commands and descriptions for every language (English is the default profile)."""
    for lang in LANGUAGES:
        code = None if lang == "en" else lang
        await bot.set_my_commands(bot_commands(lang), language_code=code)
        await bot.set_my_short_description(short_description(lang), language_code=code)
        await bot.set_my_description(description(lang), language_code=code)
    log.info("bot_profile_applied", languages=list(LANGUAGES), commands=list(COMMAND_NAMES))
