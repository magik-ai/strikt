"""Process entrypoint: settings → logging → migrations → bot + proactive engine + web server.

One process, one event loop (``uvloop.run`` when available, research/09 §1 item 10):

- ``run_migrations``: ``alembic upgrade head`` in a worker thread (Alembic's env runs its own
  ``asyncio.run``), skipped with ``RUN_MIGRATIONS=false``;
- ``build_runtime``: engine + session factory, the ``LLMFactory`` (one ``LLM`` per API key:
  each user's own key in ``LLM_KEY_MODE=user``, the server key in ``server`` mode, recording
  usage through ``DbUsageRecorder``) and the key validator, the event bus, the integrations
  registry, ``DayStateBuilder``, ``LLMDecider`` → ``ProactiveEngine`` → ``ProactiveScheduler``
  (nightly summaries + 30-minute integration sync), ``AiogramMessenger``, the pinned
  ``DayCard``, the aiohttp app (OAuth, provider webhooks, optional Telegram webhook) and the
  aiogram dispatcher - every collaborator injectable so tests wire fakes;
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
from strikt.core.clock import SystemClock, coaching_today, ensure_utc, week_start
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.engine import make_engine, make_session_factory
from strikt.db.models import SummaryKind, User
from strikt.events import EventBus
from strikt.integrations.registry import build_registry as build_integrations
from strikt.logging import configure_logging
from strikt.memory.daystate import DayStateBuilder
from strikt.memory.summaries import first_sentence, update_week_summary, write_day_summary
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
from strikt.telegram.voice import TranscriberFactory, build_transcriber
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
    # In-process run: migrations/env.py must not apply alembic.ini's logging config, which would
    # drop the root level to WARN and disable every logger the app already made.
    config.attributes["configure_logger"] = False
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
    sessions: async_sessionmaker[AsyncSession],
    llm_factory: LLMResolver,
    clock: Clock,
    *,
    card: DayCard | None = None,
    state_provider: DayStateBuilder | None = None,
) -> Callable[[int, date], Awaitable[None]]:
    """03:00 local: close every day the user left open, summarise yesterday when ``close_day``
    did not, then refresh the week.

    The close matters as much as the summary: a day nobody closed used to stay open forever, so
    the morning message opened with "yesterday is still not closed" and the close trigger kept
    firing on a day the user had long finished. It runs *before* the summaries and sweeps every
    open day older than the current coaching day, not only ``day``: the summaries need the
    user's own API key and a working model call, and a day must close even when the key is
    missing, the call fails or the process was down that night. A day the user is still living
    in (a 02:30 bedtime runs the coaching day to 03:30) is older than the cutoff only on the
    next run, which is exactly when it should close.

    The summary calls are billed to the user's own key; a user without one is skipped
    (``llm_key_missing`` in the log, nothing summarised) until they paste it."""

    builder = state_provider

    async def _verdict(session: AsyncSession, user: User, day: date) -> str | None:
        """The summary's opening line, unless the summary is the "nothing happened" fallback."""
        summary = await repo.get_summary(session, user.id, SummaryKind.day, day)
        if summary is None:
            return None
        data = summary.data if isinstance(summary.data, dict) else {}
        if data.get("fallback") and not data.get("computed", {}).get("meals_logged"):
            return None
        return first_sentence(summary.text)

    async def _refresh_card(session: AsyncSession, user: User, day: date) -> None:
        """The pinned card must say closed too, or it contradicts the database until morning.

        Only a day that already has a card is touched: posting a *new* card for a past day at
        03:00 would be a notification in the middle of the night about a day that is over.
        """
        if card is None or builder is None:
            return
        row = await repo.get_day(session, user.id, day)
        if row is None or row.card_message_id is None:
            return
        try:
            state = await builder.day_state(session, user, day)
            await card.close(session, user, state, verdict=state.verdict)
            await session.commit()
        except Exception as exc:
            log.warning(
                "daycard_close_failed", user_id=user.id, day=day.isoformat(), error=repr(exc)
            )

    async def close_open_days(session: AsyncSession, user: User, cutoff: date) -> None:
        for row in await repo.open_days_before(session, user.id, cutoff):
            await repo.close_day(
                session,
                user.id,
                row.date,
                verdict=await _verdict(session, user, row.date),
                now=ensure_utc(clock.now()),
            )
            await session.commit()
            await _refresh_card(session, user, row.date)
            log.info("day_auto_closed", user_id=user.id, day=row.date.isoformat())

    async def nightly(user_id: int, day: date) -> None:
        async with sessions() as session:
            user = await repo.get_user(session, user_id)
            if user is None:
                return
            profile = await repo.get_profile(session, user_id)
            cutoff = coaching_today(
                clock,
                user.timezone or "UTC",
                profile.bed_time if profile else None,
                profile.wake_time if profile else None,
            )
            await close_open_days(session, user, cutoff)
            await session.commit()

            llm = await llm_factory.for_user(session, user)
            if llm is None:
                log.info("nightly_summary_skipped", user_id=user_id, reason="llm_key_missing")
                return
            if await repo.get_summary(session, user_id, SummaryKind.day, day) is None:
                await write_day_summary(llm, session, user, day, clock=clock)
            await update_week_summary(llm, session, user, week_start(day), clock=clock)
            # the day just summarised may have closed above with no verdict (no summary existed
            # then); give it the one the summary now provides
            await close_open_days(session, user, cutoff)
            closed = await repo.get_day(session, user_id, day)
            if closed is not None and not closed.verdict:
                closed.verdict = await _verdict(session, user, day)
            await session.commit()
            if closed is not None and closed.verdict:
                await _refresh_card(session, user, day)
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
    profile_task: asyncio.Task[None] | None = None
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
        # Sixty Bot API calls (three per language) took forty seconds of startup before the bot
        # read its first update. Nothing waits on the result, so it runs alongside polling.
        self.profile_task = asyncio.create_task(self._apply_profile(), name="bot-profile")
        if self.webhook_mode:
            secret = self.settings.telegram_webhook_secret
            await set_webhook(self.bot, self.settings, secret.get_secret_value() if secret else "")
        else:
            self.polling_task = asyncio.create_task(
                start_polling(self.dispatcher, self.bot), name="telegram-polling"
            )
        self.started = True
        log.info("strikt_started", mode=self.settings.telegram_mode, port=self.settings.web_port)

    async def _apply_profile(self) -> None:
        try:
            await apply_bot_profile(self.bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("bot_profile_failed", error=repr(exc))

    async def stop(self) -> None:
        log.info("strikt_stopping")
        if self.profile_task is not None and not self.profile_task.done():
            self.profile_task.cancel()
            with contextlib.suppress(BaseException):
                await self.profile_task
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
        nightly_summary=make_nightly_summary(
            sessions, llm_factory, clock, card=card, state_provider=state_provider
        ),
        integration_sync=make_integration_sync(integrations),
    )

    albums: AlbumCollector[InboundMessage] = AlbumCollector()
    speech = transcriber or build_transcriber(settings)
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
        transcriber=speech,
        transcribers=TranscriberFactory(settings, cipher, speech) if transcriber is None else None,
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
    polling_died = runtime.polling_task in done and not stop.is_set()
    if polling_died and runtime.polling_task is not None:
        exc = runtime.polling_task.exception() if not runtime.polling_task.cancelled() else None
        log.error("polling_exited", error=repr(exc) if exc else "finished")
    waiter.cancel()
    await runtime.stop()
    if polling_died:
        # a bot that stopped reading updates is dead, not finished: exit non-zero so the platform
        # restart policy (Railway's ON_FAILURE, compose's restart: unless-stopped) picks it up
        raise SystemExit(1)


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
        allowed_ids=len(settings.allowed_telegram_ids),
        admin_ids=len(settings.admin_telegram_ids),
    )
    if not settings.allowed_telegram_ids and not settings.admin_telegram_ids:
        # /start is invite-only and only an admin can mint an invite, so with both lists empty
        # nobody can ever get in: the bot boots, answers /health, and refuses every message.
        log.warning(
            "nobody_can_start",
            hint="set ALLOWED_TELEGRAM_IDS (and ADMIN_TELEGRAM_IDS) to the owner's Telegram id",
        )
    run_loop(serve(settings))


if __name__ == "__main__":  # pragma: no cover
    main()
