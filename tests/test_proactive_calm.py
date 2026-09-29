"""The calmer proactive defaults: no ladder, an hour between nudges, meal nudges wait for the
user, and the morning message is a code-rendered check of yesterday's list."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.engine import make_session_factory
from strikt.db.models import TurnRole, User
from strikt.proactive.engine import ProactiveEngine
from strikt.telegram.messenger import FakeMessenger
from tests.test_proactive_helpers import (
    TODAY,
    DbStateProvider,
    FakeDecider,
    at_local,
    make_sender,
    seed_meal,
)


class Planner:
    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def schedule_followup(self, user_id: int, parent: str, window_key: str, at: object) -> str:
        self.scheduled.append(window_key)
        return window_key

    def cancel_followups(self, user_id: int, *, window_prefixes: object = None) -> int:
        return 0


@pytest.fixture
def planner() -> Planner:
    return Planner()


@pytest.fixture
def decider() -> FakeDecider:
    return FakeDecider()


@pytest.fixture
async def eng(
    engine: AsyncEngine,
    clock: FakeClock,
    settings: Settings,
    messenger: FakeMessenger,
    decider: FakeDecider,
    planner: Planner,
) -> AsyncIterator[ProactiveEngine]:
    engine_ = ProactiveEngine(
        make_session_factory(engine),
        decider,
        DbStateProvider(),
        make_sender(messenger),
        clock,
        settings,
        followups=planner,
    )
    yield engine_
    engine_.close()


async def wrote(session: AsyncSession, user: User, hhmm: str) -> None:
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=[{"type": "text", "text": "привет"}],
        now=at_local(TODAY, hhmm),
    )
    await session.commit()


async def test_meal_nudges_wait_for_the_first_message(
    eng: ProactiveEngine, user: User, clock: FakeClock, session: AsyncSession
) -> None:
    clock.set(at_local(TODAY, "11:05"))
    asleep = await eng.fire(user.id, "no_first_meal")
    assert asleep.status == "skipped" and asleep.reason == "user_not_up_yet"
    await wrote(session, user, "10:30")
    assert (await eng.fire(user.id, "no_first_meal")).sent


async def test_no_ladder_and_an_hour_between_nudges(
    eng: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    planner: Planner,
) -> None:
    await wrote(session, user, "09:00")
    clock.set(at_local(TODAY, "14:40"))
    first = await eng.fire(user.id, "no_first_meal")
    assert first.sent and planner.scheduled == []  # no 45-minute follow-up
    clock.set(at_local(TODAY, "15:05"))
    too_soon = await eng.fire(user.id, "no_lunch")
    assert too_soon.status == "skipped" and too_soon.reason == "min_gap"


async def test_morning_check_lists_yesterday_exactly(
    eng: ProactiveEngine,
    user: User,
    clock: FakeClock,
    session: AsyncSession,
    messenger: FakeMessenger,
    decider: FakeDecider,
) -> None:
    yday = TODAY - timedelta(days=1)
    await seed_meal(session, user.id, yday, "09:00", kcal=210, protein=18, name="3 яйца")
    await seed_meal(session, user.id, yday, "14:00", kcal=495, protein=93, name="курица 300 г")
    await session.commit()
    clock.set(at_local(TODAY, "10:05"))
    out = await eng.fire(user.id, "morning_line")
    assert out.sent and decider.calls == []  # rendered by code, no model call
    text = messenger.sent[-1].text
    assert text.startswith("Вчера записано:")
    assert "3 яйца - 210 ккал · Б 18" in text and "курица 300 г - 495 ккал · Б 93" in text
    assert "Итого: 705 ккал · Б 111" in text and "Всё верно?" in text


async def test_no_morning_check_without_food(
    eng: ProactiveEngine, user: User, clock: FakeClock, messenger: FakeMessenger
) -> None:
    clock.set(at_local(TODAY, "10:05"))
    out = await eng.fire(user.id, "morning_line")
    assert out.status == "skipped" and out.reason == "nothing_to_check"
    assert messenger.sent == []
