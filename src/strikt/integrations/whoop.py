"""WHOOP integration: OAuth2, v2 REST sync with pagination and token rotation, webhooks.

Facts from research/04-whoop.md (verified against the live OpenAPI spec on 2026-09-03):

- Authorize ``https://api.prod.whoop.com/oauth/oauth2/auth``, token
  ``https://api.prod.whoop.com/oauth/oauth2/token`` (form-encoded). ``offline`` is required for a
  refresh token; refreshing rotates *both* tokens, so refreshes are single-flight per user and the
  new pair is stored before it is used.
- Collections: ``/v2/activity/workout``, ``/v2/activity/sleep``, ``/v2/recovery``, ``/v2/cycle``
  with ``limit`` (max 25), ``start`` (inclusive), ``end`` (exclusive), ``nextToken`` in, and
  ``records`` + ``next_token`` out. Newest first.
- Units: kilojoules (``kcal = kJ / 4.184``), ``*_milli`` milliseconds, strain 0–21.
- ``score`` exists only when ``score_state == "SCORED"``; ``PENDING_SCORE`` records are skipped
  (a later ``*.updated`` webhook re-delivers them); ``UNSCORABLE`` ones are stored without a score.
- Webhooks: ``X-WHOOP-Signature = base64(HMAC-SHA256(client_secret, timestamp_ms + raw_body))``
  with ``X-WHOOP-Signature-Timestamp`` in milliseconds; events ``workout|sleep|recovery.updated|
  deleted``; a v2 ``recovery.*`` event carries the *sleep* UUID.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, SystemClock, ensure_utc, to_local
from strikt.db import repo
from strikt.db.models import DataSource, IntegrationStatus, User
from strikt.events import Event, RecoveryEvent, SleepEvent, WorkoutEvent
from strikt.integrations import store
from strikt.integrations.base import (
    ConnectInfo,
    ProviderName,
    WebhookRequest,
    WebhookResponse,
)
from strikt.integrations.oauth import callback_url, consume_state, issue_state
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import Integration

log = structlog.get_logger(__name__)

PROVIDER: ProviderName = "whoop"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"
SCOPES = (
    "offline read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement"
)
PAGE_LIMIT = 25
KJ_PER_KCAL = 4.184
MS_PER_MIN = 60_000.0
WEBHOOK_MAX_SKEW_S = 300
SCORED = "SCORED"
PENDING = "PENDING_SCORE"
_ZONE_KEYS = (
    ("z0", "zone_zero_milli"),
    ("z1", "zone_one_milli"),
    ("z2", "zone_two_milli"),
    ("z3", "zone_three_milli"),
    ("z4", "zone_four_milli"),
    ("z5", "zone_five_milli"),
)

ClientFactory = Callable[[], httpx.AsyncClient]

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "connect": "Open the link, log in to WHOOP and allow access. I will pull the last 7 days.",
        "not_configured": "WHOOP is not configured on this server (WHOOP_CLIENT_ID is missing).",
        "connected": "WHOOP connected. Pulled {workouts} workouts, {sleeps} sleeps, {recoveries} recoveries from the last 7 days. Go back to Telegram.",
        "denied": "WHOOP access was not granted. Send “connect WHOOP” again when you want to retry.",
        "expired": "The WHOOP link expired or was already used. Ask me for a new one in Telegram.",
        "failed": "WHOOP did not accept the login. Ask me for a new link in Telegram.",
    },
    "ru": {
        "connect": "Открой ссылку, войди в WHOOP и разреши доступ. Подтяну последние 7 дней.",
        "not_configured": "WHOOP на этом сервере не настроен (нет WHOOP_CLIENT_ID).",
        "connected": "WHOOP подключён. Забрал за 7 дней: тренировок {workouts}, снов {sleeps}, восстановлений {recoveries}. Возвращайся в Telegram.",
        "denied": "Доступ к WHOOP не выдан. Напиши «подключи WHOOP», когда захочешь повторить.",
        "expired": "Ссылка WHOOP устарела или уже использована. Попроси новую в Telegram.",
        "failed": "WHOOP не принял вход. Попроси новую ссылку в Telegram.",
    },
}


def _copy(lang: str | None, key: str, **kwargs: Any) -> str:
    text = _COPY[resolve_lang(lang)][key]
    return text.format(**kwargs) if kwargs else text


# ----------------------------------------------------------------------------------- errors


class WhoopError(RuntimeError):
    """A WHOOP API call failed (network, 5xx, malformed body)."""


class WhoopAuthError(WhoopError):
    """The token is invalid and could not be refreshed; the user must reconnect."""


# ------------------------------------------------------------------------------- signatures


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def compute_signature(client_secret: str, timestamp: str, body: bytes) -> str:
    """``base64(HMAC-SHA256(client_secret, timestamp + raw_body))`` exactly as WHOOP does."""
    mac = hmac.new(client_secret.encode("utf-8"), timestamp.encode("ascii") + body, hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("ascii")


def verify_signature(
    client_secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    now: datetime,
    max_skew_s: int = WEBHOOK_MAX_SKEW_S,
) -> bool:
    """Constant-time check of ``X-WHOOP-Signature``; rejects timestamps older than 5 minutes."""
    signature = _header(headers, "X-WHOOP-Signature")
    timestamp = _header(headers, "X-WHOOP-Signature-Timestamp")
    if not signature or not timestamp or not client_secret:
        return False
    try:
        ts_ms = int(timestamp)
    except ValueError:
        return False
    now_ms = int(ensure_utc(now).timestamp() * 1000)
    if abs(now_ms - ts_ms) > max_skew_s * 1000:
        return False
    expected = compute_signature(client_secret, timestamp, body)
    return hmac.compare_digest(expected.encode("ascii"), signature.strip().encode("ascii"))


# ---------------------------------------------------------------------------------- mapping


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def kj_to_kcal(kilojoule: Any) -> float | None:
    if kilojoule is None:
        return None
    try:
        return round(float(kilojoule) / KJ_PER_KCAL, 1)
    except (TypeError, ValueError):
        return None


def zones_to_minutes(zone_durations: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not zone_durations:
        return None
    out: dict[str, float] = {}
    for short, key in _ZONE_KEYS:
        raw = zone_durations.get(key)
        if raw is None:
            continue
        try:
            out[short] = round(float(raw) / MS_PER_MIN, 1)
        except (TypeError, ValueError):
            continue
    return out or None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    number = _opt_float(value)
    return None if number is None else round(number)


@dataclass(frozen=True, kw_only=True)
class WorkoutRecord:
    external_id: str
    sport: str
    started_at: datetime
    ended_at: datetime
    duration_min: float
    strain: float | None = None
    kcal: float | None = None
    avg_hr: int | None = None
    max_hr: int | None = None
    zones_min: dict[str, float] | None = None
    distance_m: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SleepRecord:
    external_id: str
    started_at: datetime
    ended_at: datetime
    nap: bool
    cycle_id: int | None
    in_bed_min: float | None = None
    asleep_min: float | None = None
    performance_pct: float | None = None
    stages_min: dict[str, float] | None = None
    respiratory_rate: float | None = None
    disturbances: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RecoveryRecord:
    external_id: str  # cycle id
    sleep_id: str | None
    day: date
    score: float | None = None
    rhr: float | None = None
    hrv_ms: float | None = None
    spo2: float | None = None
    skin_temp_c: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def map_workout(rec: Mapping[str, Any]) -> WorkoutRecord | None:
    """v2 ``WorkoutV2`` → :class:`WorkoutRecord`; None for pending or malformed records."""
    external_id = rec.get("id")
    started = parse_ts(rec.get("start"))
    ended = parse_ts(rec.get("end"))
    state = str(rec.get("score_state") or "")
    if not external_id or started is None or ended is None or state == PENDING:
        return None
    sport = str(rec.get("sport_name") or "activity").strip().lower() or "activity"
    score = rec.get("score") if state == SCORED and isinstance(rec.get("score"), dict) else None
    raw: dict[str, Any] = {
        "score_state": state,
        "timezone_offset": rec.get("timezone_offset"),
        "updated_at": rec.get("updated_at"),
    }
    strain = kcal = distance = None
    avg_hr = max_hr = None
    zones = None
    if score is not None:
        strain = _opt_float(score.get("strain"))
        kcal = kj_to_kcal(score.get("kilojoule"))
        avg_hr = _opt_int(score.get("average_heart_rate"))
        max_hr = _opt_int(score.get("max_heart_rate"))
        zones = zones_to_minutes(score.get("zone_durations"))
        distance = _opt_float(score.get("distance_meter"))
        raw["kilojoule"] = score.get("kilojoule")
        raw["percent_recorded"] = score.get("percent_recorded")
        raw["altitude_gain_meter"] = score.get("altitude_gain_meter")
        percent = _opt_float(score.get("percent_recorded"))
        if percent is not None and percent < 50:
            raw["low_hr_coverage"] = True
    return WorkoutRecord(
        external_id=str(external_id),
        sport=sport,
        started_at=started,
        ended_at=ended,
        duration_min=round((ended - started).total_seconds() / 60, 1),
        strain=strain,
        kcal=kcal,
        avg_hr=avg_hr,
        max_hr=max_hr,
        zones_min=zones,
        distance_m=distance,
        raw=raw,
    )


def map_sleep(rec: Mapping[str, Any]) -> SleepRecord | None:
    external_id = rec.get("id")
    started = parse_ts(rec.get("start"))
    ended = parse_ts(rec.get("end"))
    state = str(rec.get("score_state") or "")
    if not external_id or started is None or ended is None or state == PENDING:
        return None
    score = rec.get("score") if state == SCORED and isinstance(rec.get("score"), dict) else None
    cycle_id = _opt_int(rec.get("cycle_id"))
    raw: dict[str, Any] = {
        "score_state": state,
        "nap": bool(rec.get("nap")),
        "cycle_id": cycle_id,
        "timezone_offset": rec.get("timezone_offset"),
        "updated_at": rec.get("updated_at"),
    }
    in_bed = asleep = performance = resp = None
    stages: dict[str, float] | None = None
    disturbances: int | None = None
    if score is not None:
        summary = score.get("stage_summary") if isinstance(score.get("stage_summary"), dict) else {}
        light = _opt_float(summary.get("total_light_sleep_time_milli")) or 0.0
        deep = _opt_float(summary.get("total_slow_wave_sleep_time_milli")) or 0.0
        rem = _opt_float(summary.get("total_rem_sleep_time_milli")) or 0.0
        awake = _opt_float(summary.get("total_awake_time_milli")) or 0.0
        in_bed = _opt_float(summary.get("total_in_bed_time_milli"))
        in_bed = None if in_bed is None else round(in_bed / MS_PER_MIN, 1)
        asleep = round((light + deep + rem) / MS_PER_MIN, 1)
        stages = {
            "light": round(light / MS_PER_MIN, 1),
            "deep": round(deep / MS_PER_MIN, 1),
            "rem": round(rem / MS_PER_MIN, 1),
            "awake": round(awake / MS_PER_MIN, 1),
        }
        disturbances = _opt_int(summary.get("disturbance_count"))
        performance = _opt_float(score.get("sleep_performance_percentage"))
        resp = _opt_float(score.get("respiratory_rate"))
        raw["sleep_needed"] = score.get("sleep_needed")
        raw["sleep_efficiency_percentage"] = score.get("sleep_efficiency_percentage")
        raw["sleep_consistency_percentage"] = score.get("sleep_consistency_percentage")
        raw["sleep_cycle_count"] = summary.get("sleep_cycle_count")
    return SleepRecord(
        external_id=str(external_id),
        started_at=started,
        ended_at=ended,
        nap=bool(rec.get("nap")),
        cycle_id=cycle_id,
        in_bed_min=in_bed,
        asleep_min=asleep,
        performance_pct=performance,
        stages_min=stages,
        respiratory_rate=resp,
        disturbances=disturbances,
        raw=raw,
    )


def map_recovery(
    rec: Mapping[str, Any], *, tz: str, sleep_end: datetime | None = None
) -> RecoveryRecord | None:
    """Recovery has no date of its own: it is the local day the associated sleep ended (or, when
    the sleep is unknown, the local day the recovery was created)."""
    cycle_id = _opt_int(rec.get("cycle_id"))
    state = str(rec.get("score_state") or "")
    if cycle_id is None or state == PENDING:
        return None
    anchor = sleep_end or parse_ts(rec.get("created_at"))
    if anchor is None:
        return None
    score = rec.get("score") if state == SCORED and isinstance(rec.get("score"), dict) else None
    raw: dict[str, Any] = {
        "score_state": state,
        "sleep_id": rec.get("sleep_id"),
        "updated_at": rec.get("updated_at"),
    }
    result = RecoveryRecord(
        external_id=str(cycle_id),
        sleep_id=str(rec["sleep_id"]) if rec.get("sleep_id") else None,
        day=to_local(anchor, tz).date(),
        raw=raw,
    )
    if score is None:
        return result
    raw["user_calibrating"] = bool(score.get("user_calibrating"))
    return RecoveryRecord(
        external_id=result.external_id,
        sleep_id=result.sleep_id,
        day=result.day,
        score=_opt_float(score.get("recovery_score")),
        rhr=_opt_float(score.get("resting_heart_rate")),
        hrv_ms=_opt_float(score.get("hrv_rmssd_milli")),
        spo2=_opt_float(score.get("spo2_percentage")),
        skin_temp_c=_opt_float(score.get("skin_temp_celsius")),
        raw=raw,
    )


def cycle_summary(rec: Mapping[str, Any]) -> dict[str, Any] | None:
    """Day strain from ``/v2/cycle`` (no table of its own: it rides on the recovery's ``raw``)."""
    cycle_id = _opt_int(rec.get("id"))
    if cycle_id is None or str(rec.get("score_state") or "") != SCORED:
        return None
    raw_score = rec.get("score")
    score: dict[str, Any] = raw_score if isinstance(raw_score, dict) else {}
    return {
        "cycle_id": cycle_id,
        "start": rec.get("start"),
        "end": rec.get("end"),
        "strain": _opt_float(score.get("strain")),
        "kcal": kj_to_kcal(score.get("kilojoule")),
        "average_heart_rate": _opt_int(score.get("average_heart_rate")),
        "max_heart_rate": _opt_int(score.get("max_heart_rate")),
    }


# ------------------------------------------------------------------------------ integration


class WhoopIntegration:
    provider: ProviderName = PROVIDER

    def __init__(
        self,
        settings: Settings,
        *,
        cipher: TokenCipher,
        clock: Clock | None = None,
        client_factory: ClientFactory | None = None,
        initial_sync_days: int = 7,
        timeout_s: float = 10.0,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._clock: Clock = clock or SystemClock()
        self._client_factory: ClientFactory = client_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))
        )
        self._initial_sync_days = initial_sync_days
        self._locks: dict[int, asyncio.Lock] = {}
        self._seen_traces: OrderedDict[str, None] = OrderedDict()

    # --- configuration --------------------------------------------------------------------
    @property
    def client_id(self) -> str:
        return self._settings.whoop_client_id or ""

    @property
    def client_secret(self) -> str:
        secret = self._settings.whoop_client_secret
        return secret.get_secret_value() if secret is not None else ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return callback_url(self._settings, PROVIDER)

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    # --- connect / callback ---------------------------------------------------------------
    async def connect(self, session: AsyncSession, user: User) -> ConnectInfo:
        if not self.configured:
            return ConnectInfo(
                provider=PROVIDER,
                kind="instructions",
                instructions=_copy(user.language, "not_configured"),
            )
        state = await issue_state(session, user.id, PROVIDER, now=self._clock.now())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": SCOPES,
                "state": state,
            }
        )
        return ConnectInfo(
            provider=PROVIDER,
            kind="oauth",
            url=f"{AUTH_URL}?{query}",
            instructions=_copy(user.language, "connect"),
            extra={"state": state},
        )

    async def handle_callback(
        self, session: AsyncSession, query: dict[str, str]
    ) -> tuple[User | None, str]:
        now = self._clock.now()
        state = query.get("state", "")
        user_id = await consume_state(session, state, PROVIDER, now=now)
        if user_id is None:
            return None, _copy(None, "expired")
        user = await repo.get_user(session, user_id)
        if user is None:
            return None, _copy(None, "expired")
        if query.get("error") or not query.get("code"):
            log.info("whoop_callback_denied", user_id=user_id, error=query.get("error"))
            return user, _copy(user.language, "denied")
        try:
            async with self._client_factory() as client:
                tokens = await self._token_request(
                    client,
                    {
                        "grant_type": "authorization_code",
                        "code": query["code"],
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri,
                    },
                )
                await self._store_tokens(session, user.id, tokens, now=now)
                profile = await self._get(
                    client, str(tokens["access_token"]), "/v2/user/profile/basic"
                )
                external = profile.get("user_id")
                if external is not None:
                    await repo.upsert_integration(
                        session, user.id, PROVIDER, now=now, external_user_id=str(external)
                    )
        except WhoopError as exc:
            log.warning("whoop_callback_failed", user_id=user.id, error=str(exc))
            await repo.upsert_integration(
                session, user.id, PROVIDER, now=now, status=IntegrationStatus.error
            )
            return user, _copy(user.language, "failed")
        counts = {"workouts": 0, "sleeps": 0, "recoveries": 0}
        try:
            events = await self.sync(session, user, now - timedelta(days=self._initial_sync_days))
        except Exception as exc:  # the connection is fine even if the backfill is not
            log.warning("whoop_initial_sync_failed", user_id=user.id, error=repr(exc))
        else:
            for event in events:
                if isinstance(event, WorkoutEvent):
                    counts["workouts"] += 1
                elif isinstance(event, SleepEvent):
                    counts["sleeps"] += 1
                elif isinstance(event, RecoveryEvent):
                    counts["recoveries"] += 1
        log.info("whoop_connected", user_id=user.id, **counts)
        return user, _copy(user.language, "connected", **counts)

    # --- tokens ---------------------------------------------------------------------------
    async def _token_request(
        self, client: httpx.AsyncClient, data: Mapping[str, str]
    ) -> dict[str, Any]:
        try:
            response = await client.post(TOKEN_URL, data=dict(data))
        except httpx.HTTPError as exc:
            raise WhoopError(f"token request failed: {exc!r}") from exc
        if response.status_code in (400, 401):
            raise WhoopAuthError(f"token request rejected: {response.status_code}")
        if response.status_code >= 300:
            raise WhoopError(f"token request failed: {response.status_code}")
        body = _json(response)
        if "access_token" not in body:
            raise WhoopError("token response without access_token")
        return body

    async def _store_tokens(
        self, session: AsyncSession, user_id: int, tokens: Mapping[str, Any], *, now: datetime
    ) -> Integration:
        expires_in = _opt_float(tokens.get("expires_in")) or 3600.0
        return await repo.set_integration_tokens(
            session,
            self._cipher,
            user_id,
            PROVIDER,
            access_token=str(tokens["access_token"]),
            refresh_token=(str(tokens["refresh_token"]) if tokens.get("refresh_token") else None),
            expires_at=now + timedelta(seconds=expires_in),
            now=now,
            scopes=str(tokens.get("scope")) if tokens.get("scope") else None,
        )

    async def _refresh(
        self, session: AsyncSession, row: Integration, client: httpx.AsyncClient
    ) -> str:
        """Rotate the token pair (single-flight per user). Raises ``WhoopAuthError`` when the
        refresh token is gone, marking the integration expired."""
        async with self._lock(row.user_id):
            tokens = repo.integration_tokens(self._cipher, row)
            if tokens.refresh_token is None:
                await repo.set_integration_status(
                    session, row.user_id, PROVIDER, IntegrationStatus.expired
                )
                raise WhoopAuthError("no refresh token stored")
            try:
                fresh = await self._token_request(
                    client,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tokens.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "scope": "offline",
                    },
                )
            except WhoopAuthError:
                await repo.set_integration_status(
                    session, row.user_id, PROVIDER, IntegrationStatus.expired
                )
                raise
            if not fresh.get("refresh_token"):
                fresh = {**fresh, "refresh_token": tokens.refresh_token}
            updated = await self._store_tokens(session, row.user_id, fresh, now=self._clock.now())
            log.info("whoop_token_refreshed", user_id=row.user_id)
            return str(repo.integration_tokens(self._cipher, updated).access_token)

    async def _access_token(
        self, session: AsyncSession, row: Integration, client: httpx.AsyncClient
    ) -> str:
        tokens = repo.integration_tokens(self._cipher, row)
        now = self._clock.now()
        if (
            tokens.access_token
            and tokens.expires_at is not None
            and ensure_utc(tokens.expires_at) - now > timedelta(seconds=60)
        ):
            return tokens.access_token
        return await self._refresh(session, row, client)

    # --- HTTP -----------------------------------------------------------------------------
    async def _get(
        self,
        client: httpx.AsyncClient,
        token: str,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(2):
            try:
                response = await client.get(url, params=dict(params or {}), headers=headers)
            except httpx.HTTPError as exc:
                raise WhoopError(f"GET {path} failed: {exc!r}") from exc
            if response.status_code == 429 and attempt == 0:
                reset = _opt_float(response.headers.get("X-RateLimit-Reset")) or 1.0
                log.warning("whoop_rate_limited", path=path, reset_s=reset)
                await asyncio.sleep(min(max(reset, 0.0), 30.0))
                continue
            if response.status_code == 401:
                raise WhoopAuthError(f"GET {path}: unauthorized")
            if response.status_code == 404:
                raise WhoopError(f"GET {path}: not found")
            if response.status_code >= 300:
                raise WhoopError(f"GET {path}: {response.status_code}")
            return _json(response)
        raise WhoopError(f"GET {path}: rate limited")

    async def _authed_get(
        self,
        session: AsyncSession,
        row: Integration,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET with the stored token; on 401 refresh once and retry."""
        token = await self._access_token(session, row, client)
        try:
            return await self._get(client, token, path, params)
        except WhoopAuthError:
            token = await self._refresh(session, row, client)
            return await self._get(client, token, path, params)

    async def _paginate(
        self,
        session: AsyncSession,
        row: Integration,
        client: httpx.AsyncClient,
        path: str,
        *,
        start: datetime,
        end: datetime,
        max_pages: int = 40,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        next_token: str | None = None
        for _ in range(max_pages):
            params: dict[str, str] = {
                "limit": str(PAGE_LIMIT),
                "start": _iso(start),
                "end": _iso(end),
            }
            if next_token:
                params["nextToken"] = next_token
            page = await self._authed_get(session, row, client, path, params)
            batch = page.get("records")
            if isinstance(batch, list):
                records.extend(item for item in batch if isinstance(item, dict))
            next_token = page.get("next_token") or None
            if not next_token:
                break
        return records

    # --- sync -----------------------------------------------------------------------------
    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        row = await repo.get_integration(session, user.id, PROVIDER)
        if row is None or row.status != IntegrationStatus.connected:
            return []
        now = self._clock.now()
        if since is None:
            last = row.last_sync_at
            since = (
                ensure_utc(last) - timedelta(days=1)
                if last is not None
                else now - timedelta(days=self._initial_sync_days)
            )
        since = ensure_utc(since)
        try:
            async with self._client_factory() as client:
                workouts = await self._paginate(
                    session, row, client, "/v2/activity/workout", start=since, end=now
                )
                sleeps = await self._paginate(
                    session, row, client, "/v2/activity/sleep", start=since, end=now
                )
                recoveries = await self._paginate(
                    session, row, client, "/v2/recovery", start=since, end=now
                )
                cycles = await self._paginate(
                    session, row, client, "/v2/cycle", start=since, end=now
                )
        except WhoopAuthError as exc:
            log.warning("whoop_sync_unauthorized", user_id=user.id, error=str(exc))
            await repo.set_integration_status(session, user.id, PROVIDER, IntegrationStatus.expired)
            return []
        except WhoopError as exc:
            log.warning("whoop_sync_failed", user_id=user.id, error=str(exc))
            return []
        events = await self._store_batch(session, user, workouts, sleeps, recoveries, cycles)
        await repo.set_integration_status(
            session, user.id, PROVIDER, IntegrationStatus.connected, last_sync_at=now
        )
        log.info("whoop_synced", user_id=user.id, since=since.isoformat(), events=len(events))
        return events

    async def _store_batch(
        self,
        session: AsyncSession,
        user: User,
        workouts: list[dict[str, Any]],
        sleeps: list[dict[str, Any]],
        recoveries: list[dict[str, Any]],
        cycles: list[dict[str, Any]],
    ) -> list[Event]:
        events: list[Event] = []
        for raw in reversed(workouts):  # oldest first so events arrive in order
            workout = map_workout(raw)
            if workout is not None:
                events.append(await self._store_workout(session, user, workout))
        sleep_ends: dict[int, datetime] = {}
        for raw in reversed(sleeps):
            sleep = map_sleep(raw)
            if sleep is None:
                continue
            if sleep.cycle_id is not None and not sleep.nap:
                sleep_ends[sleep.cycle_id] = sleep.ended_at
            event = await self._store_sleep(session, user, sleep)
            if event is not None:
                events.append(event)
        # sleeps already in the DB (from an earlier sync) still anchor a recovery's date
        for raw in recoveries:
            cycle_id = _opt_int(raw.get("cycle_id"))
            if cycle_id is not None and cycle_id not in sleep_ends and raw.get("sleep_id"):
                known = await store.get_sleep_by_external(
                    session, user.id, DataSource.whoop, str(raw["sleep_id"])
                )
                if known is not None:
                    sleep_ends[cycle_id] = ensure_utc(known.ended_at)
        cycle_by_id = {
            summary["cycle_id"]: summary
            for summary in (cycle_summary(raw) for raw in cycles)
            if summary is not None
        }
        for raw in reversed(recoveries):
            cycle_id = _opt_int(raw.get("cycle_id"))
            recovery = map_recovery(
                raw, tz=user.timezone, sleep_end=sleep_ends.get(cycle_id) if cycle_id else None
            )
            if recovery is None:
                continue
            cycle = cycle_by_id.get(int(recovery.external_id))
            if cycle is not None:
                recovery = RecoveryRecord(
                    external_id=recovery.external_id,
                    sleep_id=recovery.sleep_id,
                    day=recovery.day,
                    score=recovery.score,
                    rhr=recovery.rhr,
                    hrv_ms=recovery.hrv_ms,
                    spo2=recovery.spo2,
                    skin_temp_c=recovery.skin_temp_c,
                    raw={**recovery.raw, "cycle": cycle},
                )
            events.append(await self._store_recovery(session, user, recovery))
        return events

    async def _store_workout(
        self, session: AsyncSession, user: User, record: WorkoutRecord
    ) -> WorkoutEvent:
        row, created = await repo.upsert_workout_by_external(
            session,
            user.id,
            source=DataSource.whoop,
            external_id=record.external_id,
            sport=record.sport,
            started_at=record.started_at,
            now=self._clock.now(),
            ended_at=record.ended_at,
            duration_min=record.duration_min,
            strain=record.strain,
            kcal=record.kcal,
            avg_hr=record.avg_hr,
            max_hr=record.max_hr,
            zones_min=record.zones_min,
            distance_m=record.distance_m,
            raw=record.raw,
        )
        return WorkoutEvent(
            user_id=user.id,
            occurred_at=record.ended_at,
            source=PROVIDER,
            external_id=record.external_id,
            sport=record.sport,
            started_at=record.started_at,
            ended_at=record.ended_at,
            duration_min=record.duration_min,
            strain=record.strain,
            kcal=record.kcal,
            avg_hr=record.avg_hr,
            max_hr=record.max_hr,
            zones_min=record.zones_min,
            distance_m=record.distance_m,
            raw={**record.raw, "created": created, "row_id": row.id},
        )

    async def _store_sleep(
        self, session: AsyncSession, user: User, record: SleepRecord
    ) -> SleepEvent | None:
        row, created = await repo.upsert_sleep_by_external(
            session,
            user.id,
            source=DataSource.whoop,
            external_id=record.external_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            now=self._clock.now(),
            in_bed_min=record.in_bed_min,
            asleep_min=record.asleep_min,
            performance_pct=record.performance_pct,
            stages_min=record.stages_min,
            respiratory_rate=record.respiratory_rate,
            disturbances=record.disturbances,
            raw=record.raw,
        )
        if record.nap:
            return None  # stored (raw.nap = true) but naps do not drive sleep coaching
        return SleepEvent(
            user_id=user.id,
            occurred_at=record.ended_at,
            source=PROVIDER,
            external_id=record.external_id,
            started_at=record.started_at,
            ended_at=record.ended_at,
            in_bed_min=record.in_bed_min,
            asleep_min=record.asleep_min,
            performance_pct=record.performance_pct,
            stages_min=record.stages_min,
            respiratory_rate=record.respiratory_rate,
            disturbances=record.disturbances,
            raw={**record.raw, "created": created, "row_id": row.id},
        )

    async def _store_recovery(
        self, session: AsyncSession, user: User, record: RecoveryRecord
    ) -> RecoveryEvent:
        row, created = await repo.upsert_recovery_by_external(
            session,
            user.id,
            source=DataSource.whoop,
            external_id=record.external_id,
            day=record.day,
            score=record.score,
            rhr=record.rhr,
            hrv_ms=record.hrv_ms,
            spo2=record.spo2,
            skin_temp_c=record.skin_temp_c,
            raw=record.raw,
        )
        return RecoveryEvent(
            user_id=user.id,
            occurred_at=self._clock.now(),
            source=PROVIDER,
            external_id=record.external_id,
            date=record.day,
            score=record.score,
            rhr=record.rhr,
            hrv_ms=record.hrv_ms,
            spo2=record.spo2,
            skin_temp_c=record.skin_temp_c,
            raw={**record.raw, "created": created, "row_id": row.id},
        )

    # --- webhooks -------------------------------------------------------------------------
    def _seen(self, trace_id: str | None) -> bool:
        if not trace_id:
            return False
        if trace_id in self._seen_traces:
            return True
        self._seen_traces[trace_id] = None
        while len(self._seen_traces) > 512:
            self._seen_traces.popitem(last=False)
        return False

    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        if request.method.upper() == "HEAD":
            return WebhookResponse(), []
        now = self._clock.now()
        if not verify_signature(self.client_secret, request.headers, request.body, now=now):
            log.warning("whoop_webhook_bad_signature")
            return WebhookResponse(status=401, body="invalid signature"), []
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return WebhookResponse(status=400, body="invalid json"), []
        if not isinstance(payload, dict):
            return WebhookResponse(status=400, body="invalid payload"), []
        event_type = str(payload.get("type") or "")
        object_id = str(payload.get("id") or "")
        external_user = payload.get("user_id")
        trace_id = payload.get("trace_id")
        if self._seen(str(trace_id) if trace_id else None):
            return WebhookResponse(body="duplicate"), []
        if external_user is None or not object_id or not event_type:
            return WebhookResponse(status=400, body="missing fields"), []
        row = await repo.integration_by_external_user(session, PROVIDER, str(external_user))
        if row is None:
            log.info("whoop_webhook_unknown_user", kind=event_type)
            return WebhookResponse(body="unknown user"), []
        user = await repo.get_user(session, row.user_id)
        if user is None:
            return WebhookResponse(body="unknown user"), []
        try:
            events = await self._apply_webhook(session, row, user, event_type, object_id)
        except WhoopAuthError as exc:
            log.warning("whoop_webhook_unauthorized", user_id=user.id, error=str(exc))
            await repo.set_integration_status(session, user.id, PROVIDER, IntegrationStatus.expired)
            return WebhookResponse(body="token expired"), []
        except WhoopError as exc:
            log.warning("whoop_webhook_fetch_failed", user_id=user.id, error=str(exc))
            return WebhookResponse(status=502, body="upstream error"), []
        log.info("whoop_webhook", user_id=user.id, kind=event_type, events=len(events))
        return WebhookResponse(), events

    async def _apply_webhook(
        self,
        session: AsyncSession,
        row: Integration,
        user: User,
        event_type: str,
        object_id: str,
    ) -> list[Event]:
        if event_type == "workout.deleted":
            await store.delete_workout_by_external(session, user.id, DataSource.whoop, object_id)
            return []
        if event_type == "sleep.deleted":
            await self._delete_recovery_for_sleep(session, user.id, object_id)
            await store.delete_sleep_by_external(session, user.id, DataSource.whoop, object_id)
            return []
        if event_type == "recovery.deleted":
            await self._delete_recovery_for_sleep(session, user.id, object_id)
            return []
        if event_type not in {"workout.updated", "sleep.updated", "recovery.updated"}:
            log.info("whoop_webhook_ignored", kind=event_type)
            return []
        async with self._client_factory() as client:
            if event_type == "workout.updated":
                data = await self._authed_get(
                    session, row, client, f"/v2/activity/workout/{object_id}"
                )
                record = map_workout(data)
                return [] if record is None else [await self._store_workout(session, user, record)]
            sleep_data = await self._authed_get(
                session, row, client, f"/v2/activity/sleep/{object_id}"
            )
            sleep_record = map_sleep(sleep_data)
            events: list[Event] = []
            if event_type == "sleep.updated":
                if sleep_record is not None:
                    sleep_event = await self._store_sleep(session, user, sleep_record)
                    if sleep_event is not None:
                        events.append(sleep_event)
                return events
            cycle_id = _opt_int(sleep_data.get("cycle_id"))
            if cycle_id is None:
                return events
            recovery_data = await self._authed_get(
                session, row, client, f"/v2/cycle/{cycle_id}/recovery"
            )
            sleep_end = sleep_record.ended_at if sleep_record is not None else None
            recovery = map_recovery(recovery_data, tz=user.timezone, sleep_end=sleep_end)
            if recovery is not None:
                events.append(await self._store_recovery(session, user, recovery))
            return events

    async def _delete_recovery_for_sleep(
        self, session: AsyncSession, user_id: int, sleep_id: str
    ) -> None:
        sleep = await store.get_sleep_by_external(session, user_id, DataSource.whoop, sleep_id)
        cycle_id = (sleep.raw or {}).get("cycle_id") if sleep is not None else None
        if cycle_id is not None:
            await store.delete_recovery_by_external(
                session, user_id, DataSource.whoop, str(cycle_id)
            )


# ---------------------------------------------------------------------------------- helpers


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise WhoopError("response is not JSON") from exc
    if not isinstance(body, dict):
        raise WhoopError("response is not a JSON object")
    return cast("dict[str, Any]", body)
