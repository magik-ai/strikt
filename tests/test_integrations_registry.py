"""Registry: providers present, sync_all publishes on the bus, failures are isolated."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.engine import make_session_factory
from strikt.db.models import User
from strikt.events import Event, EventBus, MeasurementEvent, WorkoutEvent
from strikt.integrations.base import ConnectInfo, WebhookRequest, WebhookResponse
from strikt.integrations.registry import Integrations, build_registry
from tests.test_integrations_fakes import NOW, WITHINGS_GETMEAS, Router, make_settings


class FakeIntegration:
    provider = "whoop"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.synced: list[int] = []

    async def connect(self, session: AsyncSession, user: User) -> ConnectInfo:
        return ConnectInfo(provider="whoop", kind="instructions", instructions="x")

    async def handle_callback(
        self, session: AsyncSession, query: dict[str, str]
    ) -> tuple[User | None, str]:
        return None, "x"

    async def sync(self, session: AsyncSession, user: User, since: datetime | None) -> list[Event]:
        if self.fail:
            raise RuntimeError("boom")
        self.synced.append(user.id)
        return [
            WorkoutEvent(
                user_id=user.id, occurred_at=NOW, source="whoop", sport="run", started_at=NOW
            )
        ]

    async def handle_webhook(
        self, session: AsyncSession, request: WebhookRequest
    ) -> tuple[WebhookResponse, list[Event]]:
        return WebhookResponse(), []


def test_build_registry_has_all_providers_and_respects_missing_key() -> None:
    bus = EventBus()
    registry = build_registry(make_settings(), lambda: None, bus)
    assert set(registry) == {"whoop", "withings", "apple_health"}
    assert isinstance(registry, dict)
    assert registry.bus is bus
    no_key = build_registry(make_settings(token_encryption_key=""), lambda: None, bus)
    assert set(no_key) == {"apple_health"}


async def test_sync_all_publishes_events_and_isolates_failures(
    engine: AsyncEngine, session: AsyncSession, user: User, clock: FakeClock
) -> None:
    settings = make_settings()
    cipher = TokenCipher(settings.token_encryption_key.get_secret_value())
    await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "whoop",
        access_token="a",
        refresh_token="r",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
    )
    await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "withings",
        access_token="a",
        refresh_token="r",
        expires_at=NOW + timedelta(hours=1),
        now=NOW,
        external_user_id="363",
    )
    await session.commit()
    factory = make_session_factory(engine)
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(Event, handler)
    registry = Integrations(factory, bus, clock=clock)
    fake = FakeIntegration()
    registry["whoop"] = fake
    router = Router()
    router.json("POST", "https://wbsapi.withings.net/measure", WITHINGS_GETMEAS)
    from strikt.integrations.withings import WithingsIntegration

    registry["withings"] = WithingsIntegration(
        settings, cipher=cipher, clock=clock, client_factory=router.client_factory()
    )
    total = await registry.sync_all()
    assert total == 5  # 1 fake workout + 4 withings readings
    assert fake.synced == [user.id]
    assert sum(isinstance(e, WorkoutEvent) for e in received) == 1
    assert sum(isinstance(e, MeasurementEvent) for e in received) == 4

    registry["whoop"] = FakeIntegration(fail=True)
    received.clear()
    assert await registry.sync_all(provider="whoop") == 0  # failure logged, not raised
    assert await registry.sync_user(999_999, "whoop") == 0  # unknown user
    assert await registry.sync_user(user.id, "apple_health") == 0  # not registered here
