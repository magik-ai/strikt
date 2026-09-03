"""Shared fakes for the integration tests: an httpx MockTransport router and fixtures data.

Imported by ``tests/test_integrations_*.py`` and ``tests/test_web_*.py``; holds no tests of its own
(one smoke test keeps pytest from complaining about an empty module).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from strikt.config import Settings
from strikt.db.crypto import generate_key

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
BASE_URL = "https://coach.example.com"

Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """Route ``(method, path-prefix)`` → handler; records every request for assertions."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, path: str, handler: Handler) -> None:
        self.routes.append((method.upper(), path, handler))

    def json(self, method: str, path: str, payload: Any, *, status: int = 200) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        self.add(method, path, handler)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        target = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        for method, path, handler in self.routes:
            if method == request.method.upper() and target.startswith(path):
                return handler(request)
        return httpx.Response(404, json={"error": f"no route for {request.method} {target}"})

    def client_factory(self) -> Callable[[], httpx.AsyncClient]:
        transport = httpx.MockTransport(self.handle)
        return lambda: httpx.AsyncClient(transport=transport)

    def requests(self, method: str, path: str) -> list[httpx.Request]:
        return [
            r
            for r in self.calls
            if r.method.upper() == method.upper()
            and f"{r.url.scheme}://{r.url.host}{r.url.path}".startswith(path)
        ]


def form(request: httpx.Request) -> dict[str, str]:
    """Decode an ``application/x-www-form-urlencoded`` request body."""
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}


def body_json(request: httpx.Request) -> Any:
    return json.loads(request.content.decode("utf-8"))


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "token_encryption_key": generate_key(),
        "public_base_url": BASE_URL,
        "whoop_client_id": "whoop-client",
        "whoop_client_secret": "whoop-secret",
        "withings_client_id": "withings-client",
        "withings_client_secret": "withings-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------- WHOOP fixtures
# From research/04-whoop.md §2.7 (docs example) plus schema-faithful sleep/recovery/cycle rows.

WHOOP_WORKOUT: dict[str, Any] = {
    "id": "ecfc6a15-4661-442f-a9a4-f160dd7afae8",
    "v1_id": 1043,
    "user_id": 9012,
    "created_at": "2026-09-02T11:25:44.774Z",
    "updated_at": "2026-09-02T14:25:44.774Z",
    "start": "2026-09-02T02:25:44.774Z",
    "end": "2026-09-02T03:25:44.774Z",
    "timezone_offset": "+04:00",
    "sport_name": "running",
    "score_state": "SCORED",
    "score": {
        "strain": 8.2463,
        "average_heart_rate": 123,
        "max_heart_rate": 146,
        "kilojoule": 1569.34033203125,
        "percent_recorded": 100,
        "distance_meter": 1772.77035916,
        "altitude_gain_meter": 46.64384460449,
        "altitude_change_meter": -0.781372010707855,
        "zone_durations": {
            "zone_zero_milli": 300000,
            "zone_one_milli": 600000,
            "zone_two_milli": 900000,
            "zone_three_milli": 900000,
            "zone_four_milli": 600000,
            "zone_five_milli": 300000,
        },
    },
    "sport_id": 0,
}

WHOOP_SLEEP: dict[str, Any] = {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "cycle_id": 93845,
    "user_id": 9012,
    "created_at": "2026-09-03T04:10:00.000Z",
    "updated_at": "2026-09-03T04:12:00.000Z",
    "start": "2026-09-02T20:05:00.000Z",
    "end": "2026-09-03T03:55:00.000Z",
    "timezone_offset": "+04:00",
    "nap": False,
    "score_state": "SCORED",
    "score": {
        "stage_summary": {
            "total_in_bed_time_milli": 28200000,
            "total_awake_time_milli": 1800000,
            "total_no_data_time_milli": 0,
            "total_light_sleep_time_milli": 12600000,
            "total_slow_wave_sleep_time_milli": 5400000,
            "total_rem_sleep_time_milli": 7200000,
            "sleep_cycle_count": 4,
            "disturbance_count": 7,
        },
        "sleep_needed": {
            "baseline_milli": 27000000,
            "need_from_sleep_debt_milli": 1200000,
            "need_from_recent_strain_milli": 600000,
            "need_from_recent_nap_milli": 0,
        },
        "respiratory_rate": 16.11328125,
        "sleep_performance_percentage": 87.0,
        "sleep_consistency_percentage": 71.0,
        "sleep_efficiency_percentage": 93.6,
    },
}

