"""Process entrypoint: settings → logging → migrations → bot + proactive engine + web server.

One process, one event loop (``uvloop.run`` when available, research/09 §1 item 10):

- ``run_migrations``: ``alembic upgrade head`` in a worker thread (Alembic's env runs its own
  ``asyncio.run``), skipped with ``RUN_MIGRATIONS=false``;
- ``build_runtime``: engine + session factory, the ``LLMFactory`` (one ``LLM`` per API key —
  each user's own key in ``LLM_KEY_MODE=user``, the server key in ``server`` mode — recording
  usage through ``DbUsageRecorder``) and the key validator, the event bus, the integrations
  registry, ``DayStateBuilder``, ``LLMDecider`` → ``ProactiveEngine`` → ``ProactiveScheduler``
  (nightly summaries + 30-minute integration sync), ``AiogramMessenger``, the pinned
  ``DayCard``, the aiohttp app (OAuth, provider webhooks, optional Telegram webhook) and the
  aiogram dispatcher — every collaborator injectable so tests wire fakes;
- ``Runtime.start`` reschedules every active user, starts the web server, applies the bot
  profile (commands/descriptions) and either long-polls or registers the webhook;
- ``Runtime.stop`` is the graceful shutdown on SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

from strikt.agent.client import AnthropicKeyValidator, DbUsageRecorder, LLMFactory
from strikt.agent.loop import to_telegram_html
from strikt.agent.proactive_decide import LLMDecider
from strikt.agent.tools import build_registry
from strikt.config import get_settings
from strikt.core.clock import SystemClock, week_start
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.engine import make_engine, make_session_factory
from strikt.db.models import SummaryKind
from strikt.events import EventBus
from strikt.integrations.registry import build_registry as build_integrations
from strikt.logging import configure_logging
from strikt.memory.daystate import DayStateBuilder
from strikt.memory.summaries import update_week_summary, write_day_summary
from strikt.proactive.engine import ProactiveEngine
from strikt.proactive.scheduler import ProactiveScheduler
from strikt.telegram.bot import (
    attach_webhook_lifecycle,
    build_bot,
    build_dispatcher,
    set_webhook,
    start_polling,
    webhook_handler,
)
from strikt.telegram.commands import apply_bot_profile
from strikt.telegram.daycard import DayCard
from strikt.telegram.handlers import AppDeps, InboundMessage
from strikt.telegram.media import AiogramDownloader, AlbumCollector
from strikt.telegram.messenger import AiogramMessenger
from strikt.telegram.queue import PerChatQueue
from strikt.telegram.voice import build_transcriber
from strikt.web.server import TelegramHandler, make_app, run_server

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from strikt.agent.client import KeyValidator, LLMResolver
    from strikt.config import Settings
    from strikt.core.clock import Clock
    from strikt.db.models import User
    from strikt.integrations.registry import ClientFactory, Integrations
    from strikt.proactive.types import Decider, Sender
    from strikt.telegram.media import Downloader
    from strikt.telegram.messenger import Messenger
    from strikt.telegram.voice import Transcriber

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = "alembic.ini"
MIGRATIONS_DIR = "migrations"
SHUTDOWN_GRACE_S = 10.0


# ------------------------------------------------------------------------------- migrations


def _alembic_config(database_url: str) -> Any:
    from alembic.config import Config

    root = REPO_ROOT if (REPO_ROOT / ALEMBIC_INI).exists() else Path.cwd()
    ini = root / ALEMBIC_INI
    config = Config(str(ini)) if ini.exists() else Config()
    config.set_main_option("script_location", str(root / MIGRATIONS_DIR))
    # configparser interpolation: a literal % in the URL must be doubled
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_head(database_url: str) -> None:
    """Blocking ``alembic upgrade head``; run it in a thread from async code."""
    from alembic import command

    command.upgrade(_alembic_config(database_url), "head")


async def run_migrations(settings: Settings) -> None:
    log.info("migrations_start")
    await asyncio.to_thread(upgrade_head, settings.database_url)
    log.info("migrations_done")


# ------------------------------------------------------------------------------ collaborators


def make_sender(messenger: Messenger) -> Sender:
    """Proactive sends go through the same messenger; the id is recorded in ``proactive_sends``."""

    async def send(user: User, text: str) -> int | None:
        return await messenger.send(user.chat_id, to_telegram_html(text))

    return send


def make_notifier(messenger: Messenger) -> Callable[[User, str], Awaitable[None]]:
    """OAuth callbacks tell the user in Telegram, not only in the browser tab."""

    async def notify(user: User, message: str) -> None:
        await messenger.send(user.chat_id, to_telegram_html(message))

    return notify


def make_integration_sync(integrations: Integrations) -> Callable[[], Awaitable[None]]:
    """The scheduler's 30-minute job: pull every connected OAuth integration."""

    async def sync_all() -> None:
        published = await integrations.sync_all()
        log.debug("integration_sync_tick", events=published)

    return sync_all


