"""Common contract for wearable and scale integrations.

Every provider (WHOOP, Withings, Apple Health, later Garmin/Oura/Fitbit) implements
``Integration``. The web server and the scheduler only talk to this protocol, so adding a
provider is a module plus one line in ``strikt.integrations.registry``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.db.models import User
from strikt.events import Event

ProviderName = Literal["whoop", "withings", "apple_health"]


@dataclass(frozen=True, kw_only=True)
class WebhookRequest:
    """Transport-agnostic view of an inbound webhook, built by the web server."""

    provider: ProviderName
    method: str
    path: str
    headers: dict[str, str]
    query: dict[str, str]
    body: bytes
    path_token: str | None = None  # the per-user token in the URL, when the provider uses one


@dataclass(frozen=True, kw_only=True)
class WebhookResponse:
    status: int = 200
    body: str = "ok"
    content_type: str = "text/plain"


@dataclass(frozen=True, kw_only=True)
class ConnectInfo:
    """What to show the user when they ask to connect a provider."""

    provider: ProviderName
    kind: Literal["oauth", "webhook", "instructions"]
    url: str | None = None
    instructions: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Integration(Protocol):
    provider: ProviderName

    async def connect(self, session: AsyncSession, user: User) -> ConnectInfo:
        """OAuth start URL, or the per-user webhook URL and instructions."""
        ...

    async def handle_callback(
        self, session: AsyncSession, query: dict[str, str]
    ) -> tuple[User | None, str]:
        """OAuth redirect handler: stores tokens, returns the user and a human message."""
        ...

    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        """Pull new data since ``since`` (None = provider default window), store it, return events."""
        ...

    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        """Verify, store, and return the events to publish. Never raises on bad input."""
        ...
