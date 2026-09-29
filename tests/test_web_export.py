"""GET /admin/export: bearer-gated, one user's turns and meals (estimate next to stored)."""

from __future__ import annotations

from datetime import date

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.core.clock import FakeClock, coaching_today
from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.engine import make_session_factory
from strikt.db.models import TurnRole, User
from strikt.events import EventBus
from strikt.web.export import MAX_DAYS, ExportError, authorized, parse_range
from strikt.web.server import make_app
from tests.test_integrations_fakes import make_settings

TOKEN = "export-token-for-tests"


async def _seed(session: AsyncSession, user: User, clock: FakeClock) -> date:
    now = clock.now()
    day = coaching_today(clock, user.timezone)
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=[
            {"type": "text", "text": "[image: abc]", "media_kind": "image", "media_file_id": "F"},
            {"type": "text", "text": "обед"},
        ],
        now=now,
    )
    await repo.add_turn(
        session, user.id, role=TurnRole.assistant, content=[{"type": "text", "text": "ok"}], now=now
    )
    meal = await repo.add_meal_with_items(
        session,
        user.id,
        day_date=day,
        items=[
            FoodItemIn(
                name="chicken",
                grams=150,
                macros=Macros(kcal=250, protein_g=45, carbs_g=0, fat_g=7, fiber_g=0),
            )
        ],
        logged_at=now,
    )
    meal.items[0].model_estimate = {"kcal": 240.0}
    await session.commit()
    return day


def _client(engine: AsyncEngine, clock: FakeClock, token: str | None) -> TestClient:  # type: ignore[type-arg]
    settings = make_settings(admin_export_token=token)
    app = make_app(settings, make_session_factory(engine), EventBus(), {}, clock=clock)
    return TestClient(TestServer(app))


async def test_export_returns_turns_and_meals(
    engine: AsyncEngine, session: AsyncSession, clock: FakeClock, user: User
) -> None:
    day = await _seed(session, user, clock)
    async with _client(engine, clock, TOKEN) as client:
        response = await client.get(
            f"/admin/export?user={user.id}&from={day}&to={day}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert response.status == 200
        payload = await response.json()
    assert [t["role"] for t in payload["turns"]] == ["user", "assistant"]
    first = payload["turns"][0]["content"]
    assert first[0]["text"] == "[image: abc]"
    assert "media_file_id" not in first[0]
    item = payload["meals"][0]["items"][0]
    assert item["stored"]["kcal"] == 250
    assert item["model_estimate"] == {"kcal": 240.0}
    assert "llm_key_enc" not in str(payload)


async def test_export_rejects_a_missing_or_wrong_token(
    engine: AsyncEngine, clock: FakeClock, user: User
) -> None:
    async with _client(engine, clock, TOKEN) as client:
        assert (await client.get(f"/admin/export?user={user.id}")).status == 401
        wrong = await client.get(
            f"/admin/export?user={user.id}", headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status == 401


async def test_export_route_absent_without_a_token(
    engine: AsyncEngine, clock: FakeClock, user: User
) -> None:
    async with _client(engine, clock, None) as client:
        response = await client.get(
            f"/admin/export?user={user.id}", headers={"Authorization": "Bearer "}
        )
        assert response.status == 404


async def test_export_unknown_user_and_bad_dates(
    engine: AsyncEngine, clock: FakeClock, user: User
) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with _client(engine, clock, TOKEN) as client:
        assert (await client.get("/admin/export?user=999999", headers=headers)).status == 404
        assert (await client.get("/admin/export?user=x", headers=headers)).status == 400
        bad = await client.get(f"/admin/export?user={user.id}&from=yesterday", headers=headers)
        assert bad.status == 400


def test_authorized_needs_the_bearer_scheme() -> None:
    assert authorized(f"Bearer {TOKEN}", TOKEN)
    assert authorized(f"bearer {TOKEN}", TOKEN)
    assert not authorized(TOKEN, TOKEN)
    assert not authorized(None, TOKEN)
    assert not authorized("Bearer x", "")


def test_parse_range_defaults_and_limits() -> None:
    today = date(2026, 9, 29)
    assert parse_range(None, None, today=today) == (today, today)
    assert parse_range("2026-09-28", None, today=today) == (date(2026, 9, 28), today)
    with pytest.raises(ExportError):
        parse_range("2026-09-30", "2026-09-29", today=today)
    with pytest.raises(ExportError):
        parse_range("2026-01-01", "2026-09-29", today=today)
    assert MAX_DAYS == 31
