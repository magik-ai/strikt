"""Withings integration: OAuth2, ``getmeas`` decoding, ``notify`` subscription, webhook.

Facts from research/05-scales-apple-health.md §1 (verified on 2026-09-03):

- Authorize ``https://account.withings.com/oauth2_user/authorize2`` with comma-separated scopes
  ``user.info,user.metrics,user.activity``; the code is valid for 30 seconds.
- Token ``POST https://wbsapi.withings.net/v2/oauth2`` with ``action=requesttoken``; every
  response is HTTP 200 with a JSON ``status`` (0 = ok). Access tokens live 3 h, refresh tokens
  1 year and rotate on every refresh.
- ``POST https://wbsapi.withings.net/measure`` ``action=getmeas`` returns ``measuregrps`` whose
  measures are ``value * 10^unit``; ``more``/``offset`` paginate; ``lastupdate`` is the sync cursor.
- ``POST https://wbsapi.withings.net/v2/measure`` ``action=getactivity`` returns one row per day
  (``activities``: ``date``, ``timezone``, ``steps``, ``calories`` = active kcal, ``totalcalories``,
  ``active`` seconds) with the same ``more``/``offset`` paging and ``lastupdate`` cursor; it needs
  ``user.activity``. Steps come from a Withings watch or the Health Mate app's phone tracking.
- Notification categories (``appli``, developer guide "Payload and Categories", 2026-11): 1 weight
  and body composition, 4 blood pressure and heart rate, 16 activity (posted with ``date``), 44
  sleep. Strikt subscribes to 1, 4 and 16; sleep comes from WHOOP.
- ``POST https://wbsapi.withings.net/notify`` ``action=subscribe&appli=1`` needs a public HTTPS
  callback that answers ``HEAD`` with 2xx. Notifications are unsigned form POSTs
  (``userid``, ``appli``, ``startdate``, ``enddate``). They are treated as a *hint only*: the
  body's window is ignored, the data is re-fetched from the stored cursor with the user's own
  token, the cursor is advanced only by the scheduled ``sync``, and a user is fetched for at most
  once per ``WEBHOOK_MIN_INTERVAL`` - so a spoofed POST (user ids are small integers) can
  neither move the cursor past an unimported weigh-in nor burn the user's API quota.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, SystemClock, ensure_utc, to_local
from strikt.db import repo
from strikt.db.models import IntegrationStatus, MeasurementType, User
from strikt.events import Event, MeasurementEvent
from strikt.integrations import store
from strikt.integrations.base import (
    ConnectInfo,
    ProviderName,
    WebhookRequest,
    WebhookResponse,
    link_copy,
)
from strikt.integrations.oauth import callback_url, consume_state, issue_state, webhook_url
from strikt.telegram.copy import t

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import Integration

log = structlog.get_logger(__name__)

PROVIDER: ProviderName = "withings"
AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
MEASURE_V2_URL = "https://wbsapi.withings.net/v2/measure"
NOTIFY_URL = "https://wbsapi.withings.net/notify"
SCOPES = "user.info,user.metrics,user.activity"
APPLI_WEIGHT = 1
APPLI_PRESSURE = 4
APPLI_ACTIVITY = 16
#: The notification categories Strikt subscribes to (sleep, 44, comes from WHOOP).
SUBSCRIBED_APPLIS: tuple[int, ...] = (APPLI_WEIGHT, APPLI_PRESSURE, APPLI_ACTIVITY)
ACTIVITY_FIELDS = "steps,distance,calories,totalcalories,active"
DEFAULT_WINDOW_DAYS = 30
#: A webhook-triggered fetch per user at most this often (the poller covers the rest).
WEBHOOK_MIN_INTERVAL = timedelta(minutes=1)
ATTRIB_AMBIGUOUS = 1

# meastype → (measurement type in our table, unit, metric name used in events / note)
MEASTYPES: dict[int, tuple[MeasurementType, str, str]] = {
    1: (MeasurementType.weight, "kg", "weight"),
    5: (MeasurementType.other, "kg", "lean_mass_kg"),
    6: (MeasurementType.bodyfat, "%", "bodyfat"),
    8: (MeasurementType.other, "kg", "fat_mass_kg"),
    9: (MeasurementType.bp_dia, "mmHg", "bp_dia"),
    10: (MeasurementType.bp_sys, "mmHg", "bp_sys"),
    11: (MeasurementType.other, "bpm", "heart_rate"),
    54: (MeasurementType.other, "%", "spo2"),
    71: (MeasurementType.other, "C", "body_temp_c"),
    76: (MeasurementType.other, "kg", "muscle_mass_kg"),
    77: (MeasurementType.other, "kg", "water_kg"),
    88: (MeasurementType.other, "kg", "bone_mass_kg"),
    91: (MeasurementType.other, "m/s", "pulse_wave_velocity"),
    155: (MeasurementType.other, "years", "vascular_age"),
    168: (MeasurementType.other, "kg", "extracellular_water_kg"),
    169: (MeasurementType.other, "kg", "intracellular_water_kg"),
    170: (MeasurementType.other, "index", "visceral_fat"),
    226: (MeasurementType.other, "kcal", "bmr_kcal"),
    227: (MeasurementType.other, "years", "metabolic_age"),
}
MEASTYPES_PARAM = ",".join(str(code) for code in MEASTYPES)

ClientFactory = Callable[[], httpx.AsyncClient]


def _copy(lang: str | None, key: str, **kwargs: Any) -> str:
    """Callback and connect copy in the user's language (``telegram/locales``)."""
    if key == "connected":
        return t(lang, "withings.connected", **kwargs)
    return link_copy(lang, key, PROVIDER, **kwargs)


