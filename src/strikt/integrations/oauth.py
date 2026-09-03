"""Signed per-user OAuth start links and single-use ``state`` handling.

Flow (PLAN §9, §11):

1. The bot hands the user a *start link* ``{PUBLIC_BASE_URL}/oauth/{provider}/start?u=<signed>``.
   ``u`` is ``base64url("<user_id>.<issued_ts>") + "." + base64url(HMAC-SHA256)`` so nobody can
   forge a link for another user; links expire after :data:`LINK_MAX_AGE_S`.
2. The web server verifies ``u``, creates a single-use ``oauth_states`` row (10-minute validity,
   handled by ``repo.create_oauth_state`` / ``repo.consume_oauth_state``) and redirects to the
   provider's authorize URL with that state.
3. The provider redirects back with ``code`` + ``state``; the state is consumed exactly once.

The HMAC key is a dedicated ``OAUTH_LINK_SECRET`` when the settings expose one, otherwise it is
derived from ``TOKEN_ENCRYPTION_KEY`` (never used raw: it goes through SHA-256 with a domain tag).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc
from strikt.db import repo
from strikt.db.models import Provider

if TYPE_CHECKING:
    from strikt.config import Settings
    from strikt.integrations.base import ProviderName

LINK_MAX_AGE_S = 24 * 3600
STATE_MAX_AGE_S = repo.OAUTH_STATE_MAX_AGE_S
_DOMAIN_TAG = b"strikt-oauth-link:"


class LinkError(ValueError):
    """The signed start link is malformed, forged or expired."""


# ------------------------------------------------------------------------------- provider slugs


def provider_slug(provider: ProviderName | str) -> str:
    """URL form of a provider name (``apple_health`` → ``apple-health``)."""
    return str(provider).replace("_", "-")


def provider_from_slug(slug: str) -> ProviderName | None:
    """Reverse of :func:`provider_slug`; accepts both ``apple-health`` and ``apple_health``."""
    name = slug.strip().lower().replace("-", "_")
    return _SLUGS.get(name)


_SLUGS: dict[str, ProviderName] = {
    "whoop": "whoop",
    "withings": "withings",
    "apple_health": "apple_health",
}


def public_base_url(settings: Settings) -> str:
    return str(settings.public_base_url).rstrip("/")


def callback_url(settings: Settings, provider: ProviderName | str) -> str:
    return f"{public_base_url(settings)}/oauth/{provider_slug(provider)}/callback"


def webhook_url(settings: Settings, provider: ProviderName | str, token: str | None = None) -> str:
    base = f"{public_base_url(settings)}/webhooks/{provider_slug(provider)}"
    return f"{base}/{token}" if token else base


# ------------------------------------------------------------------------------ signed user id


def link_secret(settings: Settings) -> bytes:
    """HMAC key for start links: ``OAUTH_LINK_SECRET`` if configured, else derived from the
    Fernet key. Raises ``ValueError`` when neither is set (the bot cannot issue links)."""
    dedicated = getattr(settings, "oauth_link_secret", None)
    value: str = ""
    if dedicated is not None:
        getter = getattr(dedicated, "get_secret_value", None)
        value = str(getter()) if callable(getter) else str(dedicated)
    if not value:
        value = settings.token_encryption_key.get_secret_value()
    if not value:
        raise ValueError("TOKEN_ENCRYPTION_KEY is empty: cannot sign OAuth start links")
    return hashlib.sha256(_DOMAIN_TAG + value.encode("utf-8")).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def sign_user(user_id: int, *, secret: bytes, now: datetime) -> str:
    """``base64url(user_id.ts) . base64url(hmac)``; safe to put in a query string."""
    payload = f"{user_id}.{int(ensure_utc(now).timestamp())}".encode("ascii")
    mac = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_user(
    token: str, *, secret: bytes, now: datetime, max_age_s: int = LINK_MAX_AGE_S
) -> int:
    """Return the user id inside a signed link. Raises :class:`LinkError` on any problem."""
    try:
        payload_b64, mac_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        mac = _unb64(mac_b64)
    except (ValueError, TypeError) as exc:
        raise LinkError("malformed link") from exc
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise LinkError("bad signature")
    try:
        user_part, ts_part = payload.decode("ascii").split(".", 1)
        user_id = int(user_part)
        issued = int(ts_part)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LinkError("malformed payload") from exc
    age = int(ensure_utc(now).timestamp()) - issued
    if age < -60 or age > max_age_s:
        raise LinkError("link expired")
    return user_id


def start_url(
    settings: Settings, provider: ProviderName | str, user_id: int, *, now: datetime
) -> str:
    """The link the bot shows the user (``connect_integration`` tool)."""
    token = sign_user(user_id, secret=link_secret(settings), now=now)
    return f"{public_base_url(settings)}/oauth/{provider_slug(provider)}/start?u={token}"


# --------------------------------------------------------------------------- single-use state


async def issue_state(
    session: AsyncSession, user_id: int, provider: ProviderName | str, *, now: datetime
) -> str:
    """A fresh random state (43 url-safe chars, satisfies WHOOP's "8+ characters")."""
    row = await repo.create_oauth_state(session, user_id, provider, now=now)
    return row.state


async def consume_state(
    session: AsyncSession, state: str, provider: ProviderName | str, *, now: datetime
) -> int | None:
    """User id for a valid, unexpired state of ``provider``; the row is deleted either way."""
    if not state or len(state) > 64:
        return None
    row = await repo.consume_oauth_state(session, state, now=now, max_age_s=STATE_MAX_AGE_S)
    if row is None or row.provider != Provider(provider):
        return None
    return row.user_id
