"""WHOOP: signature verification, payload mapping, pagination, token refresh, webhooks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import IntegrationStatus, Recovery, Sleep, User, Workout
from strikt.events import RecoveryEvent, SleepEvent, WorkoutEvent
from strikt.integrations import whoop
from strikt.integrations.base import WebhookRequest
from strikt.integrations.whoop import WhoopIntegration
from tests.test_integrations_fakes import (
    NOW,
    WHOOP_CYCLE,
    WHOOP_RECOVERY,
    WHOOP_SLEEP,
    WHOOP_WORKOUT,
    Router,
    form,
    make_settings,
)

SECRET = "whoop-secret"
API = "https://api.prod.whoop.com/developer"


def signed_headers(body: bytes, *, at: datetime = NOW, secret: str = SECRET) -> dict[str, str]:
    ts = str(int(at.timestamp() * 1000))
    return {
        "X-WHOOP-Signature": whoop.compute_signature(secret, ts, body),
        "X-WHOOP-Signature-Timestamp": ts,
        "Content-Type": "application/json",
    }


# ------------------------------------------------------------------------------- signatures


def test_signature_positive_negative_stale() -> None:
    body = b'{"user_id": 9012, "id": "abc", "type": "workout.updated", "trace_id": "t1"}'
    headers = signed_headers(body)
    assert whoop.verify_signature(SECRET, headers, body, now=NOW)
    # header names are case-insensitive (aiohttp may hand them over in any case)
    lower = {k.lower(): v for k, v in headers.items()}
    assert whoop.verify_signature(SECRET, lower, body, now=NOW)
    assert not whoop.verify_signature(SECRET, headers, body + b" ", now=NOW)
    assert not whoop.verify_signature("other", headers, body, now=NOW)
    assert not whoop.verify_signature(SECRET, {**headers, "X-WHOOP-Signature": "x"}, body, now=NOW)
    assert not whoop.verify_signature(SECRET, {}, body, now=NOW)
    assert not whoop.verify_signature("", headers, body, now=NOW)
    # stale: signed 6 minutes ago
    old = signed_headers(body, at=NOW - timedelta(minutes=6))
    assert not whoop.verify_signature(SECRET, old, body, now=NOW)
    recent = signed_headers(body, at=NOW - timedelta(minutes=4))
    assert whoop.verify_signature(SECRET, recent, body, now=NOW)
    assert not whoop.verify_signature(
        SECRET, {**headers, "X-WHOOP-Signature-Timestamp": "nope"}, body, now=NOW
    )


# ---------------------------------------------------------------------------------- mapping


def test_map_workout_units_and_zones() -> None:
    record = whoop.map_workout(WHOOP_WORKOUT)
    assert record is not None
    assert record.external_id == "ecfc6a15-4661-442f-a9a4-f160dd7afae8"
    assert record.sport == "running"
    assert record.kcal == 375.1  # 1569.34 kJ / 4.184
    assert record.duration_min == 60.0
    assert record.strain == 8.2463
    assert record.avg_hr == 123
    assert record.max_hr == 146
    assert record.zones_min == {
        "z0": 5.0,
        "z1": 10.0,
        "z2": 15.0,
        "z3": 15.0,
        "z4": 10.0,
        "z5": 5.0,
    }
    assert record.distance_m == 1772.77035916
    assert record.raw["percent_recorded"] == 100
    assert "low_hr_coverage" not in record.raw
    assert record.started_at == datetime(2026, 9, 2, 2, 25, 44, 774000, tzinfo=UTC)


def test_map_workout_score_states() -> None:
    pending = {**WHOOP_WORKOUT, "score_state": "PENDING_SCORE"}
    pending.pop("score")
    assert whoop.map_workout(pending) is None
    unscorable = {**WHOOP_WORKOUT, "score_state": "UNSCORABLE"}
    unscorable.pop("score")
    record = whoop.map_workout(unscorable)
    assert record is not None
    assert record.strain is None
    assert record.kcal is None
    assert record.zones_min is None
    low = json.loads(json.dumps(WHOOP_WORKOUT))
    low["score"]["percent_recorded"] = 20
    low_rec = whoop.map_workout(low)
    assert low_rec is not None
    assert low_rec.raw["low_hr_coverage"] is True
    assert whoop.map_workout({"id": "x", "start": "bad", "end": "bad"}) is None
    assert whoop.map_workout({**WHOOP_WORKOUT, "sport_name": "Strength Trainer"}) is not None
    assert whoop.map_workout({**WHOOP_WORKOUT, "sport_name": None}).sport == "activity"  # type: ignore[union-attr]


def test_map_sleep_stages_and_naps() -> None:
    record = whoop.map_sleep(WHOOP_SLEEP)
    assert record is not None
    assert record.cycle_id == 93845
    assert record.in_bed_min == 470.0
    assert record.asleep_min == 420.0  # light 210 + deep 90 + rem 120
    assert record.stages_min == {"light": 210.0, "deep": 90.0, "rem": 120.0, "awake": 30.0}
    assert record.performance_pct == 87.0
    assert record.respiratory_rate == 16.11328125
    assert record.disturbances == 7
    assert record.nap is False
    assert record.raw["sleep_cycle_count"] == 4
    assert whoop.map_sleep({**WHOOP_SLEEP, "score_state": "PENDING_SCORE"}) is None
    nap = whoop.map_sleep({**WHOOP_SLEEP, "nap": True})
    assert nap is not None
    assert nap.nap is True


def test_map_recovery_date_follows_sleep_end_in_user_timezone() -> None:
    sleep_end = datetime(2026, 9, 2, 22, 30, tzinfo=UTC)  # 02:30 next day in Dubai
    record = whoop.map_recovery(WHOOP_RECOVERY, tz="Asia/Dubai", sleep_end=sleep_end)
    assert record is not None
    assert record.external_id == "93845"
    assert record.day == date(2026, 9, 3)
    assert record.score == 44.0
    assert record.rhr == 64.0
    assert record.hrv_ms == 31.813562
    assert record.spo2 == 95.6875
    assert record.skin_temp_c == 33.7
    assert record.raw["user_calibrating"] is False
    # without a sleep it falls back to created_at (04:12Z = 08:12 Dubai)
    fallback = whoop.map_recovery(WHOOP_RECOVERY, tz="Asia/Dubai")
    assert fallback is not None
    assert fallback.day == date(2026, 9, 3)
    assert whoop.map_recovery({**WHOOP_RECOVERY, "score_state": "PENDING_SCORE"}, tz="UTC") is None
    unscored = whoop.map_recovery({**WHOOP_RECOVERY, "score_state": "UNSCORABLE"}, tz="UTC")
    assert unscored is not None
    assert unscored.score is None


def test_cycle_summary_converts_strain_kcal() -> None:
    summary = whoop.cycle_summary(WHOOP_CYCLE)
    assert summary is not None
    assert summary["strain"] == 5.2951527
    assert summary["kcal"] == 1981.0
    assert whoop.cycle_summary({**WHOOP_CYCLE, "score_state": "PENDING_SCORE"}) is None


# ------------------------------------------------------------------------------ integration


def collection(records: list[dict[str, Any]], next_token: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"records": records}
    if next_token:
        payload["next_token"] = next_token
    return payload


def make_router(*, workouts_pages: list[list[dict[str, Any]]] | None = None) -> Router:
    router = Router()
    pages = workouts_pages or [[WHOOP_WORKOUT]]

    def workouts(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("nextToken")
        index = int(token[1:]) if token else 0
        next_token = f"p{index + 1}" if index + 1 < len(pages) else None
        return httpx.Response(200, json=collection(pages[index], next_token))

    router.add("GET", f"{API}/v2/activity/workout", workouts)
    router.json("GET", f"{API}/v2/activity/sleep", collection([WHOOP_SLEEP]))
    router.json("GET", f"{API}/v2/recovery", collection([WHOOP_RECOVERY]))
    router.json("GET", f"{API}/v2/cycle", collection([WHOOP_CYCLE]))
    router.json(
        "GET",
        f"{API}/v2/user/profile/basic",
        {"user_id": 9012, "email": "x@y.z", "first_name": "T", "last_name": "U"},
    )
    return router


def make_integration(router: Router, settings: Any, clock: FakeClock) -> WhoopIntegration:
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    return WhoopIntegration(
        settings, cipher=cipher, clock=clock, client_factory=router.client_factory()
    )


async def seed_tokens(
    session: AsyncSession,
    cipher: TokenCipher,
    user: User,
    *,
    expires_at: datetime,
    access: str = "acc",
    refresh: str = "ref",
) -> None:
    await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "whoop",
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        now=NOW,
        external_user_id="9012",
    )
    await session.commit()


async def test_connect_builds_authorize_url_with_state(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    integration = make_integration(Router(), settings, clock)
    info = await integration.connect(session, user)
    assert info.kind == "oauth"
    assert info.url is not None
    assert info.url.startswith(whoop.AUTH_URL + "?")
    assert "client_id=whoop-client" in info.url
    assert "scope=offline+read%3Arecovery+read%3Acycles" in info.url
    assert "redirect_uri=https%3A%2F%2Fcoach.example.com%2Foauth%2Fwhoop%2Fcallback" in info.url
    state = info.extra["state"]
    assert len(state) >= 8
    assert f"state={state}" in info.url
    assert "WHOOP" in info.instructions
    unconfigured = make_integration(Router(), make_settings(whoop_client_id=None), clock)
    assert (await unconfigured.connect(session, user)).kind == "instructions"


async def test_callback_exchanges_code_stores_tokens_and_backfills(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    router = make_router()
    router.json(
        "POST",
        whoop.TOKEN_URL,
        {
            "access_token": "acc1",
            "refresh_token": "ref1",
            "expires_in": 3600,
            "scope": "offline read:workout",
            "token_type": "bearer",
        },
    )
    integration = make_integration(router, settings, clock)
    info = await integration.connect(session, user)
    await session.commit()
    state = info.extra["state"]

    result_user, message = await integration.handle_callback(
        session, {"code": "the-code", "state": state}
    )
    await session.commit()
    assert result_user is not None and result_user.id == user.id
    assert "WHOOP подключён" in message  # user speaks ru
    token_call = router.requests("POST", whoop.TOKEN_URL)[0]
    assert form(token_call) == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "client_id": "whoop-client",
        "client_secret": "whoop-secret",
        "redirect_uri": "https://coach.example.com/oauth/whoop/callback",
    }
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.status == IntegrationStatus.connected
    assert row.external_user_id == "9012"
    assert row.access_token_enc != "acc1"  # encrypted at rest
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    tokens = repo.integration_tokens(cipher, row)
    assert (tokens.access_token, tokens.refresh_token) == ("acc1", "ref1")
    assert tokens.expires_at is not None
    # initial 7-day sync ran: start param = now - 7d, bearer = new token
    workouts_call = router.requests("GET", f"{API}/v2/activity/workout")[0]
    assert workouts_call.headers["Authorization"] == "Bearer acc1"
    assert workouts_call.url.params["start"] == "2026-08-27T08:00:00.000Z"
    assert workouts_call.url.params["limit"] == "25"
    assert (await session.scalars(select(Workout))).all()
    assert (await session.scalars(select(Sleep))).all()
    assert (await session.scalars(select(Recovery))).all()
    assert "1" in message  # counts in the message
    # the state is single use
    again, msg = await integration.handle_callback(session, {"code": "x", "state": state})
    assert again is None
    assert "устарела" in msg or "expired" in msg


async def test_callback_denied_and_bad_exchange(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    router = make_router()
    router.json("POST", whoop.TOKEN_URL, {"error": "invalid_client"}, status=401)
    integration = make_integration(router, settings, clock)
    info = await integration.connect(session, user)
    denied_user, denied = await integration.handle_callback(
        session, {"error": "access_denied", "state": info.extra["state"]}
    )
    assert denied_user is not None
    assert "не выдан" in denied
    info2 = await integration.connect(session, user)
    failed_user, failed = await integration.handle_callback(
        session, {"code": "bad", "state": info2.extra["state"]}
    )
    assert failed_user is not None
    assert "не принял" in failed
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.status == IntegrationStatus.error


async def test_sync_paginates_maps_and_dedupes(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=1))
    second = {**WHOOP_WORKOUT, "id": "second-workout", "sport_name": "Weightlifting"}
    router = make_router(workouts_pages=[[second], [WHOOP_WORKOUT]])
    integration = make_integration(router, settings, clock)

    events = await integration.sync(session, user, NOW - timedelta(days=7))
    await session.commit()
    calls = router.requests("GET", f"{API}/v2/activity/workout")
    assert len(calls) == 2
    assert "nextToken" not in calls[0].url.params
    assert calls[1].url.params["nextToken"] == "p1"
    assert calls[1].url.params["start"] == calls[0].url.params["start"]
    assert not router.requests("POST", whoop.TOKEN_URL)  # token still valid, no refresh
    kinds = [type(e).__name__ for e in events]
    assert kinds.count("WorkoutEvent") == 2
    assert kinds.count("SleepEvent") == 1
    assert kinds.count("RecoveryEvent") == 1
    workout_events = [e for e in events if isinstance(e, WorkoutEvent)]
    assert [e.sport for e in workout_events] == ["running", "weightlifting"]  # oldest first
    recovery = next(e for e in events if isinstance(e, RecoveryEvent))
    assert recovery.date == date(2026, 9, 3)  # sleep ended 03:55Z = 07:55 Dubai
    assert recovery.raw["cycle"]["strain"] == 5.2951527
    assert recovery.raw["created"] is True
    sleep_event = next(e for e in events if isinstance(e, SleepEvent))
    assert sleep_event.asleep_min == 420.0
    rows = (await session.scalars(select(Workout).where(Workout.user_id == user.id))).all()
    assert {r.external_id for r in rows} == {"second-workout", WHOOP_WORKOUT["id"]}
    assert rows[0].kcal == 375.1
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.last_sync_at is not None

    # a second sync re-fetches the same records: rows are updated, not duplicated
    events2 = await integration.sync(session, user, NOW - timedelta(days=7))
    await session.commit()
    assert len(events2) == 4
    assert all(e.raw["created"] is False for e in events2)
    rows_after = (await session.scalars(select(Workout).where(Workout.user_id == user.id))).all()
    assert len(rows_after) == 2
    assert len((await session.scalars(select(Recovery))).all()) == 1


async def test_sync_refreshes_expired_token_and_rotates_refresh_token(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW - timedelta(minutes=1))
    router = make_router()
    router.json(
        "POST",
        whoop.TOKEN_URL,
        {
            "access_token": "acc2",
            "refresh_token": "ref2",
            "expires_in": 3600,
            "token_type": "bearer",
        },
    )
    integration = make_integration(router, settings, clock)
    events = await integration.sync(session, user, None)
    await session.commit()
    assert events
    refresh = router.requests("POST", whoop.TOKEN_URL)
    assert len(refresh) == 1
    assert form(refresh[0]) == {
        "grant_type": "refresh_token",
        "refresh_token": "ref",
        "client_id": "whoop-client",
        "client_secret": "whoop-secret",
        "scope": "offline",
    }
    for call in router.requests("GET", API):
        assert call.headers["Authorization"] == "Bearer acc2"
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    tokens = repo.integration_tokens(cipher, row)
    assert tokens.refresh_token == "ref2"
    assert tokens.access_token == "acc2"
    assert tokens.expires_at is not None
    # since=None with no last_sync_at → initial window of 7 days
    first_get = router.requests("GET", f"{API}/v2/activity/workout")[0]
    assert first_get.url.params["start"] == "2026-08-27T08:00:00.000Z"


async def test_sync_retries_once_on_401_then_marks_expired_when_refresh_fails(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=1))
    router = Router()
    router.json(
        "GET", f"{API}/v2/activity/workout", {"error": "Authorization was not valid"}, status=401
    )
    router.json("POST", whoop.TOKEN_URL, {"error": "invalid_grant"}, status=400)
    integration = make_integration(router, settings, clock)
    assert await integration.sync(session, user, None) == []
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.status == IntegrationStatus.expired
    # no more syncing until the user reconnects
    assert await integration.sync(session, user, None) == []


async def test_sync_survives_rate_limit_and_upstream_errors(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=1))
    router = make_router()
    hits: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        if len(hits) == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "0"}, json={})
        return httpx.Response(200, json=collection([WHOOP_SLEEP]))

    router.routes = [r for r in router.routes if not r[1].endswith("/activity/sleep")]
    router.add("GET", f"{API}/v2/activity/sleep", flaky)
    integration = make_integration(router, settings, clock)
    events = await integration.sync(session, user, None)
    assert any(isinstance(e, SleepEvent) for e in events)
    assert len(hits) == 2

    broken = Router()
    broken.json("GET", f"{API}/v2/activity/workout", {"error": "boom"}, status=500)
    integration2 = make_integration(broken, settings, clock)
    assert await integration2.sync(session, user, None) == []
    row = await repo.get_integration(session, user.id, "whoop")
    assert row is not None
    assert row.status == IntegrationStatus.connected


# ---------------------------------------------------------------------------------- webhooks


def webhook_request(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> WebhookRequest:
    body = json.dumps(payload).encode()
    return WebhookRequest(
        provider="whoop",
        method="POST",
        path="/webhooks/whoop",
        headers=headers if headers is not None else signed_headers(body),
        query={},
        body=body,
    )


async def test_webhook_rejects_bad_signature_and_bad_json(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    integration = make_integration(Router(), make_settings(), clock)
    request = webhook_request({"user_id": 9012, "id": "x", "type": "workout.updated"}, headers={})
    response, events = await integration.handle_webhook(session, request)
    assert response.status == 401
    assert events == []
    bad = WebhookRequest(
        provider="whoop",
        method="POST",
        path="/webhooks/whoop",
        headers=signed_headers(b"not json"),
        query={},
        body=b"not json",
    )
    assert (await integration.handle_webhook(session, bad))[0].status == 400
    head = WebhookRequest(
        provider="whoop", method="HEAD", path="/x", headers={}, query={}, body=b""
    )
    assert (await integration.handle_webhook(session, head))[0].status == 200


async def test_webhook_updates_fetch_objects_and_publish_events(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=1))
    router = Router()
    router.json("GET", f"{API}/v2/activity/workout/{WHOOP_WORKOUT['id']}", WHOOP_WORKOUT)
    router.json("GET", f"{API}/v2/activity/sleep/{WHOOP_SLEEP['id']}", WHOOP_SLEEP)
    router.json("GET", f"{API}/v2/cycle/93845/recovery", WHOOP_RECOVERY)
    integration = make_integration(router, settings, clock)

    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {
                "user_id": 9012,
                "id": WHOOP_WORKOUT["id"],
                "type": "workout.updated",
                "trace_id": "t1",
            }
        ),
    )
    assert response.status == 200
    assert len(events) == 1 and isinstance(events[0], WorkoutEvent)
    assert events[0].kcal == 375.1

    # duplicate delivery (same trace id) is acknowledged without work
    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {
                "user_id": 9012,
                "id": WHOOP_WORKOUT["id"],
                "type": "workout.updated",
                "trace_id": "t1",
            }
        ),
    )
    assert response.status == 200 and response.body == "duplicate" and events == []

    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {"user_id": 9012, "id": WHOOP_SLEEP["id"], "type": "sleep.updated", "trace_id": "t2"}
        ),
    )
    assert len(events) == 1 and isinstance(events[0], SleepEvent)

    # v2 recovery events carry the sleep uuid → sleep → cycle → recovery
    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {"user_id": 9012, "id": WHOOP_SLEEP["id"], "type": "recovery.updated", "trace_id": "t3"}
        ),
    )
    assert len(events) == 1 and isinstance(events[0], RecoveryEvent)
    assert events[0].date == date(2026, 9, 3)
    assert router.requests("GET", f"{API}/v2/cycle/93845/recovery")
    await session.commit()
    assert len((await session.scalars(select(Recovery))).all()) == 1

    # deletes
    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {
                "user_id": 9012,
                "id": WHOOP_WORKOUT["id"],
                "type": "workout.deleted",
                "trace_id": "t4",
            }
        ),
    )
    assert response.status == 200 and events == []
    assert (await session.scalars(select(Workout))).all() == []
    response, events = await integration.handle_webhook(
        session,
        webhook_request(
            {"user_id": 9012, "id": WHOOP_SLEEP["id"], "type": "sleep.deleted", "trace_id": "t5"}
        ),
    )
    assert (await session.scalars(select(Sleep))).all() == []
    assert (await session.scalars(select(Recovery))).all() == []  # goes with its sleep

    # unknown user and unknown event types are acknowledged (WHOOP must not retry them)
    response, events = await integration.handle_webhook(
        session,
        webhook_request({"user_id": 1, "id": "x", "type": "workout.updated", "trace_id": "t6"}),
    )
    assert response.status == 200 and events == []
    response, events = await integration.handle_webhook(
        session,
        webhook_request({"user_id": 9012, "id": "x", "type": "cycle.updated", "trace_id": "t7"}),
    )
    assert response.status == 200 and events == []


async def test_webhook_upstream_failure_returns_502_so_whoop_retries(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=1))
    router = Router()
    router.json("GET", f"{API}/v2/activity/workout/", {"error": "boom"}, status=500)
    integration = make_integration(router, settings, clock)
    response, events = await integration.handle_webhook(
        session,
        webhook_request({"user_id": 9012, "id": "w1", "type": "workout.updated", "trace_id": "t8"}),
    )
    assert response.status == 502 and events == []


def test_signature_known_answer_vector() -> None:
    """Independent of ``compute_signature``: base64(HMAC-SHA256(secret, timestamp + body)),
    checked against a value computed outside the module (a wrong algorithm cannot pass)."""
    import base64
    import hashlib
    import hmac

    ts = "1756886400000"
    body = b'{"user_id": 9012, "id": "abc", "type": "workout.updated", "trace_id": "t1"}'
    expected = "jUQ/JgK9Ldkg1mMHTAywMW3tcB1LjkT974F/ZGhXBOg="
    independent = base64.b64encode(
        hmac.new(b"whoop-secret", ts.encode("ascii") + body, hashlib.sha256).digest()
    ).decode("ascii")
    assert independent == expected
    assert whoop.compute_signature("whoop-secret", ts, body) == expected
    assert whoop.compute_signature("whoop-secret", ts, body + b" ") != expected