class WithingsError(RuntimeError):
    """A Withings call failed (network, non-zero ``status``, malformed body)."""


class WithingsAuthError(WithingsError):
    """The token is invalid and could not be refreshed."""


class WithingsTransportError(WithingsError):
    """Withings did not answer properly (network, HTTP error, rate limit): try again later. Never
    a reason to call the connection dead."""


#: Withings ``status`` codes that mean "not now", not "no".
_TRANSIENT_STATUSES = frozenset({601, 2554, 2555})


# --------------------------------------------------------------------------------- decoding


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def decode_value(value: Any, unit: Any) -> float | None:
    """``value * 10^unit`` (Withings encodes 65.750 kg as ``value=65750, unit=-3``)."""
    try:
        return round(float(value) * (10.0 ** int(unit)), 6)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True, kw_only=True)
class Reading:
    grpid: int
    meastype: int
    type: MeasurementType
    metric: str
    value: float
    unit: str
    measured_at: datetime
    attrib: int
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def decode_groups(groups: Any, *, skip_ambiguous: bool = True) -> list[Reading]:
    """``measuregrps`` → readings for the meastypes we know; ambiguous-user groups are skipped."""
    readings: list[Reading] = []
    if not isinstance(groups, list):
        return readings
    for group in groups:
        if not isinstance(group, dict):
            continue
        grpid_int = _to_int(group.get("grpid"))
        taken = _to_int(group.get("date"))
        attrib_int = _to_int(group.get("attrib", 0))
        if grpid_int is None or taken is None or attrib_int is None:
            continue
        try:
            measured_at = datetime.fromtimestamp(taken, tz=UTC)
        except (OverflowError, OSError, ValueError):
            continue
        if skip_ambiguous and attrib_int == ATTRIB_AMBIGUOUS:
            continue
        if int(group.get("category", 1) or 1) != 1:
            continue  # category 2 = user objectives, not measures
        model = group.get("model")
        for measure in group.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            meastype = _to_int(measure.get("type"))
            if meastype is None:
                continue
            spec = MEASTYPES.get(meastype)
            if spec is None:
                continue
            value = decode_value(measure.get("value"), measure.get("unit"))
            if value is None:
                continue
            db_type, unit, metric = spec
            readings.append(
                Reading(
                    grpid=grpid_int,
                    meastype=meastype,
                    type=db_type,
                    metric=metric,
                    value=value,
                    unit=unit,
                    measured_at=measured_at,
                    attrib=attrib_int,
                    model=str(model) if model else None,
                    raw={
                        "grpid": grpid_int,
                        "type": meastype,
                        "value": measure.get("value"),
                        "unit": measure.get("unit"),
                        "attrib": attrib_int,
                        "model": model,
                        "deviceid": group.get("deviceid"),
                    },
                )
            )
    return readings


