"""MyFitnessPal: cookie parsing, the session, the day mirror, the connect page and the chat paste.

A fake MyFitnessPal (``FakeMfp``) answers the calls the website's own client makes: the cookie
for a bearer token, the CSRF token, a custom food, the food's version, the v2 diary. No network.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from itertools import count
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from strikt.core.clock import FakeClock, coaching_today
from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.engine import make_session_factory
from strikt.db.models import (
    ConversationTurn,
    Integration,
    IntegrationStatus,
    MealSlot,
    User,
)
from strikt.events import DayStateChanged, EventBus
from strikt.integrations import myfitnesspal as mfp
from strikt.integrations.base import link_copy
from strikt.integrations.myfitnesspal import MyFitnessPalIntegration
from strikt.integrations.oauth import connect_page_url
from strikt.integrations.registry import build_registry
from strikt.telegram.copy import t
from strikt.web.server import make_app
from tests.test_integrations_fakes import Router, make_settings

GOOD = "good-session-cookie-value"
ROTATED = "rotated-session-cookie-value"
CURL = (
    "curl 'https://www.myfitnesspal.com/food/diary' \\\n"
    "  -H 'accept: text/html' \\\n"
    f"  -b '_mfp_session={GOOD}; remember_me=remember-value-1234; known_user=42; _ga=GA1.2' \\\n"
    "  -H 'user-agent: Mozilla/5.0'"
)


# ------------------------------------------------------------------------- the fake service


class FakeMfp:
    """Just enough of MyFitnessPal: sessions, custom foods, one diary."""

    def __init__(self) -> None:
        self.router = Router()
        self.valid = {GOOD, ROTATED}
        self.blocked = False
        self.entries: dict[str, dict[str, Any]] = {}
        self.foods: dict[str, dict[str, Any]] = {}
        self.created_foods: list[dict[str, Any]] = []
        self.ids = count(1)
        self.rotate = True
        r = self.router
        r.add("GET", mfp.AUTH_TOKEN_URL, self.auth_token)
        r.add("GET", mfp.SESSION_URL, lambda _req: httpx.Response(200, json={"user": {}}))
        r.add("GET", mfp.CSRF_URL, lambda _req: httpx.Response(200, json={"csrfToken": "csrf-1"}))
        r.add("POST", mfp.FOODS_URL, self.create_food)
        r.add("GET", mfp.FOODS_API_URL, self.get_food)
        r.add("GET", mfp.DIARY_URL, self.diary)
        r.add("POST", mfp.DIARY_URL, self.add_entry)
        r.add("DELETE", mfp.DIARY_URL, self.delete_entry)

    def _session(self, request: httpx.Request) -> str | None:
        for part in request.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "_mfp_session":
                return value
        return None

    def auth_token(self, request: httpx.Request) -> httpx.Response:
        if self.blocked:
            return httpx.Response(403, headers={"server": "cloudflare"}, text="Just a moment")
        if self._session(request) not in self.valid:
            return httpx.Response(
                302, headers={"location": "https://www.myfitnesspal.com/account/login"}
            )
        headers = {}
        if self.rotate:
            headers["set-cookie"] = f"_mfp_session={ROTATED}; Path=/; Secure; HttpOnly"
        return httpx.Response(
            200,
            headers=headers,
            json={"access_token": "bearer-1", "user_id": "777", "expires_in": 86400},
        )

    def _authed(self, request: httpx.Request) -> bool:
        return (
            request.headers.get("authorization") == "Bearer bearer-1"
            and request.headers.get("mfp-user-id") == "777"
            and request.headers.get("mfp-client-id") == "mfp-main-js"
        )

    def create_food(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-csrf-token") != "csrf-1" or self._session(request) is None:
            return httpx.Response(403, json={"error": "csrf"})
        item = json.loads(request.content)["item"]
        food_id = f"food-{next(self.ids)}"
        self.created_foods.append(item)
        self.foods[food_id] = {
            "id": food_id,
            "version": f"v-{food_id}",
            "serving_sizes": [{**item["serving_sizes"][0]}],
            "nutritional_contents": item["nutritional_contents"],
            "description": item["description"],
        }
        return httpx.Response(200, json=[{"id": food_id}])

    def get_food(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return httpx.Response(401)
        food = self.foods.get(request.url.params.get("ids", ""))
        return httpx.Response(200, json={"items": [food] if food else []})

    def diary(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return httpx.Response(401)
        day = request.url.params.get("entry_date")
        items = [e for e in self.entries.values() if e["date"] == day]
        return httpx.Response(200, json={"items": items})

    def add_entry(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return httpx.Response(401)
        entry = json.loads(request.content)["items"][0]
        food = self.foods.get(entry["food"]["id"])
        if food is None or food["version"] != entry["food"]["version"]:
            return httpx.Response(422, json={"error": "invalid food version"})
        entry_id = f"entry-{next(self.ids)}"
        stored = {
            **entry,
            "id": entry_id,
            "food": {**entry["food"], "description": food["description"]},
            "nutritional_contents": food["nutritional_contents"],
        }
        self.entries[entry_id] = stored
        return httpx.Response(200, json={"items": [stored]})

    def delete_entry(self, request: httpx.Request) -> httpx.Response:
        if not self._authed(request):
            return httpx.Response(401)
        entry_id = request.url.path.rsplit("/", 1)[-1]
        if self.entries.pop(entry_id, None) is None:
            return httpx.Response(404)
        return httpx.Response(204)

    def day_entries(self, day: date) -> list[dict[str, Any]]:
        return [e for e in self.entries.values() if e["date"] == day.isoformat()]

    def names(self, day: date) -> list[tuple[str, str]]:
        return sorted((e["meal_name"], e["food"]["description"]) for e in self.day_entries(day))


@pytest.fixture
def fake() -> FakeMfp:
    return FakeMfp()


@pytest.fixture
def mfp_settings() -> Any:
    return make_settings()


@pytest.fixture
def integration(
    fake: FakeMfp, mfp_settings: Any, clock: FakeClock, engine: AsyncEngine
) -> MyFitnessPalIntegration:
    cipher = TokenCipher(mfp_settings.token_encryption_key.get_secret_value())
    return MyFitnessPalIntegration(
        mfp_settings,
        cipher=cipher,
        clock=clock,
        client_factory=fake.router.client_factory(),
        session_factory=make_session_factory(engine),
        mirror_delay_s=0,
    )


def _today(clock: FakeClock, user: User) -> date:
    return coaching_today(clock, user.timezone)


async def _log(
    session: AsyncSession,
    user: User,
    day: date,
    slot: MealSlot,
    *items: tuple[str, str | None, float, float, float, float, float],
    clock: FakeClock,
) -> None:
    meal = await repo.add_meal_with_items(
        session,
        user.id,
        day_date=day,
        items=[
            FoodItemIn(
                name=name,
                unit=portion,
                macros=Macros(kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=fib),
                confidence=0.9,
                source="user",
            )
            for name, portion, kcal, p, c, f, fib in items
        ],
        slot=slot,
        logged_at=clock.now(),
    )
    assert meal.id
    await session.commit()


async def _connect(
    integration: MyFitnessPalIntegration, session: AsyncSession, user: User, text: str = CURL
) -> mfp.ConnectOutcome:
    outcome = await integration.submit_cookies(session, user, text)
    await session.commit()
    await integration.drain()
    return outcome


# ---------------------------------------------------------------------------------- parsing


def test_parse_cookie_input_takes_every_shape_a_browser_hands_out() -> None:
    expected = {"_mfp_session": GOOD, "remember_me": "remember-value-1234", "known_user": "42"}
    header = f"_mfp_session={GOOD}; remember_me=remember-value-1234; known_user=42"
    assert mfp.parse_cookie_input(header) == expected
    assert mfp.parse_cookie_input(f"Host: www.myfitnesspal.com\ncookie: {header}\naccept: */*") == (
        expected
    )
    assert mfp.parse_cookie_input(f"curl 'https://x' -H 'cookie: {header}' -H 'a: b'") == expected
    assert mfp.parse_cookie_input(CURL) == {**expected, "_ga": "GA1.2"}
    cmd = f'curl ^"https://x^" -b ^"_mfp_session={GOOD}; known_user=42^" -H ^"a: b^"'
    assert mfp.parse_cookie_input(cmd) == {"_mfp_session": GOOD, "known_user": "42"}
    fetch = f'fetch("https://x", {{"headers": {{"cookie": "{header}"}}}});'
    assert mfp.parse_cookie_input(fetch) == expected
    export = json.dumps([{"name": "_mfp_session", "value": GOOD, "domain": ".myfitnesspal.com"}])
    assert mfp.parse_cookie_input(export) == {"_mfp_session": GOOD}
    assert mfp.parse_cookie_input("") == {} and mfp.parse_cookie_input("омлет и кофе") == {}
    assert mfp.has_login(expected) and not mfp.has_login({"known_user": "42"})


def test_looks_like_mfp_cookies_spots_a_login_and_nothing_else() -> None:
    assert mfp.looks_like_mfp_cookies(CURL)
    assert mfp.looks_like_mfp_cookies(f"_mfp_session={GOOD}")
    assert mfp.looks_like_mfp_cookies("__Secure-next-auth.session-token=eyJhbGciOiJkaXIiLCJlbmMi")
    assert not mfp.looks_like_mfp_cookies("подключи MyFitnessPal")
    assert not mfp.looks_like_mfp_cookies("remember_me=1")  # too short to be a login
    assert not mfp.looks_like_mfp_cookies(None)


def test_meal_names_follow_the_slot_or_the_clock() -> None:
    assert mfp.meal_name(MealSlot.breakfast, None) == "Breakfast"
    assert mfp.meal_name(MealSlot.dinner, None) == "Dinner"
    assert mfp.meal_name(MealSlot.snack, None) == "Snacks"
    morning = datetime(2026, 9, 3, 9, 30)
    assert mfp.meal_name(MealSlot.unknown, morning) == "Breakfast"
    assert mfp.meal_name(MealSlot.unknown, morning.replace(hour=13)) == "Lunch"
    assert mfp.meal_name(MealSlot.unknown, morning.replace(hour=16, minute=30)) == "Snacks"
    assert mfp.meal_name(MealSlot.unknown, morning.replace(hour=20)) == "Dinner"
    assert mfp.meal_name(MealSlot.unknown, None) == "Snacks"


def test_desired_entry_keeps_the_numbers_and_reuses_identical_lines() -> None:
    a = mfp.DesiredEntry(
        meal="Lunch",
        name="творог 0.5%",
        portion="200 г",
        kcal=180,
        protein_g=36,
        carbs_g=6,
        fat_g=1,
        fiber_g=0,
    )
    b = mfp.DesiredEntry(
        meal="Dinner",
        name="творог  0.5%",
        portion="200 г",
        kcal=180,
        protein_g=36,
        carbs_g=6,
        fat_g=1,
        fiber_g=0,
    )
    assert a.description == "творог 0.5% (200 г)"
    assert a.food_key == b.food_key and a.key != b.key  # one food, two diary slots
    nutrition = a.nutrition()
    assert nutrition["energy"] == {"unit": "calories", "value": 180}
    assert (nutrition["protein"], nutrition["carbohydrates"], nutrition["fat"]) == (36, 6, 1)
    changed = mfp.DesiredEntry(**{**a.__dict__, "kcal": 190})
    assert changed.food_key != a.food_key


# ---------------------------------------------------------------------------------- connect


async def test_connect_gives_the_signed_page_and_instructions(
    integration: MyFitnessPalIntegration, session: AsyncSession, user: User
) -> None:
    info = await integration.connect(session, user)
    assert info.kind == "form"
    assert info.url is not None and info.url.startswith(
        "https://coach.example.com/connect/myfitnesspal?u="
    )
    assert info.instructions == t("ru", "mfp.connect")
    form = integration.connect_form("en")
    assert form.title == "Connect MyFitnessPal" and "Copy as cURL" in form.steps


async def test_a_paste_without_a_login_or_with_a_dead_one_is_refused(
    integration: MyFitnessPalIntegration, fake: FakeMfp, session: AsyncSession, user: User
) -> None:
    nothing = await _connect(integration, session, user, "known_user=42; _ga=1")
    assert nothing.status == "invalid" and nothing.message == t("ru", "mfp.invalid")
    assert not fake.router.calls  # not even asked
    dead = await _connect(integration, session, user, "_mfp_session=logged-out-cookie-1")
    assert dead.status == "invalid"
    assert await repo.get_integration(session, user.id, "myfitnesspal") is None


async def test_connect_stores_the_login_encrypted_and_copies_the_week(
    integration: MyFitnessPalIntegration,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
    mfp_settings: Any,
) -> None:
    today = _today(clock, user)
    lunch = ("творог 0.5%", "200 г", 180.0, 36.0, 6.0, 1.0, 0.0)
    await _log(session, user, today, MealSlot.lunch, lunch, clock=clock)
    await _log(
        session,
        user,
        today - timedelta(days=2),
        MealSlot.breakfast,
        ("омлет", "3 яйца", 270.0, 19.0, 2.0, 20.0, 0.0),
        lunch,
        clock=clock,
    )
    await _log(session, user, today - timedelta(days=10), MealSlot.dinner, lunch, clock=clock)

    with capture_logs() as logs:
        outcome = await _connect(integration, session, user)
    assert outcome.status == "connected"
    assert outcome.message == t("ru", "mfp.connected", count=mfp.BACKFILL_DAYS)
    assert GOOD not in repr(logs) and ROTATED not in repr(logs)

    session.expunge_all()
    row = await repo.get_integration(session, user.id, "myfitnesspal")
    assert row is not None and row.status == IntegrationStatus.connected
    assert row.external_user_id == "777"
    assert row.refresh_token_enc is not None and GOOD not in row.refresh_token_enc
    cipher = TokenCipher(mfp_settings.token_encryption_key.get_secret_value())
    cookies = json.loads(repo.integration_tokens(cipher, row).refresh_token or "{}")
    assert cookies["_mfp_session"] == ROTATED  # the re-issued cookie replaced the pasted one
    assert row.data is not None
    assert row.data["mirror_from"] == (today - timedelta(days=6)).isoformat()

    assert fake.names(today) == [("Lunch", "творог 0.5% (200 г)")]
    assert fake.names(today - timedelta(days=2)) == [
        ("Breakfast", "омлет (3 яйца)"),
        ("Breakfast", "творог 0.5% (200 г)"),
    ]
    assert fake.day_entries(today - timedelta(days=10)) == []  # before the backfill window
    assert len(fake.created_foods) == 2  # the same cottage cheese is one food
    food = next(f for f in fake.created_foods if f["description"].startswith("творог"))
    assert food["brand_name"] == "Strikt" and food["public"] is False
    assert "country_code" not in food  # MFP keeps carbohydrates exactly as sent
    assert food["nutritional_contents"]["carbohydrates"] == 6.0


async def test_mirror_follows_every_change_and_leaves_the_users_own_entries_alone(
    integration: MyFitnessPalIntegration,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
) -> None:
    today = _today(clock, user)
    await _connect(integration, session, user)
    fake.entries["own-1"] = {
        "id": "own-1",
        "type": "food_entry",
        "date": today.isoformat(),
        "meal_name": "Snacks",
        "food": {"id": "db-1", "description": "apple"},
    }
    await _log(
        session,
        user,
        today,
        MealSlot.dinner,
        ("стейк", "250 г", 520.0, 62.0, 0.0, 30.0, 0.0),
        ("брюссельская капуста", None, 150.0, 6.0, 14.0, 9.0, 6.0),
        clock=clock,
    )
    result = await integration.mirror_day(session, user, today)
    assert (result.status, result.added, result.removed) == ("mirrored", 2, 0)
    assert len(fake.day_entries(today)) == 3

    calls = len(fake.router.calls)
    assert (await integration.mirror_day(session, user, today)).status == "unchanged"
    assert len(fake.router.calls) == calls  # nothing changed: MyFitnessPal is not asked

    # the coach corrects the steak and drops the sprouts: one delete, one add, the apple stays
    meals = await repo.list_meals_for_date(session, user.id, today)
    steak, sprouts = meals[0].items
    steak.kcal = 480.0
    await session.delete(sprouts)
    await session.commit()
    result = await integration.mirror_day(session, user, today)
    assert (result.added, result.removed) == (1, 2)
    assert sorted(e["food"]["description"] for e in fake.day_entries(today)) == [
        "apple",
        "стейк (250 г)",
    ]
    steak_entry = next(e for e in fake.day_entries(today) if e["id"] != "own-1")
    assert steak_entry["nutritional_contents"]["energy"]["value"] == 480.0

    # deleted by hand in MyFitnessPal: the list did not change, so a plain mirror does not even
    # ask; the sync's check of today and yesterday reads the diary and puts it back
    del fake.entries[steak_entry["id"]]
    assert (await integration.mirror_day(session, user, today)).status == "unchanged"
    result = await integration.mirror_day(session, user, today, verify=True)
    assert (result.added, result.removed) == (1, 0)
    assert len(fake.day_entries(today)) == 2
    calls = len(fake.router.calls)
    result = await integration.mirror_day(session, user, today, verify=True)
    assert result.status == "unchanged" and len(fake.router.calls) == calls + 1  # one read

    # the day cleared: every entry Strikt made goes, the user's apple stays
    for meal in await repo.list_meals_for_date(session, user.id, today):
        await session.delete(meal)
    await session.commit()
    await integration.mirror_day(session, user, today)
    assert [e["id"] for e in fake.day_entries(today)] == ["own-1"]


async def test_a_moved_food_version_is_looked_up_again(
    integration: MyFitnessPalIntegration,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
) -> None:
    today = _today(clock, user)
    await _connect(integration, session, user)
    item = ("шейк", "330 мл", 160.0, 30.0, 4.0, 2.0, 1.0)
    await _log(session, user, today, MealSlot.snack, item, clock=clock)
    await integration.mirror_day(session, user, today)
    food_id = next(iter(fake.foods))
    fake.foods[food_id]["version"] = "v-edited"  # the user edited the food in MFP
    await _log(session, user, today, MealSlot.snack, item, clock=clock)
    result = await integration.mirror_day(session, user, today)
    assert result.added == 1 and len(fake.day_entries(today)) == 2
    del fake.foods[food_id]  # and then deleted it: a new one is made
    await _log(session, user, today, MealSlot.snack, item, clock=clock)
    await integration.mirror_day(session, user, today)
    assert len(fake.day_entries(today)) == 3 and len(fake.foods) == 1


async def test_a_past_day_with_food_already_in_mfp_is_skipped_and_said(
    integration: MyFitnessPalIntegration,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
) -> None:
    told: list[str] = []

    async def notify(_user: User, text: str) -> None:
        told.append(text)

    integration.notify = notify
    today = _today(clock, user)
    past = today - timedelta(days=3)
    await _log(
        session,
        user,
        past,
        MealSlot.lunch,
        ("суп", None, 300.0, 15.0, 30.0, 10.0, 5.0),
        clock=clock,
    )
    fake.entries["own-2"] = {
        "id": "own-2",
        "type": "food_entry",
        "date": past.isoformat(),
        "meal_name": "Lunch",
        "food": {"id": "db-2", "description": "soup"},
    }
    await _connect(integration, session, user)
    assert [e["id"] for e in fake.day_entries(past)] == ["own-2"]
    assert told == [t("ru", "mfp.skipped", dates=f"{past:%d.%m}")]
    assert (await integration.mirror_day(session, user, past)).status == "skipped"


async def test_a_dead_session_expires_once_and_the_user_is_told_once(
    engine: AsyncEngine,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
    mfp_settings: Any,
) -> None:
    registry = build_registry(
        mfp_settings,
        make_session_factory(engine),
        EventBus(),
        clock=clock,
        client_factory=fake.router.client_factory(),
        mirror_delay_s=0,
    )
    told: list[str] = []

    async def notify(_user: User, text: str) -> None:
        told.append(text)

    registry.set_notifier(notify)
    integration = registry["myfitnesspal"]
    assert isinstance(integration, MyFitnessPalIntegration)
    await _connect(integration, session, user)
    fake.valid.clear()  # logged out everywhere; the bearer still works for a while
    clock.advance(timedelta(days=2))  # past the bearer's expiry and the keepalive interval
    await _log(
        session,
        user,
        _today(clock, user),
        MealSlot.lunch,
        ("рис", "150 г", 190.0, 4.0, 42.0, 0.5, 0.6),
        clock=clock,
    )
    await registry.sync_all()
    session.expunge_all()
    row = await repo.get_integration(session, user.id, "myfitnesspal")
    assert row is not None and row.status == IntegrationStatus.expired
    assert told == [link_copy("ru", "lost", "myfitnesspal")]
    await registry.sync_all()
    assert len(told) == 1  # once per breakage
    fake.valid.add(GOOD)
    await _connect(integration, session, user)  # reconnect: news again next time
    session.expunge_all()
    row = await repo.get_integration(session, user.id, "myfitnesspal")
    assert row is not None and row.status == IntegrationStatus.connected and row.notified_at is None
    assert fake.names(_today(clock, user)) == [("Lunch", "рис (150 г)")]


async def test_a_firewall_answer_is_stored_unchecked_not_refused(
    integration: MyFitnessPalIntegration, fake: FakeMfp, session: AsyncSession, user: User
) -> None:
    fake.blocked = True
    outcome = await _connect(integration, session, user)
    assert outcome.status == "unchecked"
    assert t("ru", "mfp.unchecked") in outcome.message
    row = await repo.get_integration(session, user.id, "myfitnesspal")
    assert row is not None and row.status == IntegrationStatus.connected


async def test_day_state_changed_mirrors_the_day_through_the_bus(
    engine: AsyncEngine,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
    mfp_settings: Any,
) -> None:
    bus = EventBus()
    registry = build_registry(
        mfp_settings,
        make_session_factory(engine),
        bus,
        clock=clock,
        client_factory=fake.router.client_factory(),
        mirror_delay_s=0,
    )
    integration = registry["myfitnesspal"]
    assert isinstance(integration, MyFitnessPalIntegration)
    await _connect(integration, session, user)
    today = _today(clock, user)
    await _log(
        session,
        user,
        today,
        MealSlot.breakfast,
        ("овсянка", "60 г", 230.0, 8.0, 38.0, 4.0, 6.0),
        clock=clock,
    )
    for _ in range(3):  # three tool rounds in one turn: one mirror
        await bus.publish(
            DayStateChanged(
                user_id=user.id, occurred_at=clock.now(), date=today, reason="set_day_food"
            )
        )
    await integration.drain()
    assert fake.names(today) == [("Breakfast", "овсянка (60 г)")]
    # a user with no MyFitnessPal costs nothing
    other = DayStateChanged(user_id=user.id + 99, occurred_at=clock.now(), date=today, reason="x")
    await bus.publish(other)
    await integration.drain()


# ------------------------------------------------------------------------------- the page


@pytest.fixture
async def page_client(
    engine: AsyncEngine, fake: FakeMfp, clock: FakeClock, mfp_settings: Any
) -> Any:
    sessions = make_session_factory(engine)
    registry = build_registry(
        mfp_settings,
        sessions,
        EventBus(),
        clock=clock,
        client_factory=fake.router.client_factory(),
        mirror_delay_s=0,
    )
    told: list[tuple[int, str]] = []

    async def notify(user: User, text: str) -> None:
        told.append((user.id, text))

    app = make_app(mfp_settings, sessions, EventBus(), registry, clock=clock, notify=notify)
    async with TestClient(TestServer(app)) as client:
        client.told = told  # type: ignore[attr-defined]
        client.registry = registry  # type: ignore[attr-defined]
        yield client
    integration = registry["myfitnesspal"]
    assert isinstance(integration, MyFitnessPalIntegration)
    await integration.drain()


async def test_connect_page_shows_the_form_and_takes_the_paste(
    page_client: Any, user: User, clock: FakeClock, mfp_settings: Any, session: AsyncSession
) -> None:
    url = connect_page_url(mfp_settings, "myfitnesspal", user.id, now=clock.now())
    path = url.removeprefix("https://coach.example.com")
    token = path.split("u=", 1)[1]
    page = await page_client.get(path)
    assert page.status == 200 and page.headers["Cache-Control"] == "no-store"
    body = await page.text()
    assert "Подключить MyFitnessPal" in body and "<textarea name='cookies'" in body
    assert f"value='{token}'" in body

    bad = await page_client.post(
        "/connect/myfitnesspal",
        data=urlencode({"u": token, "cookies": "nothing here"}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert bad.status == 400 and page_client.told == []

    good = await page_client.post(
        "/connect/myfitnesspal",
        data=urlencode({"u": token, "cookies": CURL}),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert good.status == 200
    assert t("ru", "link.back") in await good.text()
    assert page_client.told == [(user.id, t("ru", "mfp.connected", count=mfp.BACKFILL_DAYS))]
    row = await repo.get_integration(session, user.id, "myfitnesspal")
    assert row is not None and row.status == IntegrationStatus.connected

    forged = await page_client.get("/connect/myfitnesspal?u=" + token[:-2] + "xx")
    assert forged.status == 400
    assert (await page_client.get(f"/connect/whoop?u={token}")).status == 404


# ---------------------------------------------------------------------------- the chat paste


async def test_cookies_pasted_into_the_chat_are_deleted_stored_and_never_a_turn(
    engine: AsyncEngine,
    fake: FakeMfp,
    session: AsyncSession,
    user: User,
    clock: FakeClock,
    mfp_settings: Any,
) -> None:
    from strikt.telegram.handlers import handle_message
    from strikt.telegram.messenger import FakeMessenger
    from tests.conftest import CHAT_ID
    from tests.test_byok_handlers import make_byok
    from tests.test_handlers_e2e import msg

    messenger = FakeMessenger()
    from strikt.agent.client import FakeLLM

    llm = FakeLLM()
    byok = make_byok(engine, clock, llm, messenger)
    registry = build_registry(
        byok.settings,
        byok.deps.sessions,
        byok.deps.bus,
        clock=clock,
        client_factory=fake.router.client_factory(),
        mirror_delay_s=0,
    )
    byok.deps.integrations = registry
    with capture_logs() as logs:
        await handle_message(byok.deps, msg(CURL, message_id=300))
    assert messenger.deletes == [(CHAT_ID, 300)]
    assert messenger.texts(CHAT_ID) == [t("ru", "mfp.connected", count=mfp.BACKFILL_DAYS)]
    assert llm.calls == []
    session.expunge_all()
    assert (await session.scalars(select(ConversationTurn))).all() == []
    assert GOOD not in repr(logs)
    row = (await session.scalars(select(Integration))).one()
    assert row.provider.value == "myfitnesspal" and row.status == IntegrationStatus.connected
    integration = registry["myfitnesspal"]
    assert isinstance(integration, MyFitnessPalIntegration)
    await integration.drain()