WHOOP_RECOVERY: dict[str, Any] = {
    "cycle_id": 93845,
    "sleep_id": "550e8400-e29b-41d4-a716-446655440000",
    "user_id": 9012,
    "created_at": "2026-09-03T04:12:00.000Z",
    "updated_at": "2026-09-03T04:12:00.000Z",
    "score_state": "SCORED",
    "score": {
        "user_calibrating": False,
        "recovery_score": 44.0,
        "resting_heart_rate": 64.0,
        "hrv_rmssd_milli": 31.813562,
        "spo2_percentage": 95.6875,
        "skin_temp_celsius": 33.7,
    },
}

WHOOP_CYCLE: dict[str, Any] = {
    "id": 93845,
    "user_id": 9012,
    "created_at": "2026-09-02T20:05:00.000Z",
    "updated_at": "2026-09-03T04:12:00.000Z",
    "start": "2026-09-02T20:05:00.000Z",
    "timezone_offset": "+04:00",
    "score_state": "SCORED",
    "score": {
        "strain": 5.2951527,
        "kilojoule": 8288.297,
        "average_heart_rate": 68,
        "max_heart_rate": 141,
    },
}


# -------------------------------------------------------------------------- Withings fixtures
# research/05 §1.4: value * 10^unit, grpid+type idempotency, attrib 1 = ambiguous user.

WITHINGS_GETMEAS: dict[str, Any] = {
    "status": 0,
    "body": {
        "updatetime": 1756884000,
        "timezone": "Asia/Dubai",
        "measuregrps": [
            {
                "grpid": 12345,
                "attrib": 0,
                "date": 1788405120,  # 2026-09-03 07:12:00 +04:00
                "created": 1756881130,
                "modified": 1756881130,
                "category": 1,
                "deviceid": "abc",
                "hash_deviceid": "abc",
                "model": "Body+",
                "measures": [
                    {"value": 65750, "type": 1, "unit": -3},
                    {"value": 213, "type": 6, "unit": -1},
                    {"value": 51750, "type": 5, "unit": -3},
                    {"value": 14, "type": 8, "unit": 0},
                    {"value": 12, "type": 12, "unit": 0},  # temperature: not requested, ignored
                ],
            },
            {
                "grpid": 12346,
                "attrib": 1,  # ambiguous user: skipped
                "date": 1756794720,
                "category": 1,
                "model": "Body+",
                "measures": [{"value": 90000, "type": 1, "unit": -3}],
            },
        ],
        "more": 0,
        "offset": 0,
    },
}


# ---------------------------------------------------------------------- Apple Health fixtures
# research/05 §3.4, verbatim dialect 1 and dialect 2.