def make_nightly_summary(
    sessions: async_sessionmaker[AsyncSession], llm_factory: LLMResolver, clock: Clock
) -> Callable[[int, date], Awaitable[None]]:
    """03:00 local: summarise yesterday when ``close_day`` did not, then refresh the week. Both
    calls are billed to the user's own key; a user without one is skipped (``llm_key_missing``
    in the log, nothing written) until they paste it."""

    async def nightly(user_id: int, day: date) -> None:
        async with sessions() as session:
            user = await repo.get_user(session, user_id)
            if user is None:
                return
            llm = await llm_factory.for_user(session, user)
            if llm is None:
                log.info("nightly_summary_skipped", user_id=user_id, reason="llm_key_missing")
                return
            if await repo.get_summary(session, user_id, SummaryKind.day, day) is None:
                await write_day_summary(llm, session, user, day, clock=clock)
            await update_week_summary(llm, session, user, week_start(day), clock=clock)
            await session.commit()
        log.info("nightly_summary_done", user_id=user_id, day=day.isoformat())

    return nightly


# ----------------------------------------------------------------------------------- runtime


@dataclass
class Runtime:
    """Every long-lived component, plus ``start``/``stop``."""

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    clock: Clock
    bus: EventBus
    llm_factory: LLMResolver
    integrations: Integrations
    state_provider: DayStateBuilder
    proactive: ProactiveEngine
    scheduler: ProactiveScheduler
    messenger: Messenger
    card: DayCard
    deps: AppDeps
    bot: Bot
    dispatcher: Dispatcher
    web_app: web.Application
    runner: web.AppRunner | None = None
    polling_task: asyncio.Task[None] | None = None
    started: bool = field(default=False)

    @property
    def webhook_mode(self) -> bool:
        return self.settings.telegram_mode == "webhook"

    async def start(self) -> None:
        self.scheduler.start()
        users = await self.scheduler.reschedule_all()
        log.info("proactive_jobs_scheduled", users=users)
        host = str(getattr(self.settings, "web_host", "0.0.0.0"))
        self.runner = await run_server(self.web_app, host, self.settings.web_port)
        try:
            await apply_bot_profile(self.bot)
        except Exception as exc:
            log.warning("bot_profile_failed", error=repr(exc))
        if self.webhook_mode:
            secret = self.settings.telegram_webhook_secret
            await set_webhook(self.bot, self.settings, secret.get_secret_value() if secret else "")
        else:
            self.polling_task = asyncio.create_task(
                start_polling(self.dispatcher, self.bot), name="telegram-polling"
            )
        self.started = True
        log.info("strikt_started", mode=self.settings.telegram_mode, port=self.settings.web_port)

    async def stop(self) -> None:
        log.info("strikt_stopping")
        if self.polling_task is not None and not self.polling_task.done():
            with contextlib.suppress(RuntimeError):
                await self.dispatcher.stop_polling()
            try:
                await asyncio.wait_for(self.polling_task, SHUTDOWN_GRACE_S)
            except (TimeoutError, asyncio.CancelledError):
                self.polling_task.cancel()
                with contextlib.suppress(BaseException):
                    await self.polling_task
            except Exception as exc:
                log.warning("polling_stop_failed", error=repr(exc))
        self.scheduler.shutdown()
        self.proactive.close()
        if self.runner is not None:
            await self.runner.cleanup()
        with contextlib.suppress(Exception):
            await self.bot.session.close()
        await self.engine.dispose()
        self.started = False
        log.info("strikt_stopped")


