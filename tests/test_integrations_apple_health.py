"""Apple Health bridge: both payload dialects, unit conversion, token auth, idempotency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.models import IntegrationStatus, Measurement, MeasurementType, Sleep, User, Workout
from strikt.events import MeasurementEvent, SleepEvent, WorkoutEvent
from strikt.integrations import apple_health
from strikt.integrations.apple_health import AppleHealthIntegration
from strikt.integrations.base import WebhookRequest
from tests.test_integrations_fakes import (
    HAE_PAYLOAD,
    NATIVE_PAYLOAD,
    SIMPLE_PAYLOAD,
    make_settings,
)

TZ = "Asia/Dubai"


# ----------------------------------------------------------------------------------- parsing


def test_parse_when_formats() -> None:
    assert apple_health.parse_when("2026-09-03 07:12:00 +0300", TZ) == datetime(
        2026, 9, 3, 4, 12, tzinfo=UTC
    )
    assert apple_health.parse_when("2026-09-03T04:12:00Z", TZ) == datetime(
        2026, 9, 3, 4, 12, tzinfo=UTC
    )
    # date-only → local midnight in the user's zone (Dubai = UTC+4)
    assert apple_health.parse_when("2026-09-02", TZ) == datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    assert apple_health.parse_when(1788405120, TZ) == datetime(
        2026, 9, 3, 7, 12, tzinfo=UTC
    ).replace(hour=3)
    assert apple_health.parse_when("", TZ) is None
    assert apple_health.parse_when("yesterday", TZ) is None
    assert apple_health.parse_when(True, TZ) is None


def test_normalise_units() -> None:
    assert apple_health.normalise(181.66, "lb", "kg") == 82.4
    assert apple_health.normalise(82.4, "kg", "kg") == 82.4
    assert apple_health.normalise(40.0, "in", "cm") == 101.6
    assert apple_health.normalise(5.1, "km", "m") == 5100.0
    assert apple_health.normalise(1, "mi", "m") == 1609.3
    assert apple_health.normalise(7.5, "hr", "s") == 27000.0
    assert apple_health.normalise(0.048, "s", "ms") == 48.0
    assert apple_health.normalise(418.4, "kJ", "kcal") == 100.0


def test_parse_hae_dialect() -> None:
    parsed = apple_health.parse_payload(HAE_PAYLOAD, tz=TZ)
    assert parsed.dialect == "hae"
    by_metric = {m.metric: m for m in parsed.measurements}
    assert set(by_metric) == {"weight", "bodyfat", "steps", "rhr", "hrv"}
    assert by_metric["weight"].value == 82.4
    assert by_metric["weight"].type == MeasurementType.weight
    assert by_metric["weight"].measured_at == datetime(2026, 9, 3, 4, 12, tzinfo=UTC)
    assert by_metric["weight"].raw["source"] == "Renpho Health"
    assert by_metric["bodyfat"].value == 21.3
    assert by_metric["steps"].value == 8500
    assert by_metric["rhr"].value == 54
    assert by_metric["hrv"].value == 48
    assert parsed.ignored == 1  # the heart_rate window has no resting semantics
    assert len(parsed.sleeps) == 1
    sleep = parsed.sleeps[0]
    assert sleep.started_at == datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    assert sleep.ended_at == datetime(2026, 9, 2, 3, 30, tzinfo=UTC)
    assert sleep.asleep_min == 420.0
    assert sleep.in_bed_min == 480.0
    assert sleep.stages_min == {"light": 210.0, "deep": 90.0, "rem": 120.0}
    assert len(parsed.workouts) == 1
    workout = parsed.workouts[0]
    assert workout.external_id == "550e8400-e29b-41d4-a716-446655440000"
    assert workout.sport == "running"
    assert workout.duration_min == 30.0
    assert workout.kcal == 350
    assert workout.distance_m == 5100.0
    assert parsed.accepted == 7


def test_parse_hae_pounds_and_legacy_names() -> None:
    payload: dict[str, Any] = {
        "data": {
            "metrics": [
                {
                    "name": "weight_body_mass",
                    "units": "lb",
                    "data": [{"qty": 200, "date": "2026-09-03T07:00:00+04:00"}],
                },
                {
                    "name": "unknown_metric",
                    "units": "x",
                    "data": [{"qty": 1, "date": "2026-09-03"}],
                },
                {
                    "name": "body_fat_percentage",
                    "units": "%",
                    "data": [{"qty": "bad", "date": "2026-09-03"}],
                },
            ],
            "workouts": [{"name": "Yoga", "start": "2026-09-03 06:00:00 +0400", "end": "bad"}],
        }
    }
    parsed = apple_health.parse_payload(payload, tz=TZ)
    assert len(parsed.measurements) == 1
    assert parsed.measurements[0].value == 90.718
    assert parsed.ignored == 3


def test_parse_native_samples_dialect() -> None:
    parsed = apple_health.parse_payload(NATIVE_PAYLOAD, tz=TZ)
    assert parsed.dialect == "samples"
    by_metric = {m.metric: m for m in parsed.measurements}
    assert set(by_metric) == {"weight", "bodyfat", "steps", "rhr", "hrv"}
    assert by_metric["weight"].value == 82.4
    assert by_metric["steps"].measured_at == datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
    assert parsed.sleeps[0].asleep_min == 450.0
    assert parsed.sleeps[0].stages_min == {
        "light": 210.0,
        "deep": 90.0,
        "rem": 120.0,
        "awake": 30.0,
    }
    assert parsed.workouts[0].kcal == 350
    assert parsed.workouts[0].distance_m == 5100
    assert parsed.workouts[0].duration_min == 30.0
    assert parsed.ignored == 0
    junk = apple_health.parse_payload(
        {"samples": [1, {"type": "weight"}, {"type": "nope", "value": 1}]}, tz=TZ
    )
    assert junk.ignored == 3


def test_parse_simple_shortcut_dialect() -> None:
    parsed = apple_health.parse_payload(SIMPLE_PAYLOAD, tz=TZ)
    assert parsed.dialect == "simple"
    by_metric = {m.metric: m for m in parsed.measurements}
    assert by_metric["weight"].value == 103.4
    assert by_metric["weight"].measured_at == datetime(2026, 9, 3, 3, 15, tzinfo=UTC)
    assert by_metric["steps"].value == 6400
    assert by_metric["rhr"].value == 52
    assert by_metric["hrv"].value == 61
    assert len(parsed.sleeps) == 1
    assert parsed.sleeps[0].asleep_min == 420
    assert parsed.sleeps[0].started_at == datetime(2026, 9, 2, 19, 40, tzinfo=UTC)
    # date-only with hours-only sleep: the end is derived from start + hours
    minimal = apple_health.parse_payload(
        {
            "weight_kg": "104",
            "date": "2026-09-03",
            "sleep": {"start": "2026-09-02T23:30:00+04:00", "hours": 7},
        },
        tz=TZ,
    )
    assert minimal.measurements[0].value == 104.0
    assert minimal.sleeps[0].ended_at == datetime(2026, 9, 3, 2, 30, tzinfo=UTC)
    assert apple_health.parse_payload({"weight_kg": 80}, tz=TZ).ignored == 1  # no date
    assert apple_health.parse_payload([], tz=TZ).ignored == 1


# ------------------------------------------------------------------------------ integration


async def test_connect_issues_token_url_and_instructions(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    integration = AppleHealthIntegration(make_settings(), clock=clock)
    info = await integration.connect(session, user)
    await session.commit()
    assert info.kind == "webhook"
    token = info.extra["token"]
    assert len(token) >= 32
    assert info.url == f"https://coach.example.com/webhooks/apple-health/{token}"
    assert info.url in info.instructions
    assert "Health Auto Export" in info.instructions
    assert "Команды" in info.instructions  # ru user gets the Russian text
    assert "X-Strikt-Secret" in info.instructions
    row = await repo.get_integration(session, user.id, "apple_health")
    assert row is not None
    assert row.webhook_token == token
    assert row.status == IntegrationStatus.connected
    # calling again keeps the same token (the user may already have configured the app)
    again = await integration.connect(session, user)
    assert again.extra["token"] == token
    user.language = "en"
    english = await integration.connect(session, user)
    assert "Shortcuts" in english.instructions
    assert "Команды" not in english.instructions
    assert (await integration.handle_callback(session, {}))[0] is None
    assert await integration.sync(session, user, None) == []


def make_request(
    token: str | None, payload: Any, headers: dict[str, str] | None = None
) -> WebhookRequest:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return WebhookRequest(
        provider="apple_health",
        method="POST",
        path=f"/webhooks/apple-health/{token}",
        headers=headers or {"Content-Type": "application/json"},
        query={},
        body=body,
        path_token=token,
    )


async def test_webhook_auth_and_idempotent_ingest(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    integration = AppleHealthIntegration(make_settings(), clock=clock)
    info = await integration.connect(session, user)
    await session.commit()
    token = info.extra["token"]

    response, events = await integration.handle_webhook(session, make_request("wrong", HAE_PAYLOAD))
    assert response.status == 404 and events == []
    response, events = await integration.handle_webhook(session, make_request(None, HAE_PAYLOAD))
    assert response.status == 404
    response, events = await integration.handle_webhook(
        session, make_request(token, HAE_PAYLOAD, {"X-Strikt-Secret": "nope"})
    )
    assert response.status == 401 and events == []
    response, events = await integration.handle_webhook(session, make_request(token, b"{not json"))
    assert response.status == 400
    head = WebhookRequest(
        provider="apple_health",
        method="HEAD",
        path="/x",
        headers={},
        query={},
        body=b"",
        path_token=token,
    )
    assert (await integration.handle_webhook(session, head))[0].status == 200

    response, events = await integration.handle_webhook(
        session, make_request(token, HAE_PAYLOAD, {"x-strikt-secret": token})
    )
    await session.commit()
    assert response.status == 202
    assert response.content_type == "application/json"
    assert json.loads(response.body) == {"accepted": 7, "ignored": 1, "events": 7}
    assert sum(isinstance(e, MeasurementEvent) for e in events) == 5
    assert sum(isinstance(e, SleepEvent) for e in events) == 1
    assert sum(isinstance(e, WorkoutEvent) for e in events) == 1
    weight = next(e for e in events if isinstance(e, MeasurementEvent) and e.type == "weight")
    assert weight.value == 82.4 and weight.unit == "kg" and weight.source == "apple_health"
    rows = (await session.scalars(select(Measurement).where(Measurement.user_id == user.id))).all()
    assert len(rows) == 5
    assert {r.source for r in rows} == {"apple_health"}
    workouts = (await session.scalars(select(Workout))).all()
    assert len(workouts) == 1 and workouts[0].source.value == "apple_health"
    sleeps = (await session.scalars(select(Sleep))).all()
    assert len(sleeps) == 1 and sleeps[0].asleep_min == 420.0

    # the same push again (HAE re-sends windows): rows updated in place, no events
    response, events = await integration.handle_webhook(session, make_request(token, HAE_PAYLOAD))
    await session.commit()
    assert response.status == 202 and events == []
    assert json.loads(response.body)["events"] == 0
    assert len((await session.scalars(select(Measurement))).all()) == 5
    assert len((await session.scalars(select(Workout))).all()) == 1
    row = await repo.get_integration(session, user.id, "apple_health")
    assert row is not None and row.last_sync_at is not None


async def test_webhook_simple_and_native_dialects(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    integration = AppleHealthIntegration(make_settings(), clock=clock)
    token = (await integration.connect(session, user)).extra["token"]
    await session.commit()
    response, events = await integration.handle_webhook(
        session, make_request(token, SIMPLE_PAYLOAD)
    )
    assert response.status == 202
    assert len(events) == 5
    response, events = await integration.handle_webhook(
        session, make_request(token, NATIVE_PAYLOAD)
    )
    assert response.status == 202
    assert len(events) == 7
    await session.commit()
    rows = (await session.scalars(select(Measurement).where(Measurement.user_id == user.id))).all()
    assert len(rows) == 9
    assert apple_health.MAX_BODY_BYTES == 5 * 1024 * 1024
    huge = make_request(token, b"{" + b" " * (apple_health.MAX_BODY_BYTES + 1) + b"}")
    assert (await integration.handle_webhook(session, huge))[0].status == 413
