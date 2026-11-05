"""Withings: getmeas decoding, OAuth, notify subscription, webhook, pagination, refresh."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock, ensure_utc
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import IntegrationStatus, Measurement, MeasurementType, User
from strikt.events import MeasurementEvent
from strikt.integrations import withings
from strikt.integrations.base import WebhookRequest
from strikt.integrations.withings import WithingsIntegration
from tests.test_integrations_fakes import NOW, WITHINGS_GETMEAS, Router, form, make_settings

TOKEN_BODY: dict[str, Any] = {
    "status": 0,
    "body": {
        "userid": 363,
        "access_token": "wacc",
        "refresh_token": "wref",
        "expires_in": 10800,
        "scope": "user.info,user.metrics,user.activity",
        "csrf_token": "x",
        "token_type": "Bearer",
    },
}


# ---------------------------------------------------------------------------------- decoding


def test_decode_value_power_of_ten() -> None:
    assert withings.decode_value(65750, -3) == 65.75
    assert withings.decode_value(20, -1) == 2.0
    assert withings.decode_value(14, 0) == 14.0
    assert withings.decode_value("213", "-1") == 21.3
    assert withings.decode_value(None, -1) is None
    assert withings.decode_value("x", -1) is None


def test_decode_groups_maps_known_types_and_skips_ambiguous() -> None:
    readings = withings.decode_groups(WITHINGS_GETMEAS["body"]["measuregrps"])
    by_type = {r.meastype: r for r in readings}
    assert set(by_type) == {1, 5, 6, 8}  # 12 (temperature) ignored, grpid 12346 ambiguous
    weight = by_type[1]
    assert weight.type == MeasurementType.weight
    assert weight.value == 65.75
    assert weight.unit == "kg"
    assert weight.metric == "weight"
    assert weight.grpid == 12345
    assert weight.model == "Body+"
    assert weight.measured_at == datetime(2026, 9, 3, 7, 12, tzinfo=UTC).astimezone(UTC).replace(
        tzinfo=UTC
    ) - timedelta(hours=4)
    assert by_type[6].type == MeasurementType.bodyfat
    assert by_type[6].value == 21.3
    assert by_type[5].type == MeasurementType.other
    assert by_type[5].metric == "lean_mass_kg"
    assert by_type[5].value == 51.75
    assert by_type[8].value == 14.0
    ambiguous = withings.decode_groups(
        WITHINGS_GETMEAS["body"]["measuregrps"], skip_ambiguous=False
    )
    assert {r.grpid for r in ambiguous} == {12345, 12346}
    assert withings.decode_groups("nope") == []
    assert withings.decode_groups([{"grpid": "x"}, 5, {"grpid": 1, "date": 1, "category": 2}]) == []


def test_parse_notification_form_and_query() -> None:
    fields = withings.parse_notification(
        b"userid=12345&appli=1&startdate=1530576000&enddate=1530698753", {}
    )
    assert fields == {
        "userid": "12345",
        "appli": "1",
        "startdate": "1530576000",
        "enddate": "1530698753",
    }
    assert withings.parse_notification(b"", {"userid": "7"}) == {"userid": "7"}
    assert withings.parse_notification(b"\xff\xfe", {}) == {}


# ------------------------------------------------------------------------------ integration


def make_integration(router: Router, settings: Any, clock: FakeClock) -> WithingsIntegration:
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    return WithingsIntegration(
        settings, cipher=cipher, clock=clock, client_factory=router.client_factory()
    )


async def seed_tokens(
    session: AsyncSession, cipher: TokenCipher, user: User, *, expires_at: datetime
) -> None:
    await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "withings",
        access_token="wacc",
        refresh_token="wref",
        expires_at=expires_at,
        now=NOW,
        external_user_id="363",
    )
    await session.commit()


async def test_connect_authorize_url(session: AsyncSession, user: User, clock: FakeClock) -> None:
    integration = make_integration(Router(), make_settings(), clock)
    info = await integration.connect(session, user)
    assert info.kind == "oauth"
    assert info.url is not None
    assert info.url.startswith(withings.AUTH_URL + "?response_type=code&client_id=withings-client")
    assert "scope=user.info%2Cuser.metrics%2Cuser.activity" in info.url
    assert "redirect_uri=https%3A%2F%2Fcoach.example.com%2Foauth%2Fwithings%2Fcallback" in info.url
    assert f"state={info.extra['state']}" in info.url
    unconfigured = make_integration(Router(), make_settings(withings_client_secret=None), clock)
    assert (await unconfigured.connect(session, user)).kind == "instructions"


async def test_callback_requesttoken_subscribe_and_initial_sync(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    router = Router()
    router.json("POST", withings.TOKEN_URL, TOKEN_BODY)
    router.json("POST", withings.NOTIFY_URL, {"status": 0, "body": {}})
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    integration = make_integration(router, settings, clock)
    info = await integration.connect(session, user)
    await session.commit()

    result_user, message = await integration.handle_callback(
        session, {"code": "c0de", "state": info.extra["state"]}
    )
    await session.commit()
    assert result_user is not None
    assert "Withings подключён" in message
    token_call = form(router.requests("POST", withings.TOKEN_URL)[0])
    assert token_call["action"] == "requesttoken"
    assert token_call["grant_type"] == "authorization_code"
    assert token_call["code"] == "c0de"
    assert token_call["client_secret"] == "withings-secret"
    assert token_call["redirect_uri"] == "https://coach.example.com/oauth/withings/callback"
    notify_calls = router.requests("POST", withings.NOTIFY_URL)
    assert [form(c)["appli"] for c in notify_calls] == ["1", "4", "16"]  # weight, pressure, steps
    notify_call = notify_calls[0]
    assert notify_call.headers["Authorization"] == "Bearer wacc"
    assert form(notify_call)["action"] == "subscribe"
    assert form(notify_call)["callbackurl"] == "https://coach.example.com/webhooks/withings"
    getmeas = form(router.requests("POST", withings.MEASURE_URL)[0])
    assert getmeas["action"] == "getmeas"
    assert getmeas["meastypes"] == "1,5,6,8,9,10,11,54,71,76,77,88,91,155,168,169,170,226,227"
    assert getmeas["category"] == "1"
    assert "startdate" in getmeas and "enddate" in getmeas  # first sync: 30-day window
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None
    assert row.external_user_id == "363"
    assert row.status == IntegrationStatus.connected
    rows = (await session.scalars(select(Measurement).where(Measurement.user_id == user.id))).all()
    assert len(rows) == 4
    assert "4" in message


async def test_callback_failures(session: AsyncSession, user: User, clock: FakeClock) -> None:
    router = Router()
    router.json("POST", withings.TOKEN_URL, {"status": 503, "error": "Invalid Params"})
    integration = make_integration(router, make_settings(), clock)
    info = await integration.connect(session, user)
    _u, failed = await integration.handle_callback(
        session, {"code": "x", "state": info.extra["state"]}
    )
    assert "не принял" in failed
    _u2, expired = await integration.handle_callback(session, {"code": "x", "state": "nope"})
    assert "устарела" in expired or "expired" in expired
    info2 = await integration.connect(session, user)
    _u3, denied = await integration.handle_callback(session, {"state": info2.extra["state"]})
    assert "не выдан" in denied


async def test_sync_uses_lastupdate_paginates_and_dedupes(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    page2 = json.loads(json.dumps(WITHINGS_GETMEAS))
    page2["body"]["measuregrps"] = [
        {
            "grpid": 999,
            "attrib": 2,
            "date": 1756708800,
            "category": 1,
            "model": "Body+",
            "measures": [{"value": 66100, "type": 1, "unit": -3}],
        }
    ]
    page1 = json.loads(json.dumps(WITHINGS_GETMEAS))
    page1["body"]["more"] = 1
    page1["body"]["offset"] = 2

    def getmeas(request: httpx.Request) -> httpx.Response:
        data = form(request)
        return httpx.Response(200, json=page2 if data.get("offset") == "2" else page1)

    router.add("POST", withings.MEASURE_URL, getmeas)
    integration = make_integration(router, settings, clock)
    since = NOW - timedelta(days=3)
    events = await integration.sync(session, user, since)
    await session.commit()
    calls = router.requests("POST", withings.MEASURE_URL)
    assert len(calls) == 2
    assert form(calls[0])["lastupdate"] == str(int(since.timestamp()))
    assert "offset" not in form(calls[0])
    assert form(calls[1])["offset"] == "2"
    assert len(events) == 5
    assert all(isinstance(e, MeasurementEvent) for e in events)
    weights = [e for e in events if e.type == "weight"]  # type: ignore[attr-defined]
    assert sorted(e.value for e in weights) == [65.75, 66.1]  # type: ignore[attr-defined]
    assert {e.type for e in events} == {"weight", "bodyfat", "lean_mass_kg", "fat_mass_kg"}  # type: ignore[attr-defined]
    # replay: nothing new, no events, no duplicate rows
    again = await integration.sync(session, user, since)
    await session.commit()
    assert again == []
    rows = (await session.scalars(select(Measurement).where(Measurement.user_id == user.id))).all()
    assert len(rows) == 5
    lean = next(r for r in rows if r.type == MeasurementType.other and r.note == "lean_mass_kg")
    assert lean.value == 51.75
    assert lean.raw is not None and lean.raw["grpid"] == 12345
    # next sync without an explicit cursor uses last_sync_at - 1h
    await integration.sync(session, user, None)
    third = form(router.requests("POST", withings.MEASURE_URL)[-1])
    assert third["lastupdate"] == str(int((NOW - timedelta(hours=1)).timestamp()))


async def test_sync_refreshes_expired_token(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW - timedelta(minutes=5))
    router = Router()
    fresh = json.loads(json.dumps(TOKEN_BODY))
    fresh["body"]["access_token"] = "wacc2"
    fresh["body"]["refresh_token"] = "wref2"
    router.json("POST", withings.TOKEN_URL, fresh)
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    integration = make_integration(router, settings, clock)
    events = await integration.sync(session, user, None)
    await session.commit()
    assert len(events) == 4
    refresh = form(router.requests("POST", withings.TOKEN_URL)[0])
    assert refresh == {
        "action": "requesttoken",
        "grant_type": "refresh_token",
        "client_id": "withings-client",
        "client_secret": "withings-secret",
        "refresh_token": "wref",
    }
    assert (
        router.requests("POST", withings.MEASURE_URL)[0].headers["Authorization"] == "Bearer wacc2"
    )
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None
    assert repo.integration_tokens(cipher, row).refresh_token == "wref2"


async def test_sync_marks_expired_when_status_401_and_refresh_fails(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    router.json("POST", withings.MEASURE_URL, {"status": 401, "error": "invalid token"})
    router.json("POST", withings.TOKEN_URL, {"status": 503, "error": "Invalid Params"})
    integration = make_integration(router, settings, clock)
    assert await integration.sync(session, user, None) == []
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None
    assert row.status == IntegrationStatus.expired


async def test_webhook_head_and_post(session: AsyncSession, user: User, clock: FakeClock) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    integration = make_integration(router, settings, clock)

    head = WebhookRequest(
        provider="withings",
        method="HEAD",
        path="/webhooks/withings",
        headers={},
        query={},
        body=b"",
    )
    response, events = await integration.handle_webhook(session, head)
    assert response.status == 200 and events == []
    assert not router.calls

    post = WebhookRequest(
        provider="withings",
        method="POST",
        path="/webhooks/withings",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        query={},
        body=b"userid=363&appli=1&startdate=1756800000&enddate=1756884000",
    )
    response, events = await integration.handle_webhook(session, post)
    await session.commit()
    assert response.status == 200
    assert len(events) == 4
    getmeas = form(router.requests("POST", withings.MEASURE_URL)[0])
    # the notification's window is untrusted input: the fetch runs from our own cursor
    assert "startdate" not in getmeas or getmeas["startdate"] != "1756800000"
    assert "enddate" not in getmeas or getmeas["enddate"] != "1756884000"
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None and row.last_sync_at is None  # only sync() moves the cursor
    # replayed notification (Withings retries) inside a minute: throttled, no upstream call
    response, events = await integration.handle_webhook(session, post)
    assert events == [] and response.body == "throttled"
    assert len(router.requests("POST", withings.MEASURE_URL)) == 1
    clock.advance(timedelta(minutes=2))
    response, events = await integration.handle_webhook(session, post)
    assert events == []  # re-fetched, but the readings were already imported
    assert len(router.requests("POST", withings.MEASURE_URL)) == 2

    unknown = WebhookRequest(
        provider="withings",
        method="POST",
        path="/webhooks/withings",
        headers={},
        query={},
        body=b"userid=1&appli=1&startdate=1&enddate=2",
    )
    response, events = await integration.handle_webhook(session, unknown)
    assert response.status == 200 and events == []
    other_appli = WebhookRequest(
        provider="withings",
        method="POST",
        path="/webhooks/withings",
        headers={},
        query={},
        body=b"userid=363&appli=44&date=2026-09-03",
    )
    assert (await integration.handle_webhook(session, other_appli))[0].body == "ignored"
    missing = WebhookRequest(
        provider="withings",
        method="POST",
        path="/webhooks/withings",
        headers={},
        query={},
        body=b"",
    )
    assert (await integration.handle_webhook(session, missing))[0].status == 400


async def test_spoofed_webhook_cannot_skip_a_weigh_in(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    """An attacker POSTs userid + a bogus window: the cursor must stay where sync() left it, so
    the next poll (lastupdate = cursor - 1 h) still imports the reading."""
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=10))
    await repo.set_integration_status(
        session, user.id, "withings", IntegrationStatus.connected, last_sync_at=NOW
    )
    await session.commit()
    router = Router()
    router.json("POST", withings.MEASURE_URL, {"status": 0, "body": {"measuregrps": []}})
    integration = make_integration(router, settings, clock)
    clock.advance(timedelta(hours=2))
    spoof = WebhookRequest(
        provider="withings",
        method="POST",
        path="/webhooks/withings",
        headers={},
        query={},
        body=b"userid=363&appli=1&startdate=0&enddate=1",
    )
    response, events = await integration.handle_webhook(session, spoof)
    assert response.status == 200 and events == [], response
    getmeas = form(router.requests("POST", withings.MEASURE_URL)[0])
    assert getmeas["lastupdate"] == str(int((NOW - timedelta(hours=1)).timestamp()))
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None and ensure_utc(row.last_sync_at) == NOW


# ------------------------------------------------------------- pressure, body comp, activity

BPM_GROUP: dict[str, Any] = {
    "grpid": 555,
    "attrib": 0,
    "date": 1788405720,  # 2026-09-03 07:22 +04:00
    "category": 1,
    "model": "BPM Connect",
    "measures": [
        {"value": 128, "type": 10, "unit": 0},
        {"value": 84, "type": 9, "unit": 0},
        {"value": 66, "type": 11, "unit": 0},
    ],
}
SCAN_GROUP: dict[str, Any] = {
    "grpid": 556,
    "attrib": 0,
    "date": 1788405180,
    "category": 1,
    "model": "Body Scan",
    "measures": [
        {"value": 9, "type": 170, "unit": 0},
        {"value": 1780, "type": 226, "unit": 0},
        {"value": 38, "type": 227, "unit": 0},
    ],
}
ACTIVITY: dict[str, Any] = {
    "status": 0,
    "body": {
        "activities": [
            {
                "date": "2026-09-02",
                "timezone": "Asia/Dubai",
                "steps": 11234,
                "distance": 8120.5,
                "calories": 512.3,
                "totalcalories": 2711.9,
                "active": 3600,
                "is_tracker": False,
            },
            {"date": "2026-09-03", "timezone": "Asia/Dubai", "steps": 0, "calories": 0},
            {"date": "not a date", "steps": 5},
        ],
        "more": False,
        "offset": 0,
    },
}


def test_decode_groups_reads_pressure_pulse_and_body_scan_extras() -> None:
    readings = {r.metric: r for r in withings.decode_groups([BPM_GROUP, SCAN_GROUP])}
    assert (readings["bp_sys"].type, readings["bp_sys"].value) == (MeasurementType.bp_sys, 128.0)
    assert (readings["bp_dia"].type, readings["bp_dia"].unit) == (MeasurementType.bp_dia, "mmHg")
    assert readings["heart_rate"].value == 66.0
    assert readings["visceral_fat"].value == 9.0
    assert readings["bmr_kcal"].unit == "kcal" and readings["metabolic_age"].value == 38.0


def test_decode_activities_files_a_day_at_local_noon_and_drops_empty_days() -> None:
    days = withings.decode_activities(ACTIVITY["body"]["activities"])
    assert len(days) == 1
    day = days[0]
    assert day.day == date(2026, 9, 2) and day.steps == 11234 and day.active_kcal == 512.3
    assert day.measured_at == datetime(2026, 9, 2, 8, 0, tzinfo=UTC)  # 12:00 in Dubai


async def test_sync_stores_pressure_and_daily_activity(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    getmeas = json.loads(json.dumps(WITHINGS_GETMEAS))
    getmeas["body"]["measuregrps"] = [BPM_GROUP]
    router = Router()
    router.json("POST", withings.MEASURE_URL, getmeas)
    router.json("POST", withings.MEASURE_V2_URL, ACTIVITY)
    integration = make_integration(router, settings, clock)

    events = await integration.sync(session, user, None)
    await session.commit()
    assert {e.type for e in events} == {"bp_sys", "bp_dia", "heart_rate"}  # type: ignore[attr-defined]
    activity_call = form(router.requests("POST", withings.MEASURE_V2_URL)[0])
    assert activity_call["action"] == "getactivity"
    assert (
        activity_call["startdateymd"] == "2026-08-04"
        and activity_call["enddateymd"] == "2026-09-03"
    )
    assert "steps" in activity_call["data_fields"]
    rows = (await session.scalars(select(Measurement).where(Measurement.user_id == user.id))).all()
    steps = [r for r in rows if r.type == MeasurementType.steps]
    assert [(r.value, r.unit) for r in steps] == [(11234.0, "steps")]
    active = [r for r in rows if r.note == "active_kcal"]
    assert [r.value for r in active] == [512.3]

    # the day goes on: the same day's steps are updated in place, not duplicated
    more = json.loads(json.dumps(ACTIVITY))
    more["body"]["activities"][0]["steps"] = 13000
    router.routes = [r for r in router.routes if r[1] != withings.MEASURE_V2_URL]
    router.json("POST", withings.MEASURE_V2_URL, more)
    await integration.sync(session, user, None)
    await session.commit()
    second = form(router.requests("POST", withings.MEASURE_V2_URL)[-1])
    assert "lastupdate" in second and "startdateymd" not in second
    rows = (
        await session.scalars(select(Measurement).where(Measurement.type == MeasurementType.steps))
    ).all()
    assert [r.value for r in rows] == [13000.0]


async def test_activity_failure_never_breaks_the_weight_sync(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    router.json("POST", withings.MEASURE_V2_URL, {"status": 401, "error": "scope"})
    integration = make_integration(router, settings, clock)
    events = await integration.sync(session, user, None)
    assert len(events) == 4
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None and row.status == IntegrationStatus.connected


async def test_pressure_and_activity_notifications_fetch_the_right_data(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    getmeas = json.loads(json.dumps(WITHINGS_GETMEAS))
    getmeas["body"]["measuregrps"] = [BPM_GROUP]
    router = Router()
    router.json("POST", withings.MEASURE_URL, getmeas)
    router.json("POST", withings.MEASURE_V2_URL, ACTIVITY)
    integration = make_integration(router, settings, clock)

    def post(body: bytes) -> WebhookRequest:
        return WebhookRequest(
            provider="withings",
            method="POST",
            path="/webhooks/withings",
            headers={},
            query={},
            body=body,
        )

    response, events = await integration.handle_webhook(
        session, post(b"userid=363&appli=16&date=2026-09-02")
    )
    assert response.status == 200 and events == []
    assert len(router.requests("POST", withings.MEASURE_V2_URL)) == 1
    assert not router.requests("POST", withings.MEASURE_URL)
    clock.advance(timedelta(minutes=2))
    response, events = await integration.handle_webhook(
        session, post(b"userid=363&appli=4&startdate=1&enddate=2")
    )
    assert {e.type for e in events} == {"bp_sys", "bp_dia", "heart_rate"}  # type: ignore[attr-defined]


async def test_a_network_error_on_refresh_is_not_a_dead_connection(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    """A timeout or a 5xx from the token endpoint used to mark the row expired (and now that
    would tell the user "disconnected"); only Withings refusing the refresh token does."""
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW - timedelta(minutes=5))
    router = Router()

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("token endpoint down")

    router.add("POST", withings.TOKEN_URL, down)
    integration = make_integration(router, settings, clock)
    assert await integration.sync(session, user, None) == []
    row = await repo.get_integration(session, user.id, "withings")
    assert row is not None and row.status == IntegrationStatus.connected
    router.routes = []
    router.json("POST", withings.TOKEN_URL, {"status": 503, "error": "invalid refresh_token"})
    user_id = user.id
    await integration.sync(session, user, None)
    session.expunge_all()
    row = await repo.get_integration(session, user_id, "withings")
    assert row is not None and row.status == IntegrationStatus.expired


async def test_the_webhook_throttle_is_per_category(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    router.json("POST", withings.MEASURE_V2_URL, ACTIVITY)
    integration = make_integration(router, settings, clock)

    def post(body: bytes) -> WebhookRequest:
        return WebhookRequest(
            provider="withings",
            method="POST",
            path="/webhooks/withings",
            headers={},
            query={},
            body=body,
        )

    await integration.handle_webhook(session, post(b"userid=363&appli=16&date=2026-09-03"))
    response, events = await integration.handle_webhook(
        session, post(b"userid=363&appli=1&startdate=1&enddate=2")
    )
    assert response.body != "throttled" and len(events) == 4  # steps never hold up a weigh-in


async def test_an_old_connection_is_subscribed_to_the_new_categories_once(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await seed_tokens(session, cipher, user, expires_at=NOW + timedelta(hours=2))
    router = Router()
    router.json("POST", withings.MEASURE_URL, WITHINGS_GETMEAS)
    router.json("POST", withings.NOTIFY_URL, {"status": 0, "body": {}})
    integration = make_integration(router, settings, clock)
    await integration.sync(session, user, None)
    assert [form(c)["appli"] for c in router.requests("POST", withings.NOTIFY_URL)] == [
        "1",
        "4",
        "16",
    ]
    await integration.sync(session, user, None)
    assert len(router.requests("POST", withings.NOTIFY_URL)) == 3  # done, not repeated
