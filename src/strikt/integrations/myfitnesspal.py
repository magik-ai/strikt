"""MyFitnessPal: every food line the coach logs is mirrored into the user's MFP food diary.

Why it exists: MyFitnessPal is where other apps look for nutrition (it writes to Apple Health and
Health Connect, and WHOOP, Garmin and others read it from there), so a meal logged in Strikt
should show up there without a second entry by hand.

How it talks to MyFitnessPal (facts as of 2026-11, see docs/INTEGRATIONS.md):

- There is no usable official API. The partner API (myfitnesspalapi.com) says "We are not
  accepting requests for API access at this time" and documents no food diary writes at all.
- The website's own session is the only working route, the same one open-source clients use
  (python-myfitnesspal, myfitnesspal-mcp-python): the browser's login cookies are traded for a
  bearer token at ``GET www.myfitnesspal.com/user/auth_token?refresh=true``; the diary is the v2
  API at ``api.myfitnesspal.com/v2/diary`` (``GET ?entry_date=&types=food_entry``, ``POST
  {"items": [food_entry]}``, ``DELETE /v2/diary/{id}``) with ``Authorization: Bearer``,
  ``mfp-client-id: mfp-main-js`` and ``mfp-user-id``; private custom foods are created on the
  website's backend (``GET /api/auth/csrf`` then ``POST /api/services/foods`` with
  ``x-csrf-token``), and ``GET /v2/foods?ids=`` returns the food's ``version`` and serving that a
  diary entry must name. This is undocumented: MyFitnessPal can change it without notice, and the
  code says so in the log instead of guessing.
- ``country_code`` is left out when a food is created: MFP then reads ``carbohydrates`` as the
  total and stores exactly the number sent, so MFP shows the same carbs the coach logged.

The mirror, per coaching day (the same date ``set_day_food`` files food under):

- each line of the day's list becomes one diary entry in Breakfast / Lunch / Dinner / Snacks,
  pointing at a private custom food named after the line ("творог 0.5% (200 г)") whose one
  serving carries the line's numbers exactly (law: the number in the log is the number said);
  identical lines reuse one food;
- what Strikt created is recorded in ``integration_links``; only those entries are ever deleted,
  so food the user logs in MFP directly is left alone;
- a change is mirrored 20 seconds after ``DayStateChanged`` (debounced), and every 30 minutes
  the scheduler's sync compares the last 14 days with the links and fixes any day that drifted;
  for today and yesterday it also reads the MFP diary back, so an entry deleted there by hand
  is added again (the coach's list is the source of truth);
- on connect the last 6 days and today are copied in the background; a past day that already
  holds food in MFP is skipped, and the user is told which ones.

Credentials: the cookies are Fernet-encrypted in ``integrations.refresh_token_enc`` (they are
the long-lived login), the bearer token in ``access_token_enc``. They arrive through our signed
connect page or a chat message that is deleted at once; they never reach the model and are
never logged. Every MFP call runs on the user's own session.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import Clock, SystemClock, coaching_today, ensure_utc, to_local
from strikt.db import repo
from strikt.db.models import IntegrationStatus, LinkKind, Meal, MealItem, MealSlot, User
from strikt.events import DayStateChanged, Event
from strikt.integrations import store
from strikt.integrations.base import (
    ConnectInfo,
    ProviderName,
    WebhookRequest,
    WebhookResponse,
    link_copy,
)
from strikt.integrations.oauth import connect_page_url
from strikt.telegram.copy import t

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.db.crypto import TokenCipher
    from strikt.db.models import Integration, IntegrationLink

log = structlog.get_logger(__name__)

PROVIDER: ProviderName = "myfitnesspal"
WEB = "https://www.myfitnesspal.com"
API = "https://api.myfitnesspal.com"
AUTH_TOKEN_URL = f"{WEB}/user/auth_token"
SESSION_URL = f"{WEB}/api/auth/session"
CSRF_URL = f"{WEB}/api/auth/csrf"
FOODS_URL = f"{WEB}/api/services/foods"
DIARY_URL = f"{API}/v2/diary"
FOODS_API_URL = f"{API}/v2/foods"
CLIENT_ID = "mfp-main-js"
MEALS: tuple[str, ...] = ("Breakfast", "Lunch", "Dinner", "Snacks")
#: MFP's web client presents itself as a browser; a bare Python agent is more likely to be stopped
#: by the WAF in front of the site.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
#: At least one of these must be in what the user pastes: without them there is no login.
LOGIN_COOKIES: tuple[str, ...] = (
    "_mfp_session",
    "remember_me",
    "__Secure-next-auth.session-token",
)
#: Past days copied on connect (today comes on top).
BACKFILL_DAYS = 6
#: How far back the 30-minute reconcile looks (``set_day_food`` rewrites up to 14 days back).
RECONCILE_DAYS = 14
#: The session is used at least this often so MFP keeps renewing it.
KEEPALIVE = timedelta(hours=6)
#: A change is mirrored this long after the last ``DayStateChanged`` of that day.
MIRROR_DELAY_S = 20.0
MAX_COOKIE_BYTES = 16_384
FOOD_BRAND = "Strikt"
MAX_FOOD_NAME = 120

ClientFactory = Callable[[], httpx.AsyncClient]
SessionFactory = Callable[[], Any]
Notify = Callable[[User, str], Awaitable[None]]

#: A pasted cookie set, in any of the shapes a browser hands out.
_LOGIN_COOKIE_RE = re.compile(
    r"(?:_mfp_session|remember_me|next-auth\.session-token)\s*=\s*[^;\s'\"]{8,}"
)
#: A request to MyFitnessPal copied from the browser (a cURL, a fetch, a header block) carries the
#: cookies even when the login cookie itself landed in another message.
_COOKIE_PAIRS_RE = re.compile(
    r"""(?i)(?:\bcookie\s*:\s*|(?:\s-b|--cookie)\s+['"^]*)[A-Za-z0-9_.\-]+=[^;\s'"]+"""
)
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
_CURL_HEADER = re.compile(r"""(?is)(?:-H|--header)\s+(['"])\s*cookie\s*:\s*(.*?)\1""")
_CURL_COOKIE = re.compile(r"""(?is)(?:\s-b|--cookie)\s+(['"])(.*?)\1""")
_FETCH_COOKIE = re.compile(r"""(?is)["']cookie["']\s*:\s*(["'])(.*?)\1""")
_HEADER_LINE = re.compile(r"(?im)^\s*cookie\s*:\s*(.+?)\s*$")


# ----------------------------------------------------------------------------------- errors


class MfpError(RuntimeError):
    """A MyFitnessPal call failed (network, 5xx, an answer we do not understand)."""


class MfpAuthError(MfpError):
    """The session is gone (logged out, expired): the user has to paste fresh cookies."""


class MfpBlockedError(MfpError):
    """The firewall in front of MyFitnessPal answered instead of MyFitnessPal itself."""


class MfpEntryRejectedError(MfpError):
    """MFP refused a diary entry (usually a food version that moved or a deleted food)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------- cookies


def looks_like_mfp_cookies(text: str | None) -> bool:
    """A chat message that carries (part of) an MFP login: a cookie header, a cURL, a cookie
    export, or a piece of a long cURL that Telegram split. Such a message is a secret: it is
    deleted and never becomes a turn, so it must never reach the model."""
    if not text:
        return False
    if _LOGIN_COOKIE_RE.search(text) is not None:
        return True
    if "myfitnesspal.com" in text.lower() and _COOKIE_PAIRS_RE.search(text) is not None:
        return True
    return has_login(parse_cookie_input(text))


def _clean(value: str) -> str | None:
    """A cookie value as RFC 6265 allows it, or None (a smart quote or a non-breaking space
    from a copy-paste would otherwise make httpx raise instead of MFP saying "invalid")."""
    value = value.strip().strip("\"\u201c\u201d'")
    if not value or any(not (33 <= ord(c) <= 126) or c in '",;\\' for c in value):
        return None
    return value


def _pairs(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, raw_value = part.strip().partition("=")
        name = name.strip()
        value = _clean(raw_value)
        if sep and name and value and _COOKIE_NAME.match(name):
            cookies[name] = value
    return cookies


def parse_cookie_input(text: str | None) -> dict[str, str]:
    """The cookies in whatever the user pasted: the ``cookie`` header value, a whole request
    header block, "Copy as cURL" (bash or Windows cmd), "Copy as fetch", or a cookie-extension
    JSON export. Returns ``{}`` when nothing cookie-shaped is found."""
    raw = (text or "").strip()
    if not raw:
        return {}
    if "^" in raw and "curl" in raw[:20].lower():
        raw = re.sub(r"\^(.)", r"\1", raw)  # Chrome's "Copy as cURL (cmd)" escapes with ^
    if raw[:1] in "[{":
        with contextlib.suppress(ValueError):
            data: Any = json.loads(raw)
            if isinstance(data, dict) and isinstance(data.get("cookies"), list):
                data = data["cookies"]
            if isinstance(data, list):
                exported: dict[str, str] = {}
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    name, value = item.get("name"), item.get("value")
                    if not isinstance(name, str) or not isinstance(value, str):
                        continue
                    clean = _clean(value)
                    if clean and _COOKIE_NAME.match(name):
                        exported[name] = clean
                return exported
    for pattern in (_CURL_HEADER, _CURL_COOKIE, _FETCH_COOKIE):
        match = pattern.search(raw)
        if match is not None:
            return _pairs(match.group(2))
    line = _HEADER_LINE.search(raw)
    if line is not None:
        return _pairs(line.group(1))
    return _pairs(" ".join(raw.split()))


def has_login(cookies: Mapping[str, str]) -> bool:
    """At least one login cookie with a value long enough to be one (``remember_me=1`` is not)."""
    return any(len(cookies.get(name) or "") >= 8 for name in LOGIN_COOKIES)


def _cookie_header(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


# ------------------------------------------------------------------------------- the mirror


def meal_name(slot: MealSlot | str, eaten_local: datetime | None) -> str:
    """Strikt's slot → MFP's meal. An unknown slot goes by the time it was eaten."""
    value = str(slot)
    if value == MealSlot.breakfast:
        return "Breakfast"
    if value == MealSlot.lunch:
        return "Lunch"
    if value == MealSlot.dinner:
        return "Dinner"
    if value == MealSlot.unknown and eaten_local is not None:
        if eaten_local.hour < 11:
            return "Breakfast"
        if eaten_local.hour < 16:
            return "Lunch"
        if eaten_local.hour >= 18:
            return "Dinner"
    return "Snacks"


def _r(value: float | None) -> float:
    return round(float(value or 0.0), 1)


@dataclass(frozen=True, kw_only=True)
class DesiredEntry:
    """One line of the coach's list as it should stand in the MFP diary."""

    meal: str
    name: str
    portion: str | None
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    fiber_g: float
    sodium_mg: float | None = None

    @property
    def description(self) -> str:
        name = " ".join(self.name.split())
        text = f"{name} ({' '.join(self.portion.split())})" if self.portion else name
        return text[:MAX_FOOD_NAME]

    @property
    def food_key(self) -> str:
        """The custom food: the same line (full name, portion, every number) is the same food."""
        name = " ".join(self.name.split()).casefold()
        portion = " ".join((self.portion or "").split()).casefold()
        numbers = (self.kcal, self.protein_g, self.carbs_g, self.fat_g, self.fiber_g)
        parts = [
            name,
            portion,
            *(f"{v:g}" for v in numbers),
            "" if self.sodium_mg is None else f"{self.sodium_mg:g}",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]

    @property
    def key(self) -> str:
        return f"{self.meal}:{self.food_key}"

    def nutrition(self) -> dict[str, Any]:
        contents: dict[str, Any] = {
            "energy": {"unit": "calories", "value": self.kcal},
            "grams": 1,
            "protein": self.protein_g,
            "carbohydrates": self.carbs_g,
            "fat": self.fat_g,
            "fiber": self.fiber_g,
        }
        if self.sodium_mg is not None:
            contents["sodium"] = self.sodium_mg
        return contents


def desired_from_rows(rows: list[tuple[Meal, MealItem]], tz: str) -> list[DesiredEntry]:
    out: list[DesiredEntry] = []
    for meal, item in rows:
        eaten = meal.eaten_at or meal.logged_at
        local = to_local(ensure_utc(eaten), tz) if eaten is not None else None
        out.append(_desired(item, meal_name(meal.slot, local)))
    return out


async def day_food(session: AsyncSession, user_id: int, day: date) -> list[tuple[Meal, MealItem]]:
    """The day's lines straight from the tables (never a collection cached in the session)."""
    result = await session.execute(
        select(Meal, MealItem)
        .join(MealItem, MealItem.meal_id == Meal.id)
        .where(Meal.user_id == user_id, Meal.day_date == day, Meal.deleted_at.is_(None))
        .order_by(Meal.logged_at, Meal.id, MealItem.position, MealItem.id)
        .execution_options(populate_existing=True)
    )
    return [(meal, item) for meal, item in result.tuples().all()]


def _desired(item: MealItem, meal: str) -> DesiredEntry:
    return DesiredEntry(
        meal=meal,
        name=item.name,
        portion=item.unit,
        kcal=_r(item.kcal),
        protein_g=_r(item.protein_g),
        carbs_g=_r(item.carbs_g),
        fat_g=_r(item.fat_g),
        fiber_g=_r(item.fiber_g),
        sodium_mg=None if item.sodium_mg is None else _r(item.sodium_mg),
    )


# --------------------------------------------------------------------------------- the client


@dataclass
class MfpApi:
    """One user's MFP session over one ``httpx.AsyncClient``. Cookies rotate as MFP re-issues
    them; ``changed`` tells the caller to store the new set and token."""

    http: httpx.AsyncClient
    cookies: dict[str, str]
    clock: Clock
    access_token: str | None = None
    user_id: str | None = None
    expires_at: datetime | None = None
    changed: bool = False

    # --- plumbing -------------------------------------------------------------------------
    def _web_headers(self, *, json_body: bool = False, csrf: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{WEB}/",
            "Cookie": _cookie_header(self.cookies),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["x-csrf-token"] = csrf
        return headers

    def _api_headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "mfp-client-id": CLIENT_ID,
            "mfp-user-id": str(self.user_id or ""),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _absorb(self, response: httpx.Response) -> None:
        """Keep every cookie MFP re-issues (``_mfp_session`` rotates on each call)."""
        for cookie in response.cookies.jar:
            if cookie.value and self.cookies.get(cookie.name) != cookie.value:
                self.cookies[cookie.name] = cookie.value
                self.changed = True

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MfpError(
                f"{method} {url.split('?', maxsplit=1)[0]} failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _blocked(response: httpx.Response) -> bool:
        if response.status_code not in (403, 429, 503):
            return False
        server = response.headers.get("server", "").lower()
        return "cloudflare" in server or "cf-ray" in response.headers

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MfpError(f"{what}: not JSON (HTTP {response.status_code})") from exc

    # --- session --------------------------------------------------------------------------
    async def authenticate(self) -> None:
        """Trade the cookies for the v2 bearer token. ``MfpAuthError`` when MFP says the
        session is gone (a redirect to the login page, a 401, an HTML page instead of JSON)."""
        response = await self._send(
            "GET", AUTH_TOKEN_URL, params={"refresh": "true"}, headers=self._web_headers()
        )
        self._absorb(response)
        if self._blocked(response):
            raise MfpBlockedError(
                f"auth_token: blocked by the firewall (HTTP {response.status_code})"
            )
        if response.is_redirect or response.status_code in (401, 403):
            raise MfpAuthError(f"auth_token: HTTP {response.status_code}")
        if response.status_code >= 300:
            raise MfpError(f"auth_token: HTTP {response.status_code}")
        if not response.headers.get("content-type", "").startswith("application/json"):
            raise MfpAuthError("auth_token: not JSON, the session is logged out")
        body = self._json(response, "auth_token")
        if not isinstance(body, dict) or not body.get("access_token") or not body.get("user_id"):
            raise MfpAuthError("auth_token: no token in the answer")
        self.access_token = str(body["access_token"])
        self.user_id = str(body["user_id"])
        try:
            expires_in = float(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self.expires_at = self.clock.now() + timedelta(seconds=max(expires_in, 300.0))
        self.changed = True

    async def keepalive(self) -> None:
        """Touch the NextAuth session so MFP renews it (best effort; failures are logged)."""
        try:
            response = await self._send("GET", SESSION_URL, headers=self._web_headers())
        except MfpError as exc:
            log.info("mfp_keepalive_failed", error=str(exc))
            return
        self._absorb(response)

    async def _api(
        self, method: str, url: str, *, retry: bool = True, **kwargs: Any
    ) -> httpx.Response:
        """A v2 API call; a 401 re-authenticates once and repeats the call."""
        if not self.access_token:
            await self.authenticate()
        json_body = "content" in kwargs
        response = await self._send(
            method, url, headers=self._api_headers(json_body=json_body), **kwargs
        )
        if response.status_code == 401 and retry:
            await self.authenticate()
            return await self._api(method, url, retry=False, **kwargs)
        if response.status_code == 401:
            raise MfpAuthError(f"{method} {url}: HTTP 401")
        if self._blocked(response):
            raise MfpBlockedError(f"{method} {url}: blocked by the firewall")
        return response

    # --- diary ----------------------------------------------------------------------------
    async def diary(self, day: date) -> list[dict[str, Any]]:
        """The day's food entries (every one, not only ours)."""
        response = await self._api(
            "GET", DIARY_URL, params={"entry_date": day.isoformat(), "types": "food_entry"}
        )
        if response.status_code != 200:
            raise MfpError(f"diary {day}: HTTP {response.status_code}")
        body = self._json(response, "diary")
        items = body.get("items") if isinstance(body, dict) else None
        return [
            item
            for item in (items or [])
            if isinstance(item, dict)
            and item.get("type") == "food_entry"
            and not item.get("deleted")
            and item.get("id")
        ]

    async def add_entry(
        self, day: date, meal: str, food_id: str, version: str, serving: Mapping[str, Any]
    ) -> str:
        entry = {
            "type": "food_entry",
            "date": day.isoformat(),
            "meal_name": meal,
            "servings": 1.0,
            "food": {"id": food_id, "version": version},
            "serving_size": dict(serving),
        }
        response = await self._api("POST", DIARY_URL, content=json.dumps({"items": [entry]}))
        if response.status_code not in (200, 201):
            raise MfpEntryRejectedError(
                f"add entry: HTTP {response.status_code}", response.status_code
            )
        body = self._json(response, "add entry")
        try:
            return str(body["items"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise MfpError("add entry: no id in the answer") from exc

    async def delete_entry(self, entry_id: str) -> None:
        response = await self._api("DELETE", f"{DIARY_URL}/{entry_id}")
        if response.status_code in (200, 202, 204, 404, 410):
            return  # gone already is gone
        raise MfpError(f"delete entry: HTTP {response.status_code}")

    # --- foods ----------------------------------------------------------------------------
    async def create_food(self, entry: DesiredEntry) -> str:
        """A private custom food whose one serving is exactly this line."""
        response = await self._send("GET", CSRF_URL, headers=self._web_headers())
        self._absorb(response)
        body = self._json(response, "csrf") if response.status_code == 200 else None
        if not isinstance(body, dict) or not body.get("csrfToken"):
            # a hiccup, not a logout: without the token the POST would get a 403 that looks
            # exactly like a dead session
            raise MfpError(f"csrf: no token (HTTP {response.status_code})")
        csrf = str(body["csrfToken"])
        item = {
            "description": entry.description,
            "brand_name": FOOD_BRAND,
            "public": False,
            "type": "food",
            "nutritional_contents": entry.nutrition(),
            "serving_sizes": [
                {
                    "value": 1,
                    "unit": "serving",
                    "nutrition_multiplier": 1,
                    "gram_weight": 1,
                    "fraction": False,
                    "index": 0,
                },
                {
                    "value": 1,
                    "unit": "container (1 serving ea.)",
                    "nutrition_multiplier": 1,
                    "gram_weight": 1,
                    "fraction": False,
                    "index": 1,
                },
            ],
        }
        response = await self._send(
            "POST",
            FOODS_URL,
            headers=self._web_headers(json_body=True, csrf=csrf),
            content=json.dumps({"item": item}),
        )
        self._absorb(response)
        if self._blocked(response):
            raise MfpBlockedError("create food: blocked by the firewall")
        if response.status_code == 401:
            raise MfpAuthError("create food: HTTP 401")
        if response.status_code not in (200, 201):
            # 403 included: with a fresh CSRF token it is a refusal, not proof of a logout (a
            # dead session fails at auth_token, which the next run calls anyway)
            raise MfpError(f"create food: HTTP {response.status_code}")
        body = self._json(response, "create food")
        food: Any = body[0] if isinstance(body, list) and body else body
        if isinstance(food, dict) and isinstance(food.get("item"), dict):
            food = food["item"]
        if not isinstance(food, dict) or not food.get("id"):
            raise MfpError("create food: no id in the answer")
        return str(food["id"])

    async def food(self, food_id: str) -> dict[str, Any] | None:
        """The food's current ``version`` and first serving, or None when it is gone."""
        response = await self._api("GET", FOODS_API_URL, params={"ids": food_id})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MfpError(f"food {food_id}: HTTP {response.status_code}")
        body = self._json(response, "food")
        items = body.get("items") if isinstance(body, dict) else None
        if not items or not isinstance(items[0], dict):
            return None
        food = items[0]
        servings = [s for s in food.get("serving_sizes") or [] if isinstance(s, dict)]
        if not servings or food.get("version") is None:
            raise MfpError(f"food {food_id}: no version or serving")
        first = servings[0]
        return {
            "version": str(food["version"]),
            "serving": {
                "value": first.get("value", 1),
                "unit": first.get("unit", "serving"),
                "nutrition_multiplier": first.get("nutrition_multiplier", 1),
            },
        }


# ------------------------------------------------------------------------------ the results


@dataclass(frozen=True, kw_only=True)
class MirrorResult:
    status: Literal["mirrored", "unchanged", "skipped", "not_connected", "before_start"]
    added: int = 0
    removed: int = 0


@dataclass(frozen=True, kw_only=True)
class ConnectOutcome:
    status: Literal["connected", "unchecked", "invalid"]
    message: str
    days: list[date] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class ConnectForm:
    title: str
    steps: str
    button: str


# ----------------------------------------------------------------------------- integration


class MyFitnessPalIntegration:
    provider: ProviderName = PROVIDER

    def __init__(
        self,
        settings: Settings,
        *,
        cipher: TokenCipher,
        clock: Clock | None = None,
        client_factory: ClientFactory | None = None,
        session_factory: SessionFactory | None = None,
        timeout_s: float = 15.0,
        mirror_delay_s: float = MIRROR_DELAY_S,
    ) -> None:
        self._settings = settings
        self._cipher = cipher
        self._clock: Clock = clock or SystemClock()
        self._client_factory: ClientFactory = client_factory or (
            lambda: httpx.AsyncClient(timeout=httpx.Timeout(timeout_s), follow_redirects=False)
        )
        self._sessions = session_factory
        self._delay = mirror_delay_s
        self._locks: dict[int, asyncio.Lock] = {}
        self._dirty: dict[tuple[int, date], bool] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._sleeping: set[asyncio.Task[Any]] = set()
        #: Tells the user something in Telegram (set by the app: ``Integrations.set_notifier``).
        self.notify: Notify | None = None

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    # --- connect --------------------------------------------------------------------------
    async def connect(self, session: AsyncSession, user: User) -> ConnectInfo:
        return ConnectInfo(
            provider=PROVIDER,
            kind="form",
            url=connect_page_url(self._settings, PROVIDER, user.id, now=self._clock.now()),
            instructions=t(user.language, "mfp.connect"),
        )

    async def link_used(self, session: AsyncSession, user_id: int, issued: int) -> bool:
        """A connect link issued before the last successful connect no longer works."""
        row = await store.fresh_integration(session, user_id, PROVIDER)
        linked_at = (row.data or {}).get("linked_at") if row is not None else None
        return isinstance(linked_at, int) and issued <= linked_at

    def connect_form(self, lang: str | None) -> ConnectForm:
        return ConnectForm(
            title=t(lang, "mfp.page.title"),
            steps=t(lang, "mfp.page.steps"),
            button=t(lang, "mfp.page.button"),
        )

    async def handle_callback(
        self, session: AsyncSession, query: dict[str, str]
    ) -> tuple[User | None, str]:
        return None, link_copy(None, "failed", PROVIDER)  # no OAuth: the connect page is a form

    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        return WebhookResponse(status=404, body="no webhooks"), []

    async def submit_cookies(self, session: AsyncSession, user: User, text: str) -> ConnectOutcome:
        """Check a pasted login, store it encrypted and start copying the last week."""
        lang = user.language
        cookies = parse_cookie_input(text)
        if not has_login(cookies) or len(_cookie_header(cookies)) > MAX_COOKIE_BYTES:
            log.info("mfp_connect_invalid", user_id=user.id, reason="no_login_cookie")
            return ConnectOutcome(status="invalid", message=t(lang, "mfp.invalid"))
        unchecked = False
        async with self._client_factory() as http:
            api = MfpApi(http=http, cookies=dict(cookies), clock=self._clock)
            try:
                await api.authenticate()
                await api.keepalive()
            except MfpAuthError as exc:
                log.info("mfp_connect_invalid", user_id=user.id, reason=str(exc))
                return ConnectOutcome(status="invalid", message=t(lang, "mfp.invalid"))
            except MfpError as exc:
                log.warning("mfp_connect_unchecked", user_id=user.id, error=str(exc))
                unchecked = True
        today = await self._today(session, user)
        async with self._lock(user.id):  # never interleaved with a mirror's cookie write
            before = await store.fresh_integration(session, user.id, PROVIDER)
            previous = before.external_user_id if before is not None else None
            if previous and api.user_id and previous != api.user_id:
                # another MFP account: what Strikt made lives in the old one
                dropped = await store.delete_links(session, user.id, PROVIDER)
                log.info("mfp_account_changed", user_id=user.id, links_dropped=dropped)
                data: dict[str, Any] = {}
            else:
                data = dict(before.data or {}) if before is not None else {}
            now = self._clock.now()
            row = await self._store(session, user.id, api, now=now)
            start = today - timedelta(days=BACKFILL_DAYS)
            known = _parse_day(data.get("mirror_from"))
            data["mirror_from"] = min(known, start).isoformat() if known else start.isoformat()
            # days before this connect that already hold food in MFP are the user's own (logged
            # by hand while the connection was down, say): the backfill leaves them alone
            data["connected_on"] = today.isoformat()
            data["linked_at"] = int(now.timestamp())
            data.pop("keepalive_at", None)
            row.data = data
            await session.commit()
        days = [today - timedelta(days=offset) for offset in range(BACKFILL_DAYS, -1, -1)]
        self._spawn(self._backfill(user.id, days))
        log.info(
            "mfp_connected", user_id=user.id, checked=not unchecked, mfp_user=bool(api.user_id)
        )
        lines = [t(lang, "mfp.connected", count=BACKFILL_DAYS)]
        if unchecked:
            lines.append(t(lang, "mfp.unchecked"))
        return ConnectOutcome(
            status="unchecked" if unchecked else "connected", message="\n".join(lines), days=days
        )

    async def _store(
        self, session: AsyncSession, user_id: int, api: MfpApi, *, now: datetime
    ) -> Integration:
        return await repo.set_integration_tokens(
            session,
            self._cipher,
            user_id,
            PROVIDER,
            access_token=api.access_token,
            refresh_token=json.dumps(api.cookies, separators=(",", ":")),
            expires_at=api.expires_at,
            now=now,
            external_user_id=api.user_id,
        )

    async def _save_rotated(self, session: AsyncSession, user_id: int, api: MfpApi) -> None:
        """Store the cookies and token MFP re-issued, on the existing row only and only while it
        is connected: a mirror that was mid-flight when the user disconnected must not write
        the login back (an upsert here re-created the row they had just removed)."""
        values: dict[str, Any] = {
            "access_token_enc": self._cipher.encrypt_optional(api.access_token),
            "refresh_token_enc": self._cipher.encrypt_optional(
                json.dumps(api.cookies, separators=(",", ":"))
            ),
            "expires_at": api.expires_at,
        }
        if api.user_id:
            values["external_user_id"] = api.user_id
        await store.update_connected(session, user_id, PROVIDER, values)

    async def disconnect(self, session: AsyncSession, user_id: int) -> bool:
        """ "Disconnect MyFitnessPal": the login is deleted, the row stays as ``revoked`` (its
        record of what Strikt made and of used connect links survives a reconnect). Under the
        user's lock and committed at once, so no running mirror can write the login back."""
        async with self._lock(user_id):
            row = await store.fresh_integration(session, user_id, PROVIDER)
            if row is None:
                return False
            row.access_token_enc = None
            row.refresh_token_enc = None
            row.expires_at = None
            row.status = IntegrationStatus.revoked
            await session.commit()
        log.info("mfp_disconnected", user_id=user_id)
        return True

    async def _today(self, session: AsyncSession, user: User) -> date:
        profile = await repo.get_profile(session, user.id)
        return coaching_today(
            self._clock,
            user.timezone or "UTC",
            profile.bed_time if profile else None,
            profile.wake_time if profile else None,
        )

    # --- background work ------------------------------------------------------------------
    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Shutdown: cancel the mirrors still waiting out their delay (the 30-minute sync redoes
        them) and give the ones already talking to MFP a few seconds to finish. One cut short
        anyway is safe: the next run adopts an entry whose answer it never read."""
        for task in list(self._tasks):
            if task in self._sleeping:
                task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*list(self._tasks), return_exceptions=True), timeout=5.0
            )

    async def drain(self) -> None:
        """Wait for every scheduled mirror (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def on_day_changed(self, event: DayStateChanged) -> None:
        """``DayStateChanged`` from the turn loop: mirror that day a little later, once."""
        if self._sessions is None:
            return
        key = (event.user_id, event.date)
        if key in self._dirty:
            self._dirty[key] = True  # the running task goes round once more
            return
        self._dirty[key] = False
        self._spawn(self._debounced(key))

    async def _debounced(self, key: tuple[int, date]) -> None:
        task = asyncio.current_task()
        try:
            while True:
                if task is not None:
                    self._sleeping.add(task)
                try:
                    await asyncio.sleep(self._delay)
                finally:
                    if task is not None:
                        self._sleeping.discard(task)
                self._dirty[key] = False
                await self._mirror_in_own_session(*key)
                if not self._dirty.get(key):
                    break
        finally:
            self._dirty.pop(key, None)

    async def _mirror_in_own_session(self, user_id: int, day: date) -> MirrorResult | None:
        if self._sessions is None:
            return None
        async with self._sessions() as session:
            user = await repo.get_user(session, user_id)
            if user is None:
                return None
            try:
                return await self.mirror_day(session, user, day)
            except MfpAuthError:
                await self._mark_expired(session, user_id)
            except MfpError as exc:
                log.warning(
                    "mfp_mirror_failed", user_id=user_id, day=day.isoformat(), error=str(exc)
                )
            except Exception:
                log.exception("mfp_mirror_crashed", user_id=user_id, day=day.isoformat())
            await session.rollback()
            return None

    async def _backfill(self, user_id: int, days: list[date]) -> None:
        skipped: list[date] = []
        for day in days:
            result = await self._mirror_in_own_session(user_id, day)
            if result is not None and result.status == "skipped":
                skipped.append(day)
        if not skipped or self.notify is None or self._sessions is None:
            return
        async with self._sessions() as session:
            user = await repo.get_user(session, user_id)
        if user is None:
            return
        dates = ", ".join(f"{day:%d.%m}" for day in skipped)
        try:
            await self.notify(user, t(user.language, "mfp.skipped", dates=dates))
        except Exception as exc:
            log.warning("mfp_skip_notice_failed", user_id=user_id, error=repr(exc))

    async def _mark_expired(self, session: AsyncSession, user_id: int) -> None:
        """Connected → expired (a revoked row stays revoked: the user disconnected it)."""
        await session.rollback()
        if await store.update_connected(
            session, user_id, PROVIDER, {"status": IntegrationStatus.expired}
        ):
            log.warning("mfp_session_expired", user_id=user_id)
        await session.commit()

    # --- sync (the scheduler's 30-minute job) ---------------------------------------------
    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        """Reconcile the last 14 days and keep the session warm. Publishes no events."""
        user_id = user.id
        row = await repo.get_integration(session, user_id, PROVIDER)
        if row is None or row.status != IntegrationStatus.connected:
            return []
        today = await self._today(session, user)
        start = max(_mirror_from(row, today), today - timedelta(days=RECONCILE_DAYS - 1))
        day = start
        try:
            while day <= today:
                # today and yesterday are read back even when nothing changed here, so an entry
                # removed in MFP by hand is put back (the coach's list is the source of truth)
                try:
                    await self.mirror_day(
                        session, user, day, verify=day >= today - timedelta(days=1)
                    )
                except MfpAuthError:
                    raise
                except MfpError as exc:  # one bad day must not hold up the others
                    # an HTTP failure, not a database one: what was flushed is still true (a
                    # vanished entry's link removed), so no rollback that would expire ``user``
                    log.warning(
                        "mfp_sync_failed", user_id=user_id, day=day.isoformat(), error=str(exc)
                    )
                day += timedelta(days=1)
            await self._keepalive(session, user)
        except MfpAuthError:
            await self._mark_expired(session, user_id)
        except MfpError as exc:
            log.warning("mfp_keepalive_failed", user_id=user_id, error=str(exc))
        return []

    async def _keepalive(self, session: AsyncSession, user: User) -> None:
        """Every ``KEEPALIVE`` hours trade the cookies again and touch the NextAuth session, so
        MFP keeps re-issuing them; the bearer token alone would let the login quietly lapse."""
        now = self._clock.now()
        async with self._lock(user.id):
            row = await store.fresh_integration(session, user.id, PROVIDER)
            if row is None or row.status != IntegrationStatus.connected:
                return
            data = dict(row.data or {})
            last = _parse_instant(data.get("keepalive_at"))
            if last is not None and now - last < KEEPALIVE:
                return
            async with self._client_factory() as http:
                api = self._api_for(row, http)
                await api.authenticate()
                await api.keepalive()
            await self._save_rotated(session, user.id, api)
            row.data = {**data, "keepalive_at": now.isoformat()}
            await session.commit()

    def _api_for(self, row: Integration, http: httpx.AsyncClient) -> MfpApi:
        tokens = repo.integration_tokens(self._cipher, row)
        try:
            raw = json.loads(tokens.refresh_token or "{}")
        except ValueError:
            raw = {}
        cookies = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        fresh = (
            tokens.access_token is not None
            and tokens.expires_at is not None
            and ensure_utc(tokens.expires_at) - self._clock.now() > timedelta(seconds=60)
            and row.external_user_id is not None
        )
        return MfpApi(
            http=http,
            cookies=cookies,
            clock=self._clock,
            access_token=tokens.access_token if fresh else None,
            user_id=row.external_user_id,
            expires_at=tokens.expires_at if fresh else None,
        )

    # --- mirroring one day ----------------------------------------------------------------
    async def mirror_day(
        self, session: AsyncSession, user: User, day: date, *, verify: bool = False
    ) -> MirrorResult:
        """Make the MFP diary of ``day`` hold exactly the coach's list (for what Strikt wrote).

        Without ``verify`` a day whose list still matches what was written is left alone with no
        call at all; ``verify`` reads the diary anyway, so an entry deleted in MFP comes back.

        Every entry is known by its MFP id, never guessed from its food: a write is recorded as
        *pending* before the POST and gets its id after it, so an answer that never came back
        (MFP stored it, the reply timed out) is matched to that pending row on the next run and
        never added twice; an entry of ours missing from a listing is kept as a *ghost* and
        recognised by its id if it shows up again. Entries the user logs in MFP, Strikt foods
        included, are never touched. Commits after every write on MFP's side. Raises
        ``MfpAuthError`` when the session is gone and ``MfpError`` on anything else.
        """
        async with self._lock(user.id):
            row = await store.fresh_integration(session, user.id, PROVIDER)
            if row is None or row.status != IntegrationStatus.connected:
                return MirrorResult(status="not_connected")
            if day < _mirror_from(row, day):
                return MirrorResult(status="before_start")
            data = dict(row.data or {})
            if day.isoformat() in (data.get("skipped") or []):
                return MirrorResult(status="skipped")
            desired = desired_from_rows(
                await day_food(session, user.id, day), user.timezone or "UTC"
            )
            links = await store.list_links(session, user.id, PROVIDER, LinkKind.entry, day=day)
            live = [link for link in links if _state(link) == "live"]
            pending = [link for link in links if _state(link) == "pending"]
            same = Counter(d.key for d in desired) == Counter(link.key for link in live)
            if same and not pending and not (verify and live):
                return MirrorResult(status="unchanged")
            async with self._client_factory() as http:
                api = self._api_for(row, http)
                try:
                    if await self._account_changed(session, user.id, row, api):
                        links = []
                    result = await self._apply(session, user, row, api, day, desired, links)
                finally:
                    if api.changed:
                        await self._save_rotated(session, user.id, api)
                        await session.commit()
            # only the timestamp, and only on a row still connected: never a status flip back
            await store.update_connected(
                session, user.id, PROVIDER, {"last_sync_at": self._clock.now()}
            )
            await session.commit()
            log.info(
                "mfp_day_mirrored",
                user_id=user.id,
                day=day.isoformat(),
                status=result.status,
                added=result.added,
                removed=result.removed,
            )
            return result

    async def _account_changed(
        self, session: AsyncSession, user_id: int, row: Integration, api: MfpApi
    ) -> bool:
        """The session now answers for another MFP account (a reconnect that could not be
        checked at paste time): what Strikt made lives in the old one, so forget it."""
        known = row.external_user_id
        if api.access_token is None:
            await api.authenticate()
        if known is None or api.user_id is None or api.user_id == known:
            return False
        dropped = await store.delete_links(session, user_id, PROVIDER)
        await session.commit()
        log.info("mfp_account_changed", user_id=user_id, links_dropped=dropped)
        return True

    async def _apply(
        self,
        session: AsyncSession,
        user: User,
        row: Integration,
        api: MfpApi,
        day: date,
        desired: list[DesiredEntry],
        links: list[IntegrationLink],
    ) -> MirrorResult:
        remote = await api.diary(day)
        by_id = {str(entry["id"]): entry for entry in remote}
        claimed: set[str] = set()
        live: list[IntegrationLink] = []
        for link in links:
            state = _state(link)
            if state == "pending":
                continue
            if link.external_id in by_id:
                if state == "ghost":  # ours, and back in the listing
                    link.data = {**(link.data or {}), "state": "live"}
                live.append(link)
                claimed.add(link.external_id)
            elif state == "live":
                # gone from the listing (deleted by hand, or a listing that left it out): no
                # longer counted, but remembered by id, so a reappearance is known to be ours
                link.data = {**(link.data or {}), "state": "ghost"}
        for link in links:
            if _state(link) != "pending":
                continue
            wanted = link.data or {}
            match = next(
                (
                    entry
                    for entry in remote
                    if str(entry["id"]) not in claimed
                    and _food_id(entry) == wanted.get("food_id")
                    and entry.get("meal_name") == wanted.get("meal")
                ),
                None,
            )
            if match is None:  # the write never landed: the list below adds it again
                await store.delete_link(session, user.id, link.id)
                continue
            link.external_id = str(match["id"])
            link.data = {**wanted, "state": "live"}
            live.append(link)
            claimed.add(link.external_id)
            log.info("mfp_pending_resolved", user_id=user.id, day=day.isoformat())
        await session.commit()
        foreign = [entry for entry in remote if str(entry["id"]) not in claimed]
        data = dict(row.data or {})
        connected_on = _parse_day(data.get("connected_on"))
        if not links and foreign and connected_on is not None and day < connected_on:
            data["skipped"] = sorted({*(data.get("skipped") or []), day.isoformat()})
            row.data = data
            await session.commit()
            return MirrorResult(status="skipped")
        pool: dict[str, list[IntegrationLink]] = {}
        for link in live:
            pool.setdefault(link.key, []).append(link)
        to_add: list[DesiredEntry] = []
        for entry in desired:
            matches = pool.get(entry.key)
            if matches:
                matches.pop()
            else:
                to_add.append(entry)
        to_remove = [link for matches in pool.values() for link in matches]
        for link in to_remove:
            await api.delete_entry(link.external_id)
            await store.delete_link(session, user.id, link.id)
            await session.commit()
        for entry in to_add:
            await self._add(session, user, api, day, entry)
        if not to_add and not to_remove:
            return MirrorResult(status="unchanged")
        return MirrorResult(status="mirrored", added=len(to_add), removed=len(to_remove))

    async def _add(
        self, session: AsyncSession, user: User, api: MfpApi, day: date, entry: DesiredEntry
    ) -> None:
        """Write one entry: a pending row first, committed, then the POST, then its id."""
        food_id, food = await self._food(session, user, api, entry)
        link = await store.add_link(
            session,
            user.id,
            PROVIDER,
            LinkKind.entry,
            key=entry.key,
            external_id=f"pending:{uuid4().hex}",
            day=day,
            now=self._clock.now(),
            data={"state": "pending", "meal": entry.meal, "food_id": food_id},
        )
        await session.commit()
        try:
            entry_id = await api.add_entry(
                day, entry.meal, food_id, food["version"], food["serving"]
            )
        except MfpEntryRejectedError as exc:
            if exc.status >= 500:
                raise
            # the food moved on (a new version) or the user deleted it in MFP: look again, once
            fresh = await api.food(food_id)
            foods = await store.list_links(
                session, user.id, PROVIDER, LinkKind.food, key=entry.food_key
            )
            if fresh is None:
                for food_link in foods:
                    await store.delete_link(session, user.id, food_link.id)
                await session.commit()
                food_id, fresh = await self._food(session, user, api, entry)
                link.data = {**(link.data or {}), "food_id": food_id}
            else:
                for food_link in foods:
                    food_link.data = dict(fresh)
            await session.commit()
            entry_id = await api.add_entry(
                day, entry.meal, food_id, fresh["version"], fresh["serving"]
            )
        link.external_id = entry_id
        link.data = {
            "state": "live",
            "meal": entry.meal,
            "food_id": food_id,
            "name": entry.description,
        }
        await session.commit()

    async def _food(
        self, session: AsyncSession, user: User, api: MfpApi, entry: DesiredEntry
    ) -> tuple[str, dict[str, Any]]:
        """The custom food for this line: the one made before, or a new one."""
        known = await store.list_links(
            session, user.id, PROVIDER, LinkKind.food, key=entry.food_key
        )
        for link in known:
            data = link.data or {}
            if data.get("version") is not None and isinstance(data.get("serving"), dict):
                return link.external_id, data
        food_id = await api.create_food(entry)
        food = await api.food(food_id)
        if food is None:
            raise MfpError(f"food {food_id} vanished right after it was created")
        await store.add_link(
            session,
            user.id,
            PROVIDER,
            LinkKind.food,
            key=entry.food_key,
            external_id=food_id,
            now=self._clock.now(),
            data=food,
        )
        await session.commit()
        return food_id, food


# ---------------------------------------------------------------------------------- helpers


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _state(link: IntegrationLink) -> str:
    """``live`` (an entry in the diary), ``pending`` (written, answer not seen yet) or ``ghost``
    (ours, missing from the last listing)."""
    state = (link.data or {}).get("state")
    return state if state in ("pending", "ghost") else "live"


def _food_id(entry: Mapping[str, Any]) -> str:
    food = entry.get("food")
    return str(food.get("id") or "") if isinstance(food, dict) else ""


def _mirror_from(row: Integration, fallback: date) -> date:
    """The first day the mirror owns (connect day minus the backfill)."""
    data = row.data or {}
    return _parse_day(data.get("mirror_from")) or fallback
