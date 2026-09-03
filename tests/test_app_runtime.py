"""``strikt.app``: the real wiring with fakes — WHOOP webhook → engine, proactive sends, the
dispatcher, the Telegram webhook secret, nightly summaries, bot profile, migrations, config docs."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiogram import Bot
from aiogram.types import BotCommand, Chat, Message, Update, User as TgUser
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt import app as app_mod
from strikt.agent.client import FakeLLM, FakeLLMFactory
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import ProactiveSend, SummaryKind, User, Workout
from strikt.events import WorkoutEvent
from strikt.integrations import whoop
from strikt.telegram import commands
from strikt.telegram.copy import t
from strikt.telegram.messenger import FakeMessenger
from tests.conftest import CHAT_ID, TELEGRAM_ID
from tests.test_handlers_e2e import FakeDownloader, FakeTranscriber
from tests.test_integrations_fakes import NOW, WHOOP_WORKOUT, Router, make_settings
from tests.test_proactive_helpers import TODAY, FakeDecider, at_local

API = "https://api.prod.whoop.com/developer"
ROOT = Path(__file__).resolve().parents[1]
FAKE_TOKEN = "424242:" + "A" * 35


class Harness:
    def __init__(
        self,
        engine: AsyncEngine,
        clock: FakeClock,
        fake_llm: FakeLLM,
        messenger: FakeMessenger,
        **overrides: Any,
    ) -> None:
        self.settings = make_settings(allowed_telegram_ids=[TELEGRAM_ID], web_port=0, **overrides)
        self.router = Router()
        self.decider = FakeDecider()
        self.messenger = messenger
        self.llm = fake_llm
        self.llm_factory = FakeLLMFactory(fake_llm)
        self.runtime = app_mod.build_runtime(
            self.settings,
            engine=engine,
            bot=Bot(token=FAKE_TOKEN),
            llm_factory=self.llm_factory,
            messenger=messenger,
            clock=clock,
            transcriber=FakeTranscriber(),
            downloader=FakeDownloader(),
            decider=self.decider,
            client_factory=self.router.client_factory(),
        )


@pytest.fixture
async def harness(
    engine: AsyncEngine, clock: FakeClock, fake_llm: FakeLLM, messenger: FakeMessenger, user: User
) -> AsyncIterator[Harness]:
    h = Harness(engine, clock, fake_llm, messenger)
    yield h
    h.runtime.proactive.close()
    await h.runtime.bot.session.close()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    async with TestClient(TestServer(harness.runtime.web_app)) as client:
        yield client


# --------------------------------------------------------------------------- wiring smoke


def test_import_and_wiring(harness: Harness) -> None:
    rt = harness.runtime
    assert rt.deps.llm_factory is harness.llm_factory and rt.llm_factory is harness.llm_factory
    assert rt.deps.messenger is harness.messenger
    assert rt.deps.cipher is not None and rt.deps.key_validator is not None
    assert rt.deps.card is rt.card and rt.deps.scheduler is rt.scheduler
    assert rt.deps.integrations is rt.integrations and set(rt.integrations) == {
        "apple_health",
        "whoop",
        "withings",
    }
    assert rt.deps.registry.names()[:2] == ["cancel_reminder", "close_day"]
    assert rt.webhook_mode is False and rt.polling_task is None
    services = rt.deps.tool_services()
    assert services["integrations"] is rt.integrations and services["card"] is rt.card


# ------------------------------------------------------------------- WHOOP webhook → engine


async def test_whoop_webhook_ends_in_a_workout_message(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
    messenger: FakeMessenger,
) -> None:
    rt = harness.runtime
    cipher = TokenCipher(harness.settings.token_encryption_key.get_secret_value())
    await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "whoop",
        access_token="acc",
        refresh_token="ref",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
        external_user_id="9012",
    )
    await session.commit()
    seen: list[WorkoutEvent] = []

    async def on_workout(event: WorkoutEvent) -> None:
        seen.append(event)

    rt.bus.subscribe(WorkoutEvent, on_workout)
    harness.router.json("GET", f"{API}/v2/activity/workout/{WHOOP_WORKOUT['id']}", WHOOP_WORKOUT)
    body = json.dumps(
        {"user_id": 9012, "id": WHOOP_WORKOUT["id"], "type": "workout.updated", "trace_id": "t1"}
    ).encode()
    ts = str(int(NOW.timestamp() * 1000))
    headers = {
        "X-WHOOP-Signature": whoop.compute_signature("whoop-secret", ts, body),
        "X-WHOOP-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }
    response = await client.post("/webhooks/whoop", data=body, headers=headers)
    assert response.status == 200

    assert len(seen) == 1 and seen[0].sport == "running" and seen[0].source == "whoop"
    assert len((await session.scalars(select(Workout))).all()) == 1
    # the engine fired whoop_workout_synced; the FakeDecider wrote the text; the sender recorded the id
    fire = harness.decider.last.fire
    assert fire.name == "whoop_workout_synced" and fire.facts["this"]["sport"] == "running"
    assert messenger.texts(CHAT_ID) == ["whoop_workout_synced step 1:"]  # html-escaped, stripped
    sends = list((await session.scalars(select(ProactiveSend))).all())
    assert len(sends) == 1
    assert sends[0].trigger == "whoop_workout_synced"
    assert sends[0].telegram_message_id == messenger.sent[-1].message_id


async def test_proactive_timer_fire_sends_and_records(
    harness: Harness, user: User, clock: FakeClock, messenger: FakeMessenger, session: AsyncSession
) -> None:
    clock.set(at_local(TODAY, "11:05"))
    outcome = await harness.runtime.proactive.fire(user.id, "no_first_meal")
    assert outcome.sent and outcome.step == 1
    assert messenger.texts(CHAT_ID) == ["no_first_meal step 1: hours_since_wake=3.1"]
    row = await repo.last_send_for_window(session, user.id, outcome.window_key or "")
    assert row is not None and row.telegram_message_id == messenger.sent[-1].message_id


async def test_sender_and_notifier_render_html(user: User, messenger: FakeMessenger) -> None:
    send = app_mod.make_sender(messenger)
    message_id = await send(user, "**14:10.** Nothing logged <yet>.")
    assert messenger.sent[-1].text == "<b>14:10.</b> Nothing logged &lt;yet&gt;."
    assert message_id == messenger.sent[-1].message_id
    notify = app_mod.make_notifier(messenger)
    await notify(user, "WHOOP connected.")
    assert messenger.last_text == "WHOOP connected."


# ---------------------------------------------------------------------------- dispatcher


def _update(update_id: int, text: str, *, chat_id: int = CHAT_ID) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=chat_id, is_bot=False, first_name="T", language_code="ru"),
        text=text,
    )
    return Update(update_id=update_id, message=message)


async def test_dispatcher_routes_updates_to_the_handlers(
    harness: Harness, user: User, messenger: FakeMessenger, fake_llm: FakeLLM
) -> None:
    rt = harness.runtime
    await rt.dispatcher.feed_update(rt.bot, _update(1, "/today"))
    assert len(messenger.sent) == 1 and "Сегодня" in messenger.sent[0].text
    assert messenger.pins == [(CHAT_ID, messenger.sent[0].message_id)]
    fake_llm.queue(FakeLLM.text("Понял."))
    await rt.dispatcher.feed_update(rt.bot, _update(2, "привет"))
    assert messenger.texts(CHAT_ID)[-1] == "Понял."
    await rt.dispatcher.feed_update(rt.bot, _update(3, "hi", chat_id=999))
    assert messenger.texts(999) == [t("ru", "err.not_allowed")]


async def test_telegram_webhook_requires_the_secret(
    engine: AsyncEngine, clock: FakeClock, fake_llm: FakeLLM, messenger: FakeMessenger, user: User
) -> None:
    h = Harness(
        engine,
        clock,
        fake_llm,
        messenger,
        telegram_mode="webhook",
        telegram_webhook_secret="s3cret",
    )
    assert h.runtime.webhook_mode
    try:
        async with TestClient(TestServer(h.runtime.web_app)) as client:
            payload = _update(7, "/today").model_dump(mode="json", exclude_none=True)
            rejected = await client.post("/telegram", json=payload)
            assert rejected.status == 401
            accepted = await client.post(
                "/telegram", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
            )
            assert accepted.status == 200
            for _ in range(50):  # handle_in_background: the card appears shortly after the 200
                if messenger.pins:
                    break
                await asyncio.sleep(0.02)
            assert messenger.pins and "Сегодня" in messenger.sent[0].text
    finally:
        h.runtime.proactive.close()
        await h.runtime.bot.session.close()


# ---------------------------------------------------------------------------- lifecycle


async def test_runtime_start_and_stop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, user: User
) -> None:
    calls: list[str] = []

    async def fake_profile(bot: Any) -> None:
        calls.append("profile")

    async def fake_polling(dispatcher: Any, bot: Any) -> None:
        calls.append("polling")
        await asyncio.sleep(3600)

    monkeypatch.setattr(app_mod, "apply_bot_profile", fake_profile)
    monkeypatch.setattr(app_mod, "start_polling", fake_polling)
    rt = harness.runtime
    await rt.start()
    try:
        assert rt.started and rt.scheduler.scheduler.running
        assert rt.scheduler.user_job_ids(user.id)  # every active user rescheduled at start
        assert rt.runner is not None and rt.polling_task is not None
        assert rt.runner.addresses
        host, port = rt.runner.addresses[0][:2]
        async with (
            aiohttp.ClientSession() as http,
            http.get(f"http://{host}:{port}/health") as response,
        ):
            assert response.status == 200
            assert (await response.json())["status"] == "ok"
        await asyncio.sleep(0)
        assert calls == ["profile", "polling"]
    finally:
        await rt.stop()
    assert not rt.started and rt.polling_task is not None and rt.polling_task.done()


async def test_nightly_summary_writes_day_and_week_once(
    harness: Harness, user: User, session: AsyncSession, fake_llm: FakeLLM, clock: FakeClock
) -> None:
    nightly = app_mod.make_nightly_summary(
        harness.runtime.sessions, FakeLLMFactory(fake_llm), clock
    )
    yesterday = date(2026, 9, 2)
    fake_llm.queue(FakeLLM.text("not json"), FakeLLM.text("not json"))  # → deterministic fallbacks
    await nightly(user.id, yesterday)
    day = await repo.get_summary(session, user.id, SummaryKind.day, yesterday)
    week = await repo.get_summary(session, user.id, SummaryKind.week, date(2026, 8, 31))
    assert day is not None and week is not None
    assert [c["purpose"] for c in fake_llm.calls] == ["summary", "summary"]
    fake_llm.queue(FakeLLM.text("not json"))
    await nightly(user.id, yesterday)  # the day exists: only the week is refreshed
    assert len(fake_llm.calls) == 3
    await nightly(9999, yesterday)  # unknown user: nothing happens
    assert len(fake_llm.calls) == 3


# ------------------------------------------------------------------------------ commands


class ProfileRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str | None]] = []

    async def set_my_commands(
        self, commands: list[BotCommand], *, language_code: str | None = None
    ) -> bool:
        self.calls.append(("commands", [c.command for c in commands], language_code))
        return True

    async def set_my_description(
        self, description: str | None = None, *, language_code: str | None = None
    ) -> bool:
        self.calls.append(("description", description, language_code))
        return True

    async def set_my_short_description(
        self, short_description: str | None = None, *, language_code: str | None = None
    ) -> bool:
        self.calls.append(("short", short_description, language_code))
        return True


def test_bot_commands_respect_telegram_limits() -> None:
    for lang in ("en", "ru"):
        cmds = commands.bot_commands(lang)
        assert [c.command for c in cmds] == ["start", "today", "forget_me"]
        for c in cmds:
            assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command)
            assert 1 <= len(c.description) <= 256
        assert 1 <= len(commands.short_description(lang)) <= 120
        assert 1 <= len(commands.description(lang)) <= 512
    assert commands.bot_commands("ru")[1].description == t("ru", "cmd.today")


async def test_apply_bot_profile_sets_both_languages() -> None:
    recorder = ProfileRecorder()
    await commands.apply_bot_profile(recorder)
    kinds = [(kind, lang) for kind, _, lang in recorder.calls]
    assert kinds == [
        ("commands", None),
        ("short", None),
        ("description", None),
        ("commands", "ru"),
        ("short", "ru"),
        ("description", "ru"),
    ]
    assert recorder.calls[3][1] == ["start", "today", "forget_me"]
    assert recorder.calls[5][1] == t("ru", "bot.description")


# ------------------------------------------------------------------------- config & deploy


def test_env_example_documents_every_setting() -> None:
    documented = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if re.match(r"^[A-Z][A-Z0-9_]*=", line)
    }
    fields = {name.upper() for name in Settings.model_fields}
    missing = sorted(fields - documented)
    assert missing == [], f".env.example lacks {missing}"
    compose_only = documented - fields
    assert compose_only <= {"POSTGRES_PASSWORD", "CADDY_DOMAIN"}, sorted(compose_only)


def test_settings_have_the_startup_fields() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.run_migrations is True and s.web_host == "0.0.0.0" and s.web_port == 8080


def test_docker_compose_file_is_valid_yaml_with_bot_and_postgres() -> None:
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"bot", "postgres"} <= set(services)
    assert services["bot"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "DATABASE_URL" in services["bot"]["environment"]
    assert services["postgres"]["image"].startswith("postgres:")


async def test_migrations_upgrade_head_in_a_thread(tmp_path: Path) -> None:
    db = tmp_path / "strikt.db"
    await asyncio.to_thread(app_mod.upgrade_head, f"sqlite+aiosqlite:///{db}")
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("select name from sqlite_master")}
        version = conn.execute("select version_num from alembic_version").fetchone()
    assert {"users", "meals", "proactive_sends", "alembic_version"} <= tables
    assert version is not None and version[0]
    # idempotent: a second run on a migrated database is a no-op
    await asyncio.to_thread(app_mod.upgrade_head, f"sqlite+aiosqlite:///{db}")


def test_run_loop_prefers_uvloop() -> None:
    seen: list[str] = []

    async def probe() -> None:
        seen.append(type(asyncio.get_running_loop()).__module__)

    app_mod.run_loop(probe())
    assert seen and seen[0].startswith("uvloop")


def test_compose_pins_the_container_port_and_binds_loopback() -> None:
    """WEB_PORT from .env is the host port only: the container must keep listening on 8080
    (Dockerfile HEALTHCHECK, Caddy's ``bot:8080``), and plain HTTP is not published to the world."""
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    bot = compose["services"]["bot"]
    assert str(bot["environment"]["WEB_PORT"]) == "8080"
    assert bot["ports"] == ["127.0.0.1:${WEB_PORT:-8080}:8080"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "127.0.0.1:8080/health" in dockerfile
    assert "reverse_proxy bot:8080" in (ROOT / "Caddyfile").read_text(encoding="utf-8")