HAE_PAYLOAD: dict[str, Any] = {
    "data": {
        "metrics": [
            {
                "name": "weight_&_body_mass",
                "units": "kg",
                "data": [
                    {"qty": 82.4, "date": "2026-09-03 07:12:00 +0300", "source": "Renpho Health"}
                ],
            },
            {
                "name": "body_fat_percentage",
                "units": "%",
                "data": [
                    {"qty": 21.3, "date": "2026-09-03 07:12:00 +0300", "source": "Renpho Health"}
                ],
            },
            {
                "name": "step_count",
                "units": "count",
                "data": [{"qty": 8500, "date": "2026-09-02 00:00:00 +0300", "source": "iPhone"}],
            },
            {
                "name": "heart_rate",
                "units": "bpm",
                "data": [{"date": "2026-09-02 14:00:00 +0300", "Min": 65, "Avg": 72, "Max": 85}],
            },
            {
                "name": "resting_heart_rate",
                "units": "bpm",
                "data": [{"qty": 54, "date": "2026-09-02 00:00:00 +0300", "source": "Apple Watch"}],
            },
            {
                "name": "heart_rate_variability",
                "units": "ms",
                "data": [{"qty": 48, "date": "2026-09-02 06:30:00 +0300", "source": "Apple Watch"}],
            },
            {
                "name": "sleep_analysis",
                "units": "hr",
                "data": [
                    {
                        "date": "2026-09-02",
                        "totalSleep": 7.5,
                        "asleep": 7.0,
                        "core": 3.5,
                        "deep": 1.5,
                        "rem": 2.0,
                        "sleepStart": "2026-09-01 23:00:00 +0300",
                        "sleepEnd": "2026-09-02 06:30:00 +0300",
                        "inBed": 8.0,
                        "inBedStart": "2026-09-01 22:45:00 +0300",
                        "inBedEnd": "2026-09-02 06:45:00 +0300",
                    }
                ],
            },
        ],
        "workouts": [
            {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "name": "Running",
                "start": "2026-09-02 07:00:00 +0300",
                "end": "2026-09-02 07:30:00 +0300",
                "duration": 1800,
                "activeEnergyBurned": {"qty": 350, "units": "kcal"},
                "distance": {"qty": 5.1, "units": "km"},
            }
        ],
    }
}

NATIVE_PAYLOAD: dict[str, Any] = {
    "schema": "bomiso.health.v1",
    "platform": "ios",
    "sent_at": "2026-09-03T04:12:30Z",
    "samples": [
        {
            "type": "weight",
            "value": 82.4,
            "unit": "kg",
            "start": "2026-09-03T04:12:00Z",
            "end": "2026-09-03T04:12:00Z",
            "source": "Renpho Health",
            "sample_id": "6F1C2A3B-HK-UUID",
        },
        {
            "type": "body_fat",
            "value": 21.3,
            "unit": "%",
            "start": "2026-09-03T04:12:00Z",
            "end": "2026-09-03T04:12:00Z",
            "source": "Renpho Health",
        },
        {
            "type": "steps",
            "value": 8500,
            "unit": "count",
            "start": "2026-09-02T00:00:00+03:00",
            "end": "2026-09-03T00:00:00+03:00",
        },
        {
            "type": "resting_hr",
            "value": 54,
            "unit": "bpm",
            "start": "2026-09-02T00:00:00+03:00",
            "end": "2026-09-03T00:00:00+03:00",
        },
        {
            "type": "hrv_sdnn",
            "value": 48,
            "unit": "ms",
            "start": "2026-09-02T03:30:00Z",
            "end": "2026-09-02T03:30:00Z",
        },
        {
            "type": "sleep",
            "value": 27000,
            "unit": "s",
            "start": "2026-09-01T20:00:00Z",
            "end": "2026-09-02T03:30:00Z",
            "stages": {"core": 12600, "deep": 5400, "rem": 7200, "awake": 1800},
        },
        {
            "type": "workout",
            "value": 1800,
            "unit": "s",
            "start": "2026-09-02T04:00:00Z",
            "end": "2026-09-02T04:30:00Z",
            "activity": "running",
            "energy_kcal": 350,
            "distance_m": 5100,
        },
    ],
}

SIMPLE_PAYLOAD: dict[str, Any] = {
    "weight_kg": 103.4,
    "steps": 6400,
    "resting_hr": 52,
    "hrv_ms": 61,
    "sleep": {
        "start": "2026-09-02T23:40:00+04:00",
        "end": "2026-09-03T07:10:00+04:00",
        "asleep_min": 420,
    },
    "date": "2026-09-03T07:15:00+04:00",
}


def test_router_records_calls() -> None:
    router = Router()
    router.json("GET", "https://x.test/a", {"ok": True})
    response = router.handle(httpx.Request("GET", "https://x.test/a?q=1"))
    assert response.status_code == 200
    assert router.handle(httpx.Request("POST", "https://x.test/a")).status_code == 404
    assert len(router.requests("GET", "https://x.test/a")) == 1
