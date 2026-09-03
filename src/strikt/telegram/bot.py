"""aiogram wiring: the ``Bot``, one ``Dispatcher`` with one ``Router``, polling and webhook.

The router does nothing but translate updates (``handlers.from_message`` /
``handlers.from_callback``) and call the transport-free handlers, so the same dispatcher serves
``start_polling`` in development and the aiohttp ``SimpleRequestHandler`` webhook in production
(research/03 §1 item 3). The two modes are mutually exclusive on Telegram's side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from strikt.telegram.handlers import (
    AppDeps,
    from_callback,
    from_message,
    handle_callback,
    handle_message as _handle_message,
)

if TYPE_CHECKING:
    from aiohttp import web

    from strikt.config import Settings
    from strikt.web.server import TelegramHandler

log = structlog.get_logger(__name__)

WEBHOOK_PATH = "/telegram"
POLLING_TIMEOUT_S = 30
WEBHOOK_MAX_CONNECTIONS = 40
#: Explicit so a future aiogram default cannot widen it (research/03 §12).
ALLOWED_UPDATES: list[str] = ["message", "callback_query"]


def build_bot(settings: Settings) -> Bot:
    """HTML parse mode everywhere (research/03 §1 item 8); no network until the first call."""
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_router(deps: AppDeps) -> Router:
    router = Router(name="strikt")

    @router.message()
    async def on_message(message: Message) -> None:
        await _handle_message(deps, from_message(message, received_at=deps.clock.now()))

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        await handle_callback(deps, from_callback(query))

    return router


def build_dispatcher(deps: AppDeps) -> Dispatcher:
    """One router, no FSM (PLAN §7)."""
    dispatcher = Dispatcher(disable_fsm=True, name="strikt")
    dispatcher.include_router(build_router(deps))
    return dispatcher


def webhook_url(settings: Settings) -> str:
    return f"{str(settings.public_base_url).rstrip('/')}{WEBHOOK_PATH}"


def webhook_handler(dispatcher: Dispatcher, bot: Bot, secret: str) -> TelegramHandler:
    """``POST /telegram`` for ``web.server.make_app``: verifies the secret header, answers 200
    at once and processes the update in the background (LLM turns outlive Telegram's timeout)."""
    handler = SimpleRequestHandler(
        dispatcher=dispatcher, bot=bot, secret_token=secret, handle_in_background=True
    )
    return handler.handle


def attach_webhook_lifecycle(app: web.Application, dispatcher: Dispatcher, bot: Bot) -> None:
    """Run the dispatcher's startup/shutdown with the aiohttp app (polling does this itself)."""
    setup_application(app, dispatcher, bot=bot)


async def set_webhook(bot: Bot, settings: Settings, secret: str) -> str:
    url = webhook_url(settings)
    await bot.set_webhook(
        url,
        secret_token=secret,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=True,
        max_connections=WEBHOOK_MAX_CONNECTIONS,
    )
    log.info("webhook_set", url=url)
    return url


async def start_polling(dispatcher: Dispatcher, bot: Bot) -> None:
    """Long polling; signals are handled by ``app.py``, not by aiogram."""
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(
        bot,
        allowed_updates=ALLOWED_UPDATES,
        polling_timeout=POLLING_TIMEOUT_S,
        handle_signals=False,
        close_bot_session=False,
    )
