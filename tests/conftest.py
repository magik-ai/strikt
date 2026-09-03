"""Shared fixtures: SQLite engine/session, FakeClock, FakeLLM, FakeMessenger, a seeded user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, time

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.agent.client import FakeLLM
from strikt.agent.tools import Registry, ToolContext, build_registry
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.crypto import TokenCipher, generate_key
from strikt.db.engine import (
    SQLITE_MEMORY_URL,
    init_sqlite_for_tests,
    make_engine,
    make_session_factory,
)
from strikt.db.models import (
    CoachingIntensity,
    PrimaryKpi,
    Profile,
    Protocol,
    User,
    UserStatus,
)
from strikt.telegram.messenger import FakeMessenger

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)  # 12:00 in Asia/Dubai
TELEGRAM_ID = 111_222_333
CHAT_ID = 111_222_333


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = make_engine(SQLITE_MEMORY_URL)
    await init_sqlite_for_tests(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as session:
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, token_encryption_key=generate_key())  # type: ignore[call-arg]


@pytest.fixture
def cipher(settings: Settings) -> TokenCipher:
    return TokenCipher(settings.token_encryption_key.get_secret_value())


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


@pytest.fixture
def registry() -> Registry:
    return build_registry()


@pytest.fixture
async def user(session: AsyncSession, clock: FakeClock) -> User:
    """An active user in Asia/Dubai speaking Russian, with a profile and a 2000 kcal protocol."""
    user, _ = await repo.get_or_create_user(
        session,
        telegram_id=TELEGRAM_ID,
        chat_id=CHAT_ID,
        now=clock.now(),
        language="ru",
        timezone="Asia/Dubai",
        status=UserStatus.active,
    )
    await repo.upsert_profile(
        session,
        user.id,
        {
            "name": "Test",
            "city": "Dubai",
            "height_cm": 190,
            "birth_year": 1988,
            "sex": "male",
            "goal_text": "waist under 94",
            "primary_kpi": PrimaryKpi.waist,
            "kpi_target_low": 94,
            "kpi_target_high": 90,
            "kpi_unit": "cm",
            "wake_time": time(8, 0),
            "bed_time": time(0, 30),
            "coaching_intensity": CoachingIntensity.pushy,
            "onboarding_step": 10,
            "onboarding_done_at": clock.now(),
        },
        now=clock.now(),
    )
    await repo.set_active_protocol(
        session,
        user.id,
        kcal=2000,
        protein_g=210,
        fat_g=105,
        carbs_g=75,
        fiber_g=30,
        rationale="chosen after discussion",
        now=clock.now(),
    )
    await session.commit()
    return user


@pytest.fixture
async def profile(session: AsyncSession, user: User) -> Profile:
    profile = await repo.get_profile(session, user.id)
    assert profile is not None
    return profile


@pytest.fixture
async def protocol(session: AsyncSession, user: User) -> Protocol:
    protocol = await repo.get_active_protocol(session, user.id)
    assert protocol is not None
    return protocol


@pytest.fixture
async def tool_ctx(
    session: AsyncSession,
    user: User,
    profile: Profile,
    protocol: Protocol,
    clock: FakeClock,
    settings: Settings,
    fake_llm: FakeLLM,
    messenger: FakeMessenger,
) -> ToolContext:
    return ToolContext(
        session=session,
        user=user,
        profile=profile,
        protocol=protocol,
        clock=clock,
        settings=settings,
        services={"llm": fake_llm, "messenger": messenger},
    )
