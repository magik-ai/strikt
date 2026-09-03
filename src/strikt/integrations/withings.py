"""Withings integration: OAuth2, ``getmeas`` decoding, ``notify`` subscription, webhook.

Facts from research/05-scales-apple-health.md §1 (verified on 2026-09-03):

- Authorize ``https://account.withings.com/oauth2_user/authorize2`` with comma-separated scopes
  ``user.info,user.metrics,user.activity``; the code is valid for 30 seconds.
- Token ``POST https://wbsapi.withings.net/v2/oauth2`` with ``action=requesttoken``; every
  response is HTTP 200 with a JSON ``status`` (0 = ok). Access tokens live 3 h, refresh tokens
  1 year and rotate on every refresh.
- ``POST https://wbsapi.withings.net/measure`` ``action=getmeas`` returns ``measuregrps`` whose
  measures are ``value * 10^unit``; ``more``/``offset`` paginate; ``lastupdate`` is the sync cursor.
- ``POST https://wbsapi.withings.net/notify`` ``action=subscribe&appli=1`` needs a public HTTPS
  callback that answers ``HEAD`` with 2xx. Notifications are unsigned form POSTs
  (``userid``, ``appli``, ``startdate``, ``enddate``). They are treated as a *hint only*: the
  body's window is ignored, the data is re-fetched from the stored cursor with the user's own
  token, the cursor is advanced only by the scheduled ``sync``, and a user is fetched for at most
  once per ``WEBHOOK_MIN_INTERVAL`` — so a spoofed POST (user ids are small integers) can
  neither move the cursor past an unimported weigh-in nor burn the user's API quota.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, SystemClock, ensure_utc
from strikt.db import repo
from strikt.db.models import IntegrationStatus, MeasurementType, User
from strikt.events import Event, MeasurementEvent
from strikt.integrations import store
from strikt.integrations.base import (
    ConnectInfo,
    ProviderName,
    WebhookRequest,
    WebhookResponse,
)
from strikt.integrations.oauth import callback_url, consume_state, issue_state, webhook_url
from strikt.telegram.copy import resolve_lang

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import Integration

log = structlog.get_logger(__name__)

PROVIDER: ProviderName = "withings"
AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
NOTIFY_URL = "https://wbsapi.withings.net/notify"
SCOPES = "user.info,user.metrics,user.activity"
APPLI_WEIGHT = 1
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
    76: (MeasurementType.other, "kg", "muscle_mass_kg"),
    77: (MeasurementType.other, "kg", "water_kg"),
    88: (MeasurementType.other, "kg", "bone_mass_kg"),
}
MEASTYPES_PARAM = ",".join(str(code) for code in MEASTYPES)

ClientFactory = Callable[[], httpx.AsyncClient]

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "connect": "Open the link, log in to Withings and allow access. Every weigh-in will arrive here automatically.",
        "not_configured": "Withings is not configured on this server (WITHINGS_CLIENT_ID is missing).",
        "connected": "Withings connected. Imported {count} readings from the last 30 days. Go back to Telegram.",
        "denied": "Withings access was not granted. Send “connect Withings” again when you want to retry.",
        "expired": "The Withings link expired or was already used. Ask me for a new one in Telegram.",
        "failed": "Withings did not accept the login. Ask me for a new link in Telegram.",
    },
    "ru": {
        "connect": "Открой ссылку, войди в Withings и разреши доступ. Каждое взвешивание будет приходить сюда само.",
        "not_configured": "Withings на этом сервере не настроен (нет WITHINGS_CLIENT_ID).",
        "connected": "Withings подключён. Импортировал {count} измерений за 30 дней. Возвращайся в Telegram.",
        "denied": "Доступ к Withings не выдан. Напиши «подключи Withings», когда захочешь повторить.",
        "expired": "Ссылка Withings устарела или уже использована. Попроси новую в Telegram.",
        "failed": "Withings не принял вход. Попроси новую ссылку в Telegram.",
    },
}


def _copy(lang: str | None, key: str, **kwargs: Any) -> str:
    text = _COPY[resolve_lang(lang)][key]
    return text.format(**kwargs) if kwargs else text


class WithingsError(RuntimeError):
    """A Withings call failed (network, non-zero ``status``, malformed body)."""


class WithingsAuthError(WithingsError):
    """The token is invalid and could not be refreshed."""


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
        self._webhook_fetch_at: dict[int, datetime] = {}

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
                await self._store_tokens(session, user.id, body, now=now)
                await self._subscribe(client, str(body["access_token"]))
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

    async def _subscribe(self, client: httpx.AsyncClient, access_token: str) -> bool:
        """``notify subscribe appli=1``. A failure is logged, not fatal: polling still works."""
        try:
            body = await self._call(
                client,
                NOTIFY_URL,
                {
                    "action": "subscribe",
                    "callbackurl": self.notify_url,
                    "appli": str(APPLI_WEIGHT),
                    "comment": "Strikt weight sync",
                },
                access_token=access_token,
            )
        except WithingsError as exc:
            log.warning("withings_subscribe_failed", error=str(exc), callback=self.notify_url)
            return False
        log.info("withings_subscribed", callback=self.notify_url, status=body.get("status"))
        return True

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
            raise WithingsError(f"POST {url} failed: {exc!r}") from exc
        if response.status_code >= 300:
            raise WithingsError(f"POST {url}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WithingsError("response is not JSON") from exc
        if not isinstance(body, dict):
            raise WithingsError("response is not a JSON object")
        status = int(body.get("status", -1) or 0)
        if status == 401:
            raise WithingsAuthError(f"status 401: {body.get('error')}")
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
        try:
            async with self._client_factory() as client:
                groups = await self._getmeas(session, row, client, params)
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
        log.info("withings_synced", user_id=user.id, groups=len(groups), events=len(events))
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
        if appli != str(APPLI_WEIGHT):
            return WebhookResponse(body="ignored"), []
        row = await repo.integration_by_external_user(session, PROVIDER, userid)
        if row is None or row.status != IntegrationStatus.connected:
            log.info("withings_webhook_unknown_user")
            return WebhookResponse(body="unknown user"), []
        user = await repo.get_user(session, row.user_id)
        if user is None:
            return WebhookResponse(body="unknown user"), []
        now = self._clock.now()
        last = self._webhook_fetch_at.get(user.id)
        if last is not None and now - last < WEBHOOK_MIN_INTERVAL:
            log.info("withings_webhook_throttled", user_id=user.id)
            return WebhookResponse(body="throttled"), []
        self._webhook_fetch_at[user.id] = now
        # The notification's own startdate/enddate are untrusted input: fetch from our cursor.
        params = self._cursor_params(row, now)
        try:
            async with self._client_factory() as client:
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
