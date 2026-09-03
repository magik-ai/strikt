"""Set the bot's public profile through the Bot API: name, descriptions, commands, avatar.

Run once after creating the bot in BotFather (and again whenever the copy or the avatar changes):

    TELEGRAM_BOT_TOKEN=... uv run python scripts/setup_telegram.py
    uv run python scripts/setup_telegram.py --avatar brand/avatar/avatar-512.jpg
    uv run python scripts/setup_telegram.py --skip-photo      # texts and commands only

The texts follow BRAND.md (brand voice: fact first, no flattery, no emoji). Telegram limits: name
64 characters, short description 120, description 512. English is the default profile; Russian is
set for clients whose language is ru.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.types import BotCommand, FSInputFile, InputProfilePhotoStatic

NAME = "Strikt"

SHORT = {
    "en": "A coach in one chat. Send a photo of your food, get the number. It owns your day and messages first.",
    "ru": "Тренер в одном чате. Пришли фото еды, получи цифру. Он ведёт твой день и пишет первым.",
}

DESCRIPTION = {
    "en": (
        "Strikt is a personal health coach that lives in this chat. No settings, no menus. "
        "Send photos of food, screenshots of delivery apps, labels, WHOOP screens or voice "
        "notes; it logs them, answers with kcal and macros, keeps the day's budget and tells "
        "you what is left. It connects to WHOOP and scales, remembers everything from day one, "
        "and when you go quiet it messages first. Direct, no flattery. Start with /start."
    ),
    "ru": (
        "Strikt — персональный тренер по здоровью, который живёт в этом чате. Без настроек и "
        "меню. Присылай фото еды, скриншоты доставки, этикетки, экраны WHOOP или голосовые: он "
        "всё запишет, ответит калориями и макросами, посчитает бюджет дня и скажет, сколько "
        "осталось. Подключает WHOOP и весы, помнит всё с первого дня, а если ты замолчал, "
        "напишет первым. Прямо, без лести. Начни с /start."
    ),
}

COMMANDS = {
    "en": [
        BotCommand(command="start", description="start or continue"),
        BotCommand(command="today", description="re-post today's card"),
        BotCommand(command="forget_me", description="delete everything about me"),
    ],
    "ru": [
        BotCommand(command="start", description="начать или продолжить"),
        BotCommand(command="today", description="показать карточку дня"),
        BotCommand(command="forget_me", description="удалить всё обо мне"),
    ],
}

LIMITS = {"name": 64, "short": 120, "description": 512}


def _check_limits() -> None:
    if len(NAME) > LIMITS["name"]:
        raise SystemExit(f"name is {len(NAME)} characters, the limit is {LIMITS['name']}")
    for lang, text in SHORT.items():
        if len(text) > LIMITS["short"]:
            raise SystemExit(f"short description ({lang}) is {len(text)} characters, limit 120")
    for lang, text in DESCRIPTION.items():
        if len(text) > LIMITS["description"]:
            raise SystemExit(f"description ({lang}) is {len(text)} characters, limit 512")


async def apply(token: str, avatar: Path | None) -> None:
    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        print(f"bot @{me.username} (id {me.id})")
        await bot.set_my_name(name=NAME)
        # default (no language code) is English; Russian clients get the ru copy
        await bot.set_my_short_description(short_description=SHORT["en"])
        await bot.set_my_short_description(short_description=SHORT["ru"], language_code="ru")
        await bot.set_my_description(description=DESCRIPTION["en"])
        await bot.set_my_description(description=DESCRIPTION["ru"], language_code="ru")
        await bot.set_my_commands(commands=COMMANDS["en"])
        await bot.set_my_commands(commands=COMMANDS["ru"], language_code="ru")
        print("name, descriptions and commands set (en, ru)")
        if avatar is not None:
            if avatar.suffix.lower() not in {".jpg", ".jpeg"}:
                raise SystemExit(
                    "the profile photo must be a .jpg (Bot API InputProfilePhotoStatic)"
                )
            ok = await bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(photo=FSInputFile(avatar))
            )
            print(f"profile photo set from {avatar}: {ok}")
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="bot token (default: $TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--avatar", default="brand/avatar/avatar-512.jpg", help="path to the 512x512 .jpg avatar"
    )
    parser.add_argument("--skip-photo", action="store_true", help="do not touch the profile photo")
    args = parser.parse_args(argv)
    if not args.token:
        print("no token: pass --token or set TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    _check_limits()
    avatar: Path | None = None
    if not args.skip_photo:
        avatar = Path(args.avatar)
        if not avatar.exists():
            print(f"avatar not found at {avatar}; texts and commands only", file=sys.stderr)
            avatar = None
    asyncio.run(apply(args.token, avatar))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