def build_runtime(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    bot: Bot | None = None,
    llm_factory: LLMResolver | None = None,
    key_validator: KeyValidator | None = None,
    messenger: Messenger | None = None,
    clock: Clock | None = None,
    transcriber: Transcriber | None = None,
    downloader: Downloader | None = None,
    decider: Decider | None = None,
    client_factory: ClientFactory | None = None,
    scheduler: Any | None = None,
) -> Runtime:
    """Assemble the process. Every keyword is a seam for tests (fakes) and for ``main``."""
    clock = clock or SystemClock()
    engine = engine or make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    fernet_key = settings.token_encryption_key.get_secret_value()
    cipher = TokenCipher(fernet_key) if fernet_key else None
    llm_factory = llm_factory or LLMFactory(settings, DbUsageRecorder(sessions, clock), cipher)
    key_validator = key_validator or AnthropicKeyValidator(settings)
    bus = EventBus()
    integrations = build_integrations(
        settings, sessions, bus, clock=clock, client_factory=client_factory
    )
    state_provider = DayStateBuilder(clock, settings)
    bot = bot or build_bot(settings)
    messenger = messenger or AiogramMessenger(bot)
    card = DayCard(messenger, clock)

    decider = decider or LLMDecider(llm_factory, settings, clock=clock)
    proactive = ProactiveEngine(
        sessions,
        decider,
        state_provider,
        make_sender(messenger),
        clock,
        settings,
        bus,
        llm_factory=llm_factory,
    )
    proactive_scheduler = ProactiveScheduler(
        proactive,
        sessions,
        clock,
        scheduler=scheduler,
        nightly_summary=make_nightly_summary(sessions, llm_factory, clock),
        integration_sync=make_integration_sync(integrations),
    )

    albums: AlbumCollector[InboundMessage] = AlbumCollector()
    deps = AppDeps(
        settings=settings,
        sessions=sessions,
        clock=clock,
        llm_factory=llm_factory,
        key_validator=key_validator,
        cipher=cipher,
        registry=build_registry(),
        messenger=messenger,
        bus=bus,
        state_provider=state_provider,
        transcriber=transcriber or build_transcriber(settings),
        downloader=downloader or AiogramDownloader(bot),
        albums=albums,
        queue=PerChatQueue(),
        card=card,
        scheduler=proactive_scheduler,
        integrations=integrations,
    )
    dispatcher = build_dispatcher(deps)

    telegram: TelegramHandler | None = None
    if settings.telegram_mode == "webhook":
        secret = settings.telegram_webhook_secret
        telegram = webhook_handler(dispatcher, bot, secret.get_secret_value() if secret else "")
    web_app = make_app(
        settings,
        sessions,
        bus,
        integrations,
        telegram,
        clock=clock,
        notify=make_notifier(messenger),
    )
    if telegram is not None:
        attach_webhook_lifecycle(web_app, dispatcher, bot)

    return Runtime(
        settings=settings,
        engine=engine,
        sessions=sessions,
        clock=clock,
        bus=bus,
        llm_factory=llm_factory,
        integrations=integrations,
        state_provider=state_provider,
        proactive=proactive,
        scheduler=proactive_scheduler,
        messenger=messenger,
        card=card,
        deps=deps,
        bot=bot,
        dispatcher=dispatcher,
        web_app=web_app,
    )


# -------------------------------------------------------------------------------------- main


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def serve(settings: Settings) -> None:
    """Migrate, build, start, wait for a signal, stop."""
    if bool(getattr(settings, "run_migrations", True)):
        await run_migrations(settings)
    runtime = build_runtime(settings)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    await runtime.start()
    waiter = asyncio.create_task(stop.wait(), name="wait-for-signal")
    tasks: set[asyncio.Task[Any]] = {waiter}
    if runtime.polling_task is not None:
        tasks.add(runtime.polling_task)
    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if runtime.polling_task in done and not stop.is_set():
        exc = runtime.polling_task.exception() if not runtime.polling_task.cancelled() else None
        log.error("polling_exited", error=repr(exc) if exc else "finished")
    waiter.cancel()
    await runtime.stop()


def run_loop(coro: Coroutine[Any, Any, None]) -> None:
    """``uvloop.run`` when installed (Linux/macOS), else ``asyncio.run``."""
    try:
        import uvloop
    except ImportError:  # pragma: no cover - uvloop is a hard dependency off Windows
        asyncio.run(coro)
        return
    uvloop.run(coro)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    missing = settings.missing_for_runtime()
    if missing:
        log.error("missing_settings", names=missing)
        sys.exit(2)
    log.info(
        "strikt_boot",
        model=settings.model,
        mode=settings.telegram_mode,
        key_mode=settings.llm_key_mode,
        server_key=settings.server_api_key is not None,
    )
    run_loop(serve(settings))


if __name__ == "__main__":  # pragma: no cover
    main()
