"""Provider registry: one ``Integration`` per provider plus the periodic sync runner.

``build_registry(settings, session_factory, bus)`` returns an :class:`Integrations` mapping
(``dict[ProviderName, Integration]``) that also knows how to run the scheduler's
``integration_sync`` job: :meth:`Integrations.sync_all` pulls every connected OAuth integration
and publishes the resulting events on the bus. Adding a provider is one module and one line here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from strikt.core.clock import Clock, SystemClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import IntegrationStatus
from strikt.integrations import store
from strikt.integrations.apple_health import AppleHealthIntegration
from strikt.integrations.base import Integration, ProviderName
from strikt.integrations.whoop import WhoopIntegration
from strikt.integrations.withings import WithingsIntegration

if TYPE_CHECKING:
    import httpx

    from strikt.config import Settings
    from strikt.events import EventBus

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], Any]  # async_sessionmaker[AsyncSession] or a compatible callable
ClientFactory = Callable[[], "httpx.AsyncClient"]


class Integrations(dict[ProviderName, Integration]):
    """``dict[ProviderName, Integration]`` with the sync job attached."""

    def __init__(
        self,
        session_factory: SessionFactory,
        bus: EventBus,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__()
        self._sessions = session_factory
        self._bus = bus
        self._clock: Clock = clock or SystemClock()

    @property
    def bus(self) -> EventBus:
        return self._bus

    async def sync_user(self, user_id: int, provider: ProviderName) -> int:
        """Sync one user's provider in its own session; returns the number of events published."""
        integration = self.get(provider)
        if integration is None:
            return 0
        published = 0
        async with self._sessions() as session:
            user = await repo.get_user(session, user_id)
            if user is None:
                return 0
            try:
                events = await integration.sync(session, user, None)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                log.error(
                    "integration_sync_failed", provider=provider, user_id=user_id, error=repr(exc)
                )
                return 0
        for event in events:
            await self._bus.publish(event)
            published += 1
        return published

    async def sync_all(self, provider: ProviderName | None = None) -> int:
        """The scheduler's ``integration_sync`` job: every connected integration, one by one."""
        async with self._sessions() as session:
            rows = await store.list_connected(session, provider)
            targets = [
                (row.user_id, row.provider.value)
                for row in rows
                if row.status == IntegrationStatus.connected and row.provider.value in self
            ]
        total = 0
        for user_id, name in targets:
            total += await self.sync_user(user_id, name)  # type: ignore[arg-type]
        log.info("integration_sync_run", provider=provider, integrations=len(targets), events=total)
        return total


def build_registry(
    settings: Settings,
    session_factory: SessionFactory,
    bus: EventBus,
    *,
    clock: Clock | None = None,
    client_factory: ClientFactory | None = None,
) -> Integrations:
    """Every provider the server knows. WHOOP and Withings need the Fernet key for tokens; when
    it is missing they are still registered (``connect`` explains) but cannot store tokens."""
    registry = Integrations(session_factory, bus, clock=clock)
    key = settings.token_encryption_key.get_secret_value()
    cipher = TokenCipher(key) if key else None
    registry["apple_health"] = AppleHealthIntegration(settings, clock=clock)
    if cipher is not None:
        registry["whoop"] = WhoopIntegration(
            settings, cipher=cipher, clock=clock, client_factory=client_factory
        )
        registry["withings"] = WithingsIntegration(
            settings, cipher=cipher, clock=clock, client_factory=client_factory
        )
    else:
        log.warning("integrations_without_token_key", providers=["whoop", "withings"])
    return registry