def parse_notification(body: bytes, query: Mapping[str, str]) -> dict[str, str]:
    """Withings POSTs ``application/x-www-form-urlencoded``; some setups put it in the query."""
    fields: dict[str, str] = dict(query.items())
    if body:
        try:
            parsed = parse_qs(body.decode("utf-8"), keep_blank_values=False)
        except UnicodeDecodeError:
            parsed = {}
        for key, values in parsed.items():
            if values:
                fields[key] = values[0]
    return fields


@dataclass(frozen=True, kw_only=True)
class ActivityDay:
    """One day of ``getactivity``: the reading is filed at local noon of that day."""

    day: date
    measured_at: datetime
    steps: int | None
    active_kcal: float | None
    total_kcal: float | None
    raw: dict[str, Any] = field(default_factory=dict)


def decode_activities(activities: Any, *, default_tz: str = "UTC") -> list[ActivityDay]:
    """``body.activities`` → one :class:`ActivityDay` per date (malformed rows are skipped)."""
    days: list[ActivityDay] = []
    if not isinstance(activities, list):
        return days
    for row in activities:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row.get("date") or ""))
        except ValueError:
            continue
        tz = str(row.get("timezone") or default_tz)
        try:
            measured_at = datetime.combine(day, time(12, 0), tzinfo=ZoneInfo(tz)).astimezone(UTC)
        except (ZoneInfoNotFoundError, ValueError):
            measured_at = datetime.combine(day, time(12, 0), tzinfo=UTC)
        steps = _to_int(row.get("steps"))
        active = _to_float(row.get("calories"))
        total = _to_float(row.get("totalcalories"))
        if not steps and not active:
            continue  # an empty day (no tracker worn, no phone in the pocket) says nothing
        days.append(
            ActivityDay(
                day=day,
                measured_at=measured_at,
                steps=steps,
                active_kcal=active,
                total_kcal=total,
                raw={
                    "date": day.isoformat(),
                    "timezone": tz,
                    "distance_m": _to_float(row.get("distance")),
                    "active_s": _to_int(row.get("active")),
                    "total_kcal": total,
                    "is_tracker": row.get("is_tracker"),
                    "modified": row.get("modified"),
                },
            )
        )
    return days


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError, OverflowError):
        return None


# ------------------------------------------------------------------------------ integration


