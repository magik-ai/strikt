"""Apple Health bridge: a per-user webhook that accepts Health Auto Export and Shortcuts JSON.

Endpoint ``POST /webhooks/apple-health/<webhook_token>`` (research/05 §3). The path token is the
credential (32 random url-safe bytes from ``repo.generate_webhook_token``); an optional
``X-Strikt-Secret`` header, when present, must match it too (constant-time compare). Three
dialects are accepted on the same URL, detected by shape:

1. **Health Auto Export** — ``{"data": {"metrics": [...], "workouts": [...]}}`` with metric names
   like ``weight_&_body_mass``, ``body_fat_percentage``, ``step_count``, ``resting_heart_rate``,
   ``heart_rate_variability``, ``sleep_analysis``; dates ``yyyy-MM-dd HH:mm:ss Z`` or ISO-8601.
2. **Shortcuts (simple)** — ``{"weight_kg": 82.4, "steps": 8500, "resting_hr": 54, "hrv_ms": 48,
   "sleep": {"start": ..., "end": ..., "asleep_min": ...}, "date": "2026-09-03"}``.
3. **Native samples** — ``{"samples": [{"type": "weight", "value": 82.4, "unit": "kg",
   "start": ..., "end": ...}, ...]}`` for any client that can build a list.

Everything is idempotent: measurements dedupe on (source, type, instant, metric), sleeps and
workouts on their external id (or a hash of start/end when the client sends none).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, SystemClock, ensure_utc, zone
from strikt.db import repo
from strikt.db.models import DataSource, IntegrationStatus, MeasurementType, User
from strikt.events import Event, MeasurementEvent, SleepEvent, WorkoutEvent
from strikt.integrations import store
from strikt.integrations.base import (
    ConnectInfo,
    ProviderName,
    WebhookRequest,
    WebhookResponse,
)
from strikt.integrations.oauth import webhook_url
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.config import Settings

log = structlog.get_logger(__name__)

PROVIDER: ProviderName = "apple_health"
SECRET_HEADER = "X-Strikt-Secret"
MAX_BODY_BYTES = 5 * 1024 * 1024
LB_TO_KG = 0.45359237
IN_TO_CM = 2.54
MI_TO_M = 1609.344
KJ_PER_KCAL = 4.184

# Health Auto Export metric name → (our type, canonical unit, event metric)
HAE_METRICS: dict[str, tuple[MeasurementType, str, str]] = {
    "weight_&_body_mass": (MeasurementType.weight, "kg", "weight"),
    "weight_body_mass": (MeasurementType.weight, "kg", "weight"),
    "body_mass": (MeasurementType.weight, "kg", "weight"),
    "weight": (MeasurementType.weight, "kg", "weight"),
    "body_fat_percentage": (MeasurementType.bodyfat, "%", "bodyfat"),
    "lean_body_mass": (MeasurementType.other, "kg", "lean_mass_kg"),
    "waist_circumference": (MeasurementType.waist, "cm", "waist"),
    "step_count": (MeasurementType.steps, "count", "steps"),
    "resting_heart_rate": (MeasurementType.rhr, "bpm", "rhr"),
    "heart_rate_variability": (MeasurementType.hrv, "ms", "hrv"),
    "blood_pressure_systolic": (MeasurementType.bp_sys, "mmHg", "bp_sys"),
    "blood_pressure_diastolic": (MeasurementType.bp_dia, "mmHg", "bp_dia"),
}
# Native / Shortcuts sample type → same triple
SAMPLE_TYPES: dict[str, tuple[MeasurementType, str, str]] = {
    "weight": (MeasurementType.weight, "kg", "weight"),
    "weight_kg": (MeasurementType.weight, "kg", "weight"),
    "body_fat": (MeasurementType.bodyfat, "%", "bodyfat"),
    "bodyfat": (MeasurementType.bodyfat, "%", "bodyfat"),
    "bodyfat_pct": (MeasurementType.bodyfat, "%", "bodyfat"),
    "body_fat_pct": (MeasurementType.bodyfat, "%", "bodyfat"),
    "waist": (MeasurementType.waist, "cm", "waist"),
    "waist_cm": (MeasurementType.waist, "cm", "waist"),
    "steps": (MeasurementType.steps, "count", "steps"),
    "resting_hr": (MeasurementType.rhr, "bpm", "rhr"),
    "rhr": (MeasurementType.rhr, "bpm", "rhr"),
    "hrv": (MeasurementType.hrv, "ms", "hrv"),
    "hrv_ms": (MeasurementType.hrv, "ms", "hrv"),
    "hrv_sdnn": (MeasurementType.hrv, "ms", "hrv"),
    "hrv_rmssd": (MeasurementType.hrv, "ms", "hrv"),
    "lean_mass_kg": (MeasurementType.other, "kg", "lean_mass_kg"),
    "bp_sys": (MeasurementType.bp_sys, "mmHg", "bp_sys"),
    "bp_dia": (MeasurementType.bp_dia, "mmHg", "bp_dia"),
}
SIMPLE_FIELDS: tuple[str, ...] = (
    "weight_kg",
    "weight",
    "bodyfat_pct",
    "body_fat_pct",
    "waist_cm",
    "steps",
    "resting_hr",
    "hrv_ms",
    "lean_mass_kg",
    "bp_sys",
    "bp_dia",
)

_INSTRUCTIONS: dict[str, str] = {
    "en": (
        "Apple Health → Strikt. Anything that syncs to Apple Health (Renpho, Eufy, Xiaomi, "
        "Apple Watch, Garmin, Oura) lands here.\n"
        "\n"
        "Your personal URL (keep it private, it is your key):\n"
        "{url}\n"
        "\n"
        "Option A — Health Auto Export app (recommended, 2 minutes):\n"
        "1. Install “Health Auto Export – JSON+CSV” from the App Store.\n"
        "2. Automations → + → REST API. URL: the link above. Method POST, format JSON, "
        "export version 2, batch requests on.\n"
        "3. Data types: Health Metrics (weight, body fat, steps, resting HR, HRV, sleep) "
        "and Workouts. Period: since last sync. Enable the automation.\n"
        "4. Optional header: {header}: {token}\n"
        "5. iPhone must be unlocked for it to run — add a Shortcuts “Time of Day” automation "
        "at 08:00 that runs Health Auto Export’s “Run Automation” action.\n"
        "\n"
        "Option B — Shortcuts only (no extra app):\n"
        "1. Shortcuts → + → add “Find Health Samples” (Weight, last 1 day, sort by Start Date, limit 1).\n"
        "2. Add “Dictionary”: weight_kg = the sample’s Value, date = the sample’s Start Date. "
        "Add steps / resting_hr / hrv_ms the same way if you want them.\n"
        "3. Add “Get Contents of URL”: URL above, Method POST, Request Body JSON = the Dictionary.\n"
        "4. Automation → Time of Day → 08:00 daily → run the shortcut, “Ask Before Running” off.\n"
        "\n"
        "Send a first push and I will confirm what arrived."
    ),
    "ru": (
        "Apple Health → Strikt. Всё, что синкается в Apple Health (Renpho, Eufy, Xiaomi, "
        "Apple Watch, Garmin, Oura), попадает сюда.\n"
        "\n"
        "Твоя личная ссылка (не показывай никому, это ключ):\n"
        "{url}\n"
        "\n"
        "Вариант A — приложение Health Auto Export (рекомендую, 2 минуты):\n"
        "1. Поставь «Health Auto Export – JSON+CSV» из App Store.\n"
        "2. Automations → + → REST API. URL: ссылка выше. Метод POST, формат JSON, "
        "export version 2, batch requests включить.\n"
        "3. Data types: Health Metrics (вес, жир, шаги, пульс покоя, HRV, сон) и Workouts. "
        "Период: since last sync. Включи автоматизацию.\n"
        "4. Необязательный заголовок: {header}: {token}\n"
        "5. iPhone должен быть разблокирован — добавь в «Команды» автоматизацию «Время суток» "
        "на 08:00, которая запускает действие Health Auto Export «Run Automation».\n"
        "\n"
        "Вариант B — только «Команды» (без приложений):\n"
        "1. Команды → + → «Найти образцы Здоровья» (Вес, за последний 1 день, сортировка по дате начала, лимит 1).\n"
        "2. Добавь «Словарь»: weight_kg = Значение образца, date = Дата начала образца. "
        "Так же можно steps / resting_hr / hrv_ms.\n"
        "3. Добавь «Получить содержимое URL»: URL выше, метод POST, тело запроса JSON = Словарь.\n"
        "4. Автоматизация → Время суток → 08:00 ежедневно → запустить команду, «Спрашивать» выключить.\n"
        "\n"
        "Отправь первый пуш — подтвержу, что пришло."
    ),
}

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "not_oauth": "Apple Health uses a webhook, not a login. Ask me for the link in Telegram."
    },
    "ru": {
        "not_oauth": "Apple Health работает через вебхук, без входа. Попроси ссылку в Telegram."
    },
}


def instructions_text(lang: str | None, *, url: str, token: str) -> str:
    return _INSTRUCTIONS[resolve_lang(lang)].format(url=url, header=SECRET_HEADER, token=token)


# ---------------------------------------------------------------------------------- parsing


def parse_when(value: Any, tz: str) -> datetime | None:
    """HAE ``2026-09-03 07:12:00 +0300``, ISO-8601, date-only (local midnight) or unix seconds."""
    if value is None:
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=zone("UTC"))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone(tz))
    return ensure_utc(parsed)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalise(value: float, unit: str | None, canonical: str) -> float:
    """Convert a sample into the canonical unit (kg, cm, m, ms, s ...)."""
    u = (unit or canonical).strip().lower()
    if canonical == "kg" and u in {"lb", "lbs"}:
        return round(value * LB_TO_KG, 3)
    if canonical == "cm" and u in {"in", "inch", "inches"}:
        return round(value * IN_TO_CM, 2)
    if canonical == "cm" and u == "m":
        return round(value * 100, 2)
    if canonical == "ms" and u == "s":
        return round(value * 1000, 3)
    if canonical == "m" and u == "km":
        return round(value * 1000, 1)
    if canonical == "m" and u == "mi":
        return round(value * MI_TO_M, 1)
    if canonical == "s" and u in {"min", "minutes"}:
        return round(value * 60, 1)
    if canonical == "s" and u in {"hr", "h", "hours"}:
        return round(value * 3600, 1)
    if canonical == "kcal" and u == "kj":
        return round(value / KJ_PER_KCAL, 1)
    return value


@dataclass(frozen=True, kw_only=True)
class MeasurementIn:
    type: MeasurementType
    metric: str
    value: float
    unit: str
    measured_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SleepIn:
    external_id: str
    started_at: datetime
    ended_at: datetime
    in_bed_min: float | None = None
    asleep_min: float | None = None
    stages_min: dict[str, float] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class WorkoutIn:
    external_id: str
    sport: str
    started_at: datetime
    ended_at: datetime
    duration_min: float
    kcal: float | None = None
    distance_m: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Parsed:
    measurements: list[MeasurementIn] = field(default_factory=list)
    sleeps: list[SleepIn] = field(default_factory=list)
    workouts: list[WorkoutIn] = field(default_factory=list)
    ignored: int = 0
    dialect: str = "unknown"

    @property
    def accepted(self) -> int:
        return len(self.measurements) + len(self.sleeps) + len(self.workouts)


def _synthetic_id(*parts: Any) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"h:{digest[:32]}"


def _stages(source: Mapping[str, Any], *, factor: float) -> dict[str, float] | None:
    out: dict[str, float] = {}
    for key in ("core", "deep", "rem", "awake", "light"):
        value = _num(source.get(key))
        if value is not None:
            out["light" if key == "core" else key] = round(value * factor, 1)
    return out or None


def parse_payload(payload: Any, *, tz: str) -> Parsed:
    """Detect the dialect and normalise everything into our units."""
    parsed = Parsed()
    if not isinstance(payload, dict):
        parsed.ignored = 1
        return parsed
    data = payload.get("data")
    if isinstance(data, dict) and ("metrics" in data or "workouts" in data):
        parsed.dialect = "hae"
        _parse_hae(data, tz, parsed)
    elif isinstance(payload.get("samples"), list):
        parsed.dialect = "samples"
        _parse_samples(payload["samples"], tz, parsed)
    else:
        parsed.dialect = "simple"
        _parse_simple(payload, tz, parsed)
    return parsed


def _parse_hae(data: Mapping[str, Any], tz: str, out: Parsed) -> None:
    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict):
            out.ignored += 1
            continue
        name = str(metric.get("name") or "").strip().lower()
        units = str(metric.get("units") or "")
        points = metric.get("data") or []
        if name == "sleep_analysis":
            for point in points:
                if isinstance(point, dict):
                    sleep = _hae_sleep(point, tz, units)
                    if sleep is not None:
                        out.sleeps.append(sleep)
                        continue
                out.ignored += 1
            continue
        spec = HAE_METRICS.get(name)
        if spec is None:
            out.ignored += len(points) if isinstance(points, list) else 1
            continue
        db_type, canonical, metric_name = spec
        for point in points:
            if not isinstance(point, dict):
                out.ignored += 1
                continue
            when = parse_when(point.get("date"), tz)
            qty = _num(point.get("qty"))
            if qty is None:
                qty = _num(point.get("Avg"))
            if when is None or qty is None:
                out.ignored += 1
                continue
            out.measurements.append(
                MeasurementIn(
                    type=db_type,
                    metric=metric_name,
                    value=normalise(qty, units, canonical),
                    unit=canonical,
                    measured_at=when,
                    raw={"name": name, "units": units, "source": point.get("source"), "qty": qty},
                )
            )
    for workout in data.get("workouts") or []:
        if not isinstance(workout, dict):
            out.ignored += 1
            continue
        mapped = _hae_workout(workout, tz)
        if mapped is None:
            out.ignored += 1
        else:
            out.workouts.append(mapped)


def _hae_sleep(point: Mapping[str, Any], tz: str, units: str) -> SleepIn | None:
    start = parse_when(point.get("sleepStart") or point.get("startDate"), tz)
    end = parse_when(point.get("sleepEnd") or point.get("endDate"), tz)
    if start is None or end is None or end <= start:
        return None
    factor = 60.0 if units.lower() in {"hr", "h", "hours", ""} else 1.0
    asleep = _num(point.get("asleep"))
    if asleep is None:
        asleep = _num(point.get("totalSleep"))
    if asleep is None and point.get("value") in {"Asleep", "Core", "REM", "Deep"}:
        asleep = _num(point.get("qty"))
    in_bed = _num(point.get("inBed"))
    return SleepIn(
        external_id=_synthetic_id("hae", start.isoformat(), end.isoformat()),
        started_at=start,
        ended_at=end,
        in_bed_min=None if in_bed is None else round(in_bed * factor, 1),
        asleep_min=None if asleep is None else round(asleep * factor, 1),
        stages_min=_stages(point, factor=factor),
        raw={"dialect": "hae", "source": point.get("source"), "units": units},
    )


def _hae_workout(workout: Mapping[str, Any], tz: str) -> WorkoutIn | None:
    start = parse_when(workout.get("start"), tz)
    end = parse_when(workout.get("end"), tz)
    if start is None or end is None or end <= start:
        return None
    name = str(workout.get("name") or "workout").strip().lower()
    duration = _num(workout.get("duration"))
    duration_min = (
        round(duration / 60, 1) if duration else round((end - start).total_seconds() / 60, 1)
    )
    energy = workout.get("activeEnergyBurned") or workout.get("totalEnergy")
    kcal = None
    if isinstance(energy, dict):
        qty = _num(energy.get("qty"))
        if qty is not None:
            kcal = normalise(qty, str(energy.get("units") or "kcal"), "kcal")
    distance = workout.get("distance")
    distance_m = None
    if isinstance(distance, dict):
        qty = _num(distance.get("qty"))
        if qty is not None:
            distance_m = normalise(qty, str(distance.get("units") or "m"), "m")
    avg_hr = max_hr = None
    hr = workout.get("avgHeartRate") or workout.get("heartRateAverage")
    if isinstance(hr, dict):
        hr = hr.get("qty")
    mx = workout.get("maxHeartRate") or workout.get("heartRateMaximum")
    if isinstance(mx, dict):
        mx = mx.get("qty")
    hr_value = _num(hr)
    mx_value = _num(mx)
    if hr_value is not None:
        avg_hr = round(hr_value)
    if mx_value is not None:
        max_hr = round(mx_value)
    external = str(workout.get("id") or "") or _synthetic_id("hae", name, start.isoformat())
    return WorkoutIn(
        external_id=external,
        sport=name,
        started_at=start,
        ended_at=end,
        duration_min=duration_min,
        kcal=kcal,
        distance_m=distance_m,
        avg_hr=avg_hr,
        max_hr=max_hr,
        raw={"dialect": "hae", "location": workout.get("location")},
    )


def _parse_samples(samples: list[Any], tz: str, out: Parsed) -> None:
    for sample in samples:
        if not isinstance(sample, dict):
            out.ignored += 1
            continue
        kind = str(sample.get("type") or "").strip().lower()
        start = parse_when(sample.get("start") or sample.get("date"), tz)
        end = parse_when(sample.get("end"), tz) or start
        value = _num(sample.get("value"))
        unit = str(sample.get("unit") or "")
        if kind == "sleep":
            if start is None or end is None or end <= start:
                out.ignored += 1
                continue
            asleep_s = normalise(value, unit or "s", "s") if value is not None else None
            stages = sample.get("stages") if isinstance(sample.get("stages"), dict) else None
            out.sleeps.append(
                SleepIn(
                    external_id=str(sample.get("sample_id") or "")
                    or _synthetic_id("sample", "sleep", start.isoformat(), end.isoformat()),
                    started_at=start,
                    ended_at=end,
                    asleep_min=None if asleep_s is None else round(asleep_s / 60, 1),
                    stages_min=_stages(stages, factor=1 / 60) if stages else None,
                    raw={"dialect": "samples", "source": sample.get("source")},
                )
            )
            continue
        if kind == "workout":
            if start is None or end is None or end <= start:
                out.ignored += 1
                continue
            energy = _num(sample.get("energy_kcal"))
            distance = _num(sample.get("distance_m"))
            out.workouts.append(
                WorkoutIn(
                    external_id=str(sample.get("sample_id") or "")
                    or _synthetic_id("sample", "workout", start.isoformat(), end.isoformat()),
                    sport=str(sample.get("activity") or "workout").strip().lower(),
                    started_at=start,
                    ended_at=end,
                    duration_min=round((end - start).total_seconds() / 60, 1),
                    kcal=energy,
                    distance_m=distance,
                    raw={"dialect": "samples", "source": sample.get("source")},
                )
            )
            continue
        spec = SAMPLE_TYPES.get(kind)
        if spec is None or start is None or value is None:
            out.ignored += 1
            continue
        db_type, canonical, metric = spec
        out.measurements.append(
            MeasurementIn(
                type=db_type,
                metric=metric,
                value=normalise(value, unit, canonical),
                unit=canonical,
                measured_at=start,
                raw={
                    "dialect": "samples",
                    "type": kind,
                    "unit": unit,
                    "source": sample.get("source"),
                },
            )
        )


def _parse_simple(payload: Mapping[str, Any], tz: str, out: Parsed) -> None:
    when = parse_when(payload.get("date") or payload.get("measured_at"), tz)
    for key in SIMPLE_FIELDS:
        if key not in payload:
            continue
        value = _num(payload.get(key))
        if value is None or when is None:
            out.ignored += 1
            continue
        db_type, canonical, metric = SAMPLE_TYPES[key]
        out.measurements.append(
            MeasurementIn(
                type=db_type,
                metric=metric,
                value=normalise(value, canonical, canonical),
                unit=canonical,
                measured_at=when,
                raw={"dialect": "simple", "field": key},
            )
        )
    sleep = payload.get("sleep")
    if isinstance(sleep, dict):
        start = parse_when(sleep.get("start") or sleep.get("bed_time"), tz)
        end = parse_when(sleep.get("end") or sleep.get("wake_time"), tz)
        asleep_min = _num(sleep.get("asleep_min"))
        if asleep_min is None and _num(sleep.get("asleep_hours")) is not None:
            asleep_min = round(float(sleep["asleep_hours"]) * 60, 1)
        if asleep_min is None and _num(sleep.get("hours")) is not None:
            asleep_min = round(float(sleep["hours"]) * 60, 1)
        if end is None and start is not None and asleep_min is not None:
            end = start + timedelta(minutes=asleep_min)
        if start is None and end is not None and asleep_min is not None:
            start = end - timedelta(minutes=asleep_min)
        if start is not None and end is not None and end > start:
            in_bed = _num(sleep.get("in_bed_min"))
            out.sleeps.append(
                SleepIn(
                    external_id=_synthetic_id("simple", start.isoformat(), end.isoformat()),
                    started_at=start,
                    ended_at=end,
                    in_bed_min=in_bed,
                    asleep_min=asleep_min,
                    stages_min=_stages(sleep, factor=1.0),
                    raw={"dialect": "simple"},
                )
            )
        else:
            out.ignored += 1
    workout = payload.get("workout")
    if isinstance(workout, dict):
        start = parse_when(workout.get("start"), tz)
        end = parse_when(workout.get("end"), tz)
        if start is not None and end is not None and end > start:
            out.workouts.append(
                WorkoutIn(
                    external_id=str(workout.get("id") or "")
                    or _synthetic_id("simple", "workout", start.isoformat(), end.isoformat()),
                    sport=str(workout.get("activity") or workout.get("name") or "workout").lower(),
                    started_at=start,
                    ended_at=end,
                    duration_min=round((end - start).total_seconds() / 60, 1),
                    kcal=_num(workout.get("kcal") or workout.get("energy_kcal")),
                    distance_m=_num(workout.get("distance_m")),
                    raw={"dialect": "simple"},
                )
            )
        else:
            out.ignored += 1


# ------------------------------------------------------------------------------ integration


class AppleHealthIntegration:
    provider: ProviderName = PROVIDER

    def __init__(self, settings: Settings, *, clock: Clock | None = None) -> None:
        self._settings = settings
        self._clock: Clock = clock or SystemClock()

    async def connect(self, session: AsyncSession, user: User) -> ConnectInfo:
        now = self._clock.now()
        row = await repo.get_integration(session, user.id, PROVIDER)
        token = row.webhook_token if row is not None else None
        if not token:
            token = repo.generate_webhook_token()
        await repo.upsert_integration(
            session,
            user.id,
            PROVIDER,
            now=now,
            webhook_token=token,
            status=IntegrationStatus.connected,
        )
        url = webhook_url(self._settings, PROVIDER, token)
        return ConnectInfo(
            provider=PROVIDER,
            kind="webhook",
            url=url,
            instructions=instructions_text(user.language, url=url, token=token),
            extra={"token": token, "secret_header": SECRET_HEADER},
        )

    async def handle_callback(
        self, session: AsyncSession, query: dict[str, str]
    ) -> tuple[User | None, str]:
        return None, _COPY["en"]["not_oauth"]

    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        return []  # push-only: the phone sends, we never pull

    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        if request.method.upper() == "HEAD":
            return WebhookResponse(), []
        token = request.path_token
        if not token:
            return WebhookResponse(status=404, body="unknown token"), []
        row = await repo.integration_by_webhook_token(session, token)
        if row is None or row.provider.value != PROVIDER:
            log.info("apple_health_unknown_token")
            return WebhookResponse(status=404, body="unknown token"), []
        header_secret = _header(request.headers, SECRET_HEADER)
        if header_secret is not None and not hmac.compare_digest(
            header_secret.strip().encode("utf-8"), token.encode("utf-8")
        ):
            log.warning("apple_health_bad_secret", user_id=row.user_id)
            return WebhookResponse(status=401, body="bad secret"), []
        if len(request.body) > MAX_BODY_BYTES:
            return WebhookResponse(status=413, body="payload too large"), []
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return WebhookResponse(status=400, body="invalid json"), []
        user = await repo.get_user(session, row.user_id)
        if user is None:
            return WebhookResponse(status=404, body="unknown token"), []
        parsed = parse_payload(payload, tz=user.timezone)
        events = await self._store(session, user, parsed)
        await repo.set_integration_status(
            session, user.id, PROVIDER, IntegrationStatus.connected, last_sync_at=self._clock.now()
        )
        log.info(
            "apple_health_webhook",
            user_id=user.id,
            dialect=parsed.dialect,
            accepted=parsed.accepted,
            ignored=parsed.ignored,
            events=len(events),
        )
        body = json.dumps(
            {"accepted": parsed.accepted, "ignored": parsed.ignored, "events": len(events)}
        )
        return WebhookResponse(status=202, body=body, content_type="application/json"), events

    async def _store(self, session: AsyncSession, user: User, parsed: Parsed) -> list[Event]:
        now = self._clock.now()
        events: list[Event] = []
        for m in parsed.measurements:
            note = m.metric if m.type == MeasurementType.other else None
            _row, created = await store.upsert_measurement(
                session,
                user.id,
                type=m.type,
                value=m.value,
                unit=m.unit,
                measured_at=m.measured_at,
                source=PROVIDER,
                raw=m.raw,
                note=note,
            )
            if created:
                events.append(
                    MeasurementEvent(
                        user_id=user.id,
                        occurred_at=m.measured_at,
                        source=PROVIDER,
                        type=m.metric,
                        value=m.value,
                        unit=m.unit,
                        measured_at=m.measured_at,
                        raw=m.raw,
                    )
                )
        for s in parsed.sleeps:
            _srow, created = await repo.upsert_sleep_by_external(
                session,
                user.id,
                source=DataSource.apple_health,
                external_id=s.external_id,
                started_at=s.started_at,
                ended_at=s.ended_at,
                now=now,
                in_bed_min=s.in_bed_min,
                asleep_min=s.asleep_min,
                stages_min=s.stages_min,
                raw=s.raw,
            )
            if created:
                events.append(
                    SleepEvent(
                        user_id=user.id,
                        occurred_at=s.ended_at,
                        source=PROVIDER,
                        external_id=s.external_id,
                        started_at=s.started_at,
                        ended_at=s.ended_at,
                        in_bed_min=s.in_bed_min,
                        asleep_min=s.asleep_min,
                        stages_min=s.stages_min,
                        raw=s.raw,
                    )
                )
        for w in parsed.workouts:
            _wrow, created = await repo.upsert_workout_by_external(
                session,
                user.id,
                source=DataSource.apple_health,
                external_id=w.external_id,
                sport=w.sport,
                started_at=w.started_at,
                now=now,
                ended_at=w.ended_at,
                duration_min=w.duration_min,
                kcal=w.kcal,
                avg_hr=w.avg_hr,
                max_hr=w.max_hr,
                distance_m=w.distance_m,
                raw=w.raw,
            )
            if created:
                events.append(
                    WorkoutEvent(
                        user_id=user.id,
                        occurred_at=w.ended_at,
                        source=PROVIDER,
                        external_id=w.external_id,
                        sport=w.sport,
                        started_at=w.started_at,
                        ended_at=w.ended_at,
                        duration_min=w.duration_min,
                        kcal=w.kcal,
                        avg_hr=w.avg_hr,
                        max_hr=w.max_hr,
                        distance_m=w.distance_m,
                        raw=w.raw,
                    )
                )
        return events


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None
