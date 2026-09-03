"""aiohttp routes: health, OAuth start/callback, webhooks (POST + HEAD), Telegram passthrough."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.engine import make_session_factory
from strikt.db.models import Measurement, User, Workout
from strikt.events import Event, EventBus, MeasurementEvent, WorkoutEvent
from strikt.integrations import whoop
from strikt.integrations.oauth import link_secret, sign_user, start_url
from strikt.integrations.registry import build_registry
from strikt.web.server import make_app, run_server
from tests.test_integrations_fakes import (
    HAE_PAYLOAD,
    NOW,
    WHOOP_WORKOUT,
    WITHINGS_GETMEAS,
    Router,
    make_settings,
)

API = "https://api.prod.whoop.com/developer"


class Harness:
    def __init__(self, engine: AsyncEngine, clock: FakeClock) -> None:
        self.settings = make_settings()
        self.router = Router()
        self.bus = EventBus()
        self.received: list[Event] = []
        self.notified: list[tuple[int, str]] = []
        self.sessions = make_session_factory(engine)
        self.integrations = build_registry(
            self.settings,
            self.sessions,
            self.bus,
            clock=clock,
            client_factory=self.router.client_factory(),
        )
        self.cipher = TokenCipher(self.settings.token_encryption_key.get_secret_value())
        self.clock = clock

        async def collect(event: Event) -> None:
            self.received.append(event)

        self.bus.subscribe(Event, collect)

    async def notify(self, user: User, message: str) -> None:
        self.notified.append((user.id, message))

    async def telegram(self, request: web.Request) -> web.Response:
        return web.json_response({"telegram": await request.json()})

    def app(self, *, telegram: bool = True) -> web.Application:
        return make_app(
            self.settings,
            self.sessions,
            self.bus,
            self.integrations,
            self.telegram if telegram else None,
            clock=self.clock,
            notify=self.notify,
        )


@pytest.fixture
async def harness(engine: AsyncEngine, clock: FakeClock, user: User) -> Harness:
    return Harness(engine, clock)


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[TestClient[web.Request, web.Application]]:
    async with TestClient(TestServer(harness.app())) as client:
        yield client


async def test_health_lists_providers(client: TestClient[web.Request, web.Application]) -> None:
    response = await client.get("/health")
    assert response.status == 200
    assert await response.json() == {
        "status": "ok",
        "providers": ["apple_health", "whoop", "withings"],
    }


async def test_oauth_start_redirects_only_with_a_valid_signed_link(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
) -> None:
    url = start_url(harness.settings, "whoop", user.id, now=NOW)
    path = url.replace("https://coach.example.com", "")
    response = await client.get(path, allow_redirects=False)
    assert response.status == 302
    location = response.headers["Location"]
    assert location.startswith(whoop.AUTH_URL)
    state = location.split("state=")[1].split("&")[0]
    assert len(state) >= 8
    assert await session.get(
        type(await session.get(User, user.id)), user.id
    )  # session still usable

    bad = await client.get("/oauth/whoop/start?u=forged.link", allow_redirects=False)
    assert bad.status == 400
    assert "invalid or expired" in await bad.text()
    expired_token = sign_user(
        user.id, secret=link_secret(harness.settings), now=NOW - timedelta(days=3)
    )
    assert (await client.get(f"/oauth/whoop/start?u={expired_token}")).status == 400
    unknown_user = sign_user(999_999, secret=link_secret(harness.settings), now=NOW)
    assert (await client.get(f"/oauth/whoop/start?u={unknown_user}")).status == 404
    assert (await client.get(f"/oauth/garmin/start?u={expired_token}")).status == 404

    # webhook providers render their instructions instead of redirecting
    apple = start_url(harness.settings, "apple_health", user.id, now=NOW)
    page = await client.get(apple.replace("https://coach.example.com", ""))
    assert page.status == 200
    text = await page.text()
    assert "/webhooks/apple-health/" in text
    assert "Health Auto Export" in text


async def test_oauth_callback_completes_the_flow_and_notifies(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
) -> None:
    harness.router.json(
        "POST",
        whoop.TOKEN_URL,
        {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600, "token_type": "bearer"},
    )
    harness.router.json(
        "GET",
        f"{API}/v2/user/profile/basic",
        {"user_id": 9012, "email": "e", "first_name": "f", "last_name": "l"},
    )
    harness.router.json("GET", f"{API}/v2/activity/workout", {"records": [WHOOP_WORKOUT]})
    harness.router.json("GET", f"{API}/v2/activity/sleep", {"records": []})
    harness.router.json("GET", f"{API}/v2/recovery", {"records": []})
    harness.router.json("GET", f"{API}/v2/cycle", {"records": []})
    start = start_url(harness.settings, "whoop", user.id, now=NOW).replace(
        "https://coach.example.com", ""
    )
    redirect = await client.get(start, allow_redirects=False)
    state = redirect.headers["Location"].split("state=")[1].split("&")[0]

    page = await client.get(f"/oauth/whoop/callback?code=abc&state={state}")
    assert page.status == 200
    text = await page.text()
    assert "WHOOP подключён" in text
    assert harness.notified and harness.notified[0][0] == user.id
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.external_user_id == "9012"
    assert len((await session.scalars(select(Workout))).all()) == 1
    # initial backfill is stored but not announced on the bus (no 7-day spam)
    assert harness.received == []

    replay = await client.get(f"/oauth/whoop/callback?code=abc&state={state}")
    assert replay.status == 400
    assert (await client.get("/oauth/nope/callback?code=a&state=b")).status == 404


async def test_apple_health_webhook_publishes_events(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
) -> None:
    info = await harness.integrations["apple_health"].connect(session, user)
    await session.commit()
    token = info.extra["token"]
    response = await client.post(f"/webhooks/apple-health/{token}", json=HAE_PAYLOAD)
    assert response.status == 202
    body = await response.json()
    assert body["accepted"] == 7 and body["events"] == 7
    assert sum(isinstance(e, MeasurementEvent) for e in harness.received) == 5
    assert sum(isinstance(e, WorkoutEvent) for e in harness.received) == 1
    assert len((await session.scalars(select(Measurement))).all()) == 5
    # underscore spelling works too; wrong token is 404; HEAD is 200
    assert (await client.post(f"/webhooks/apple_health/{token}", json=HAE_PAYLOAD)).status == 202
    assert (await client.post("/webhooks/apple-health/nope", json=HAE_PAYLOAD)).status == 404
    assert (await client.post("/webhooks/apple-health", json=HAE_PAYLOAD)).status == 404
    head = await client.head(f"/webhooks/apple-health/{token}")
    assert head.status == 200


async def test_whoop_webhook_route_verifies_signature(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
) -> None:
    await repo.set_integration_tokens(
        session,
        harness.cipher,
        user.id,
        "whoop",
        access_token="acc",
        refresh_token="ref",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
        external_user_id="9012",
    )
    await session.commit()
    harness.router.json("GET", f"{API}/v2/activity/workout/{WHOOP_WORKOUT['id']}", WHOOP_WORKOUT)
    payload: dict[str, Any] = {
        "user_id": 9012,
        "id": WHOOP_WORKOUT["id"],
        "type": "workout.updated",
        "trace_id": "web-1",
    }
    body = json.dumps(payload).encode()
    ts = str(int(NOW.timestamp() * 1000))
    headers = {
        "X-WHOOP-Signature": whoop.compute_signature("whoop-secret", ts, body),
        "X-WHOOP-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }
    unsigned = await client.post(
        "/webhooks/whoop", data=body, headers={"Content-Type": "application/json"}
    )
    assert unsigned.status == 401
    signed = await client.post("/webhooks/whoop", data=body, headers=headers)
    assert signed.status == 200
    assert await signed.text() == "ok"
    assert sum(isinstance(e, WorkoutEvent) for e in harness.received) == 1
    assert len((await session.scalars(select(Workout))).all()) == 1


async def test_withings_webhook_head_and_post(
    client: TestClient[web.Request, web.Application],
    harness: Harness,
    user: User,
    session: AsyncSession,
) -> None:
    await repo.set_integration_tokens(
        session,
        harness.cipher,
        user.id,
        "withings",
        access_token="acc",
        refresh_token="ref",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
        external_user_id="363",
    )
    await session.commit()
    harness.router.json("POST", "https://wbsapi.withings.net/measure", WITHINGS_GETMEAS)
    assert (await client.head("/webhooks/withings")).status == 200
    response = await client.post(
        "/webhooks/withings",
        data="userid=363&appli=1&startdate=1756800000&enddate=1756884000",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status == 200
    assert sum(isinstance(e, MeasurementEvent) for e in harness.received) == 4
    assert (await client.post("/webhooks/garmin", data="x")).status == 404


async def test_telegram_route_only_when_handler_given(harness: Harness) -> None:
    async with TestClient(TestServer(harness.app())) as client:
        response = await client.post("/telegram", json={"update_id": 1})
        assert response.status == 200
        assert await response.json() == {"telegram": {"update_id": 1}}
    async with TestClient(TestServer(harness.app(telegram=False))) as client:
        assert (await client.post("/telegram", json={})).status == 404


async def test_run_server_binds_a_port(harness: Harness) -> None:
    runner = await run_server(harness.app(), "127.0.0.1", 0)
    try:
        assert runner.addresses
    finally:
        await runner.cleanup()