class WithingsIntegration:
    provider: ProviderName = PROVIDER

    def __init__(
        self,
        settings: Settings,
        *,
        cipher: TokenCipher,
        clock: Clock | None = None,
        client_factory: ClientFactory | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._clock: Clock = clock or SystemClock()
        self._client_factory: ClientFactory = client_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(timeout_s))
        )
        self._webhook_fetch_at: dict[tuple[int, str], datetime] = {}

    @property
    def client_id(self) -> str:
        return self._settings.withings_client_id or ""

    @property
    def client_secret(self) -> str:
        secret = self._settings.withings_client_secret
        return secret.get_secret_value() if secret is not None else ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return callback_url(self._settings, PROVIDER)

    @property
    def notify_url(self) -> str:
        return webhook_url(self._settings, PROVIDER)

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
                "scope": SCOPES,
                "redirect_uri": self.redirect_uri,
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
        user_id = await consume_state(session, query.get("state", ""), PROVIDER, now=now)
        if user_id is None:
            return None, _copy(None, "expired")
        user = await repo.get_user(session, user_id)
        if user is None:
            return None, _copy(None, "expired")
        if query.get("error") or not query.get("code"):
            log.info("withings_callback_denied", user_id=user_id, error=query.get("error"))
            return user, _copy(user.language, "denied")
        try:
            async with self._client_factory() as client:
                body = await self._token_request(
                    client,
                    {
                        "action": "requesttoken",
                        "grant_type": "authorization_code",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "code": query["code"],
                        "redirect_uri": self.redirect_uri,
                    },
                )
                row = await self._store_tokens(session, user.id, body, now=now)
                accepted = await self._subscribe(client, str(body["access_token"]))
                row.data = {
                    **(row.data or {}),
                    "subscribed": accepted,
                    "subscribe_tried": now.date().isoformat(),
                }
        except WithingsError as exc:
            log.warning("withings_callback_failed", user_id=user.id, error=str(exc))
            await repo.upsert_integration(
                session, user.id, PROVIDER, now=now, status=IntegrationStatus.error
            )
            return user, _copy(user.language, "failed")
        count = 0
        try:
            count = len(await self.sync(session, user, None))
        except Exception as exc:
            log.warning("withings_initial_sync_failed", user_id=user.id, error=repr(exc))
        log.info("withings_connected", user_id=user.id, readings=count)
        return user, _copy(user.language, "connected", count=count)

    async def _subscribe(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        applis: tuple[int, ...] = SUBSCRIBED_APPLIS,
    ) -> list[int]:
        """``notify subscribe`` per category; returns the ones Withings accepted. A failure is
        logged, not fatal: the 30-minute poll still brings everything in."""
        accepted: list[int] = []
        for appli in applis:
            try:
                body = await self._call(
                    client,
                    NOTIFY_URL,
                    {
                        "action": "subscribe",
                        "callbackurl": self.notify_url,
                        "appli": str(appli),
                        "comment": "Strikt sync",
                    },
                    access_token=access_token,
                )
            except WithingsError as exc:
                log.warning(
                    "withings_subscribe_failed",
                    appli=appli,
                    error=str(exc),
                    callback=self.notify_url,
                )
                continue
            accepted.append(appli)
            log.info("withings_subscribed", appli=appli, status=body.get("status"))
        return accepted

    async def _ensure_subscribed(
        self, session: AsyncSession, row: Integration, client: httpx.AsyncClient
    ) -> None:
        """A connection made before pressure (4) and activity (16) were subscribed gets them on
        its next sync; a category Withings refuses is tried again once a day, not every run."""
        data = dict(row.data or {})
        done = {int(a) for a in data.get("subscribed") or [] if isinstance(a, int)}
        missing = tuple(a for a in SUBSCRIBED_APPLIS if a not in done)
        today = self._clock.now().date().isoformat()
        if not missing or data.get("subscribe_tried") == today:
            return
        token = await self._access_token(session, row, client)
        accepted = await self._subscribe(client, token, missing)
        data["subscribed"] = sorted(done | set(accepted))
        data["subscribe_tried"] = today
        row.data = data
        await session.flush()

    # --- tokens ---------------------------------------------------------------------------
    async def _token_request(
        self, client: httpx.AsyncClient, data: Mapping[str, str]
    ) -> dict[str, Any]:
        body = await self._call(client, TOKEN_URL, data)
        inner = body.get("body")
        if not isinstance(inner, dict) or "access_token" not in inner:
            raise WithingsAuthError("token response without access_token")
        return cast("dict[str, Any]", inner)

    async def _store_tokens(
        self, session: AsyncSession, user_id: int, tokens: Mapping[str, Any], *, now: datetime
    ) -> Integration:
        try:
            expires_in = float(tokens.get("expires_in") or 10800)
        except (TypeError, ValueError):
            expires_in = 10800.0
        external = tokens.get("userid")
        return await repo.set_integration_tokens(
            session,
            self._cipher,
            user_id,
            PROVIDER,
            access_token=str(tokens["access_token"]),
            refresh_token=str(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
            expires_at=now + timedelta(seconds=expires_in),
            now=now,
            scopes=str(tokens.get("scope")) if tokens.get("scope") else None,
            external_user_id=str(external) if external is not None else None,
        )

    async def _refresh(
        self, session: AsyncSession, row: Integration, client: httpx.AsyncClient
    ) -> str:
        tokens = repo.integration_tokens(self._cipher, row)
        if tokens.refresh_token is None:
            await repo.set_integration_status(
                session, row.user_id, PROVIDER, IntegrationStatus.expired
            )
            raise WithingsAuthError("no refresh token stored")
        try:
            fresh = await self._token_request(
                client,
                {
                    "action": "requesttoken",
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": tokens.refresh_token,
                },
            )
        except WithingsTransportError:
            raise  # Withings did not answer: the refresh token may be fine, try again later
        except WithingsError:
            await repo.set_integration_status(
                session, row.user_id, PROVIDER, IntegrationStatus.expired
            )
            raise WithingsAuthError("refresh rejected") from None
        updated = await self._store_tokens(session, row.user_id, fresh, now=self._clock.now())
        log.info("withings_token_refreshed", user_id=row.user_id)
        return str(repo.integration_tokens(self._cipher, updated).access_token)

    async def _access_token(
        self, session: AsyncSession, row: Integration, client: httpx.AsyncClient
    ) -> str:
        tokens = repo.integration_tokens(self._cipher, row)
        if (
            tokens.access_token
            and tokens.expires_at is not None
            and ensure_utc(tokens.expires_at) - self._clock.now() > timedelta(seconds=60)
        ):
            return tokens.access_token
        return await self._refresh(session, row, client)

    # --- HTTP -----------------------------------------------------------------------------
    async def _call(
        self,
        client: httpx.AsyncClient,
        url: str,
        data: Mapping[str, str],
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        try:
            response = await client.post(url, data=dict(data), headers=headers)
        except httpx.HTTPError as exc:
            raise WithingsTransportError(f"POST {url} failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise WithingsTransportError(f"POST {url}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WithingsTransportError("response is not JSON") from exc
        if not isinstance(body, dict):
            raise WithingsTransportError("response is not a JSON object")
        status = int(body.get("status", -1) or 0)
        if status == 401:
            raise WithingsAuthError(f"status 401: {body.get('error')}")
        if status in _TRANSIENT_STATUSES:
            raise WithingsTransportError(f"status {status}: {body.get('error')}")
        if status != 0:
            raise WithingsError(f"status {status}: {body.get('error')}")
        return cast("dict[str, Any]", body)

    async def _getmeas(
        self,
        session: AsyncSession,
        row: Integration,
        client: httpx.AsyncClient,
        params: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """All ``measuregrps`` for the query, following ``more``/``offset``; one refresh on 401."""
        token = await self._access_token(session, row, client)
        groups: list[dict[str, Any]] = []
        offset: int | None = None
        for _ in range(40):
            data = {"action": "getmeas", "meastypes": MEASTYPES_PARAM, "category": "1", **params}
            if offset is not None:
                data["offset"] = str(offset)
            try:
                body = await self._call(client, MEASURE_URL, data, access_token=token)
            except WithingsAuthError:
                token = await self._refresh(session, row, client)
                body = await self._call(client, MEASURE_URL, data, access_token=token)
            raw_inner = body.get("body")
            inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else {}
            batch = inner.get("measuregrps")
            if isinstance(batch, list):
                groups.extend(item for item in batch if isinstance(item, dict))
            next_offset = _to_int(inner.get("offset"))
            if _to_int(inner.get("more")) and next_offset is not None:
                offset = next_offset
                continue
            break
        return groups

    async def _getactivity(
        self,
        session: AsyncSession,
        row: Integration,
        client: httpx.AsyncClient,
        params: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Every ``activities`` row for the query (``more``/``offset``); one refresh on 401."""
        token = await self._access_token(session, row, client)
        rows: list[dict[str, Any]] = []
        offset: int | None = None
        for _ in range(20):
            data = {"action": "getactivity", "data_fields": ACTIVITY_FIELDS, **params}
            if offset is not None:
                data["offset"] = str(offset)
            try:
                body = await self._call(client, MEASURE_V2_URL, data, access_token=token)
            except WithingsAuthError:
                token = await self._refresh(session, row, client)
                body = await self._call(client, MEASURE_V2_URL, data, access_token=token)
            raw_inner = body.get("body")
            inner: dict[str, Any] = raw_inner if isinstance(raw_inner, dict) else {}
            batch = inner.get("activities")
            if isinstance(batch, list):
                rows.extend(item for item in batch if isinstance(item, dict))
            next_offset = _to_int(inner.get("offset"))
            if inner.get("more") and next_offset is not None:
                offset = next_offset
                continue
            break
        return rows

    async def _activity(
        self,
        session: AsyncSession,
        user: User,
        row: Integration,
        client: httpx.AsyncClient,
        params: Mapping[str, str],
    ) -> int:
        """Pull and store daily steps and active kcal; never fails the weight sync around it
        (an account without activity data, or a scope the user unticked, is not a broken one)."""
        try:
            rows = await self._getactivity(session, row, client, params)
        except WithingsError as exc:
            log.info("withings_activity_unavailable", user_id=user.id, error=str(exc))
            return 0
        return await self._store_activity(
            session, user, decode_activities(rows, default_tz=user.timezone or "UTC")
        )

    def _activity_params(
        self, row: Any, now: datetime, since: datetime | None, tz: str
    ) -> dict[str, str]:
        if since is not None:
            return {"lastupdate": str(int(ensure_utc(since).timestamp()))}
        if row.last_sync_at is not None:
            cursor = ensure_utc(row.last_sync_at) - timedelta(hours=1)
            return {"lastupdate": str(int(cursor.timestamp()))}
        today = to_local(now, tz).date()
        return {
            "startdateymd": (today - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat(),
            "enddateymd": today.isoformat(),
        }

    async def _store_activity(
        self, session: AsyncSession, user: User, days: list[ActivityDay]
    ) -> int:
        stored = 0
        for day in days:
            if day.steps:
                await store.upsert_measurement(
                    session,
                    user.id,
                    type=MeasurementType.steps,
                    value=float(day.steps),
                    unit="steps",
                    measured_at=day.measured_at,
                    source=PROVIDER,
                    raw=day.raw,
                )
                stored += 1
            if day.active_kcal is not None:
                await store.upsert_measurement(
                    session,
                    user.id,
                    type=MeasurementType.other,
                    value=day.active_kcal,
                    unit="kcal",
                    measured_at=day.measured_at,
                    source=PROVIDER,
                    raw=day.raw,
                    note="active_kcal",
                )
        return stored

    # --- sync -----------------------------------------------------------------------------
    def _cursor_params(self, row: Any, now: datetime) -> dict[str, str]:
        """``getmeas`` window from the stored cursor: an hour before the last sync, or the
        default window for a fresh connection."""
        if row.last_sync_at is not None:
            cursor = ensure_utc(row.last_sync_at) - timedelta(hours=1)
            return {"lastupdate": str(int(cursor.timestamp()))}
        start = now - timedelta(days=DEFAULT_WINDOW_DAYS)
        return {"startdate": str(int(start.timestamp())), "enddate": str(int(now.timestamp()))}

    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        row = await repo.get_integration(session, user.id, PROVIDER)
        if row is None or row.status != IntegrationStatus.connected:
            return []
        now = self._clock.now()
        params: dict[str, str]
        if since is not None:
            params = {"lastupdate": str(int(ensure_utc(since).timestamp()))}
        else:
            params = self._cursor_params(row, now)
        activity_params = self._activity_params(row, now, since, user.timezone or "UTC")
        try:
            async with self._client_factory() as client:
                groups = await self._getmeas(session, row, client, params)
                days = await self._activity(session, user, row, client, activity_params)
                await self._ensure_subscribed(session, row, client)
        except WithingsAuthError as exc:
            log.warning("withings_sync_unauthorized", user_id=user.id, error=str(exc))
            await repo.set_integration_status(session, user.id, PROVIDER, IntegrationStatus.expired)
            return []
        except WithingsError as exc:
            log.warning("withings_sync_failed", user_id=user.id, error=str(exc))
            return []
        events = await self._store_readings(session, user, decode_groups(groups))
        await repo.set_integration_status(
            session, user.id, PROVIDER, IntegrationStatus.connected, last_sync_at=now
        )
        log.info(
            "withings_synced",
            user_id=user.id,
            groups=len(groups),
            events=len(events),
            activity_days=days,
        )
        return events

    async def _store_readings(
        self, session: AsyncSession, user: User, readings: list[Reading]
    ) -> list[Event]:
        events: list[Event] = []
        for reading in sorted(readings, key=lambda r: (r.measured_at, r.meastype)):
            note = None if reading.type != MeasurementType.other else reading.metric
            _row, created = await store.upsert_measurement(
                session,
                user.id,
                type=reading.type,
                value=reading.value,
                unit=reading.unit,
                measured_at=reading.measured_at,
                source=PROVIDER,
                raw=reading.raw,
                note=note,
            )
            if not created:
                continue  # already imported: a repeated notification must not re-fire events
            events.append(
                MeasurementEvent(
                    user_id=user.id,
                    occurred_at=reading.measured_at,
                    source=PROVIDER,
                    type=reading.metric,
                    value=reading.value,
                    unit=reading.unit,
                    measured_at=reading.measured_at,
                    raw=reading.raw,
                )
            )
        return events

    # --- webhook --------------------------------------------------------------------------
    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        if request.method.upper() == "HEAD":
            return WebhookResponse(), []  # reachability probe before subscribe
        fields = parse_notification(request.body, request.query)
        userid = fields.get("userid")
        if not userid:
            return WebhookResponse(status=400, body="missing userid"), []
        appli = fields.get("appli", str(APPLI_WEIGHT))
        if appli not in {str(code) for code in SUBSCRIBED_APPLIS}:
            return WebhookResponse(body="ignored"), []
        row = await repo.integration_by_external_user(session, PROVIDER, userid)
        if row is None or row.status != IntegrationStatus.connected:
            log.info("withings_webhook_unknown_user")
            return WebhookResponse(body="unknown user"), []
        user = await repo.get_user(session, row.user_id)
        if user is None:
            return WebhookResponse(body="unknown user"), []
        now = self._clock.now()
        last = self._webhook_fetch_at.get((user.id, appli))
        if last is not None and now - last < WEBHOOK_MIN_INTERVAL:
            log.info("withings_webhook_throttled", user_id=user.id)
            return WebhookResponse(body="throttled"), []
        self._webhook_fetch_at[(user.id, appli)] = (
            now  # per category: steps never hold up a weigh-in
        )
        # The notification's own startdate/enddate are untrusted input: fetch from our cursor.
        params = self._cursor_params(row, now)
        try:
            async with self._client_factory() as client:
                if appli == str(APPLI_ACTIVITY):
                    activity = self._activity_params(row, now, None, user.timezone or "UTC")
                    days = await self._activity(session, user, row, client, activity)
                    log.info("withings_webhook_activity", user_id=user.id, days=days)
                    return WebhookResponse(), []
                groups = await self._getmeas(session, row, client, params)
        except WithingsAuthError as exc:
            log.warning("withings_webhook_unauthorized", user_id=user.id, error=str(exc))
            await repo.set_integration_status(session, user.id, PROVIDER, IntegrationStatus.expired)
            return WebhookResponse(body="token expired"), []
        except WithingsError as exc:
            log.warning("withings_webhook_fetch_failed", user_id=user.id, error=str(exc))
            return WebhookResponse(status=502, body="upstream error"), []
        events = await self._store_readings(session, user, decode_groups(groups))
        # The cursor moves only in ``sync``: a spoofed notification must not skip a weigh-in.
        log.info("withings_webhook", user_id=user.id, groups=len(groups), events=len(events))
        return WebhookResponse(), events
