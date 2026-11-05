"""Provider registry: one ``Integration`` per provider plus the periodic sync runner.

``build_registry(settings, session_factory, bus)`` returns an :class:`Integrations` mapping
(``dict[ProviderName, Integration]``) that also knows how to run the scheduler's
``integration_sync`` job: :meth:`Integrations.sync_all` pulls every connected OAuth integration
and publishes the resulting events on the bus. Adding a provider is one module and one line here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import time
from typing import TYPE_CHECKING, Any

import structlog

from strikt.core.clock import Clock, SystemClock, ensure_utc, in_quiet_hours, to_local
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import Integration as IntegrationRow, IntegrationStatus, User, UserStatus
from strikt.events import DayStateChanged
from strikt.integrations import store
from strikt.integrations.apple_health import AppleHealthIntegration
from strikt.integrations.base import Integration, ProviderName, link_copy
from strikt.integrations.myfitnesspal import MyFitnessPalIntegration
from strikt.integrations.whoop import WhoopIntegration
from strikt.integrations.withings import WithingsIntegration

if TYPE_CHECKING:
    from collections.abc import Awaitable

    import httpx

    from strikt.config import Settings
    from strikt.events import EventBus

log = structlog.get_logger(__name__)

SessionFactory = Callable[[], Any]  # async_sessionmaker[AsyncSession] or a compatible callable
ClientFactory = Callable[[], "httpx.AsyncClient"]
Notify = Callable[["User", str], "Awaitable[None]"]


class Integrations(dict[ProviderName, Integration]):
    """``dict[ProviderName, Integration]`` with the sync job attached."""

    def __init__(
        self,
        session_factory: SessionFactory,
        bus: EventBus,
        *,
        clock: Clock | None = None,
        quiet_start: time = time(0, 0),
        quiet_end: time = time(7, 30),
    ) -> None:
        super().__init__()
        self._sessions = session_factory
        self._bus = bus
        self._clock: Clock = clock or SystemClock()
        self._notify: Notify | None = None
        self._quiet_start = quiet_start
        self._quiet_end = quiet_end

    @property
    def bus(self) -> EventBus:
        return self._bus

    def set_notifier(self, notify: Notify) -> None:
        """How integrations talk to the user in Telegram (the app wires the messenger in)."""
        self._notify = notify
        mfp = self.get("myfitnesspal")
        if isinstance(mfp, MyFitnessPalIntegration):
            mfp.notify = notify

    async def aclose(self) -> None:
        """Stop background work (MyFitnessPal mirrors still waiting) on shutdown."""
        mfp = self.get("myfitnesspal")
        if isinstance(mfp, MyFitnessPalIntegration):
            await mfp.aclose()

    async def announce_broken(self) -> int:
        """Tell each user once that a connection died (a refresh MFP or WHOOP rejected).

        The sync marks such a row ``expired`` and stops pulling; before this nothing said so,
        and the coach went quiet about sleep and training while the user thought it was all
        connected. ``notified_at`` makes it once per breakage; a reconnect clears it.
        """
        if self._notify is None:
            return 0
        told = 0
        async with self._sessions() as session:
            rows = await store.list_unannounced(session, IntegrationStatus.expired)
            for row in rows:
                if row.provider.value not in self:
                    continue
                user = await repo.get_user(session, row.user_id)
                if user is None or user.status != UserStatus.active:
                    continue
                if await self._quiet(session, user):
                    continue  # quiet hours are absolute: it goes out on the first run after
                try:
                    await self._notify(user, link_copy(user.language, "lost", row.provider.value))
                except Exception as exc:
                    log.warning(
                        "integration_lost_notice_failed",
                        provider=row.provider.value,
                        user_id=row.user_id,
                        error=repr(exc),
                    )
                    continue
                await _mark_announced(session, row, self._clock)
                told += 1
            await session.commit()
        if told:
            log.info("integration_lost_announced", count=told)
        return told

    async def _quiet(self, session: Any, user: User) -> bool:
        profile = await repo.get_profile(session, user.id)
        start = (profile.quiet_start if profile else None) or self._quiet_start
        end = (profile.quiet_end if profile else None) or self._quiet_end
        return in_quiet_hours(to_local(self._clock.now(), user.timezone or "UTC"), start, end)

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
        try:
            await self.announce_broken()
        except Exception as exc:
            log.warning("integration_lost_announce_failed", error=repr(exc))
        return total


async def _mark_announced(session: Any, row: IntegrationRow, clock: Clock) -> None:
    row.notified_at = ensure_utc(clock.now())
    await session.flush()


def build_registry(
    settings: Settings,
    session_factory: SessionFactory,
    bus: EventBus,
    *,
    clock: Clock | None = None,
    client_factory: ClientFactory | None = None,
    mirror_delay_s: float | None = None,
) -> Integrations:
    """Every provider the server knows. WHOOP and Withings need the Fernet key for tokens; when
    it is missing they are still registered (``connect`` explains) but cannot store tokens."""
    registry = Integrations(
        session_factory,
        bus,
        clock=clock,
        quiet_start=settings.quiet_start,
        quiet_end=settings.quiet_end,
    )
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
        mfp = MyFitnessPalIntegration(
            settings,
            cipher=cipher,
            clock=clock,
            client_factory=client_factory,
            session_factory=session_factory,
            **({} if mirror_delay_s is None else {"mirror_delay_s": mirror_delay_s}),
        )
        registry["myfitnesspal"] = mfp
        bus.subscribe(DayStateChanged, mfp.on_day_changed)
    else:
        log.warning(
            "integrations_without_token_key", providers=["whoop", "withings", "myfitnesspal"]
        )
    return registry
