"""Food tools: search_food, log_meal (sanity + resolution), update_meal, delete_meal, undo_last."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.tools import Registry, ToolContext, food as food_tools, schemas
from strikt.core.types import Incoming, Macros
from strikt.db import repo
from strikt.db.models import ItemSource, MealSource, User

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def offline_http() -> httpx.AsyncClient:
    """OFF answers 404 (not found), USDA answers an empty result list: never a real network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "openfoodfacts" in request.url.host:
            return httpx.Response(404, json={"status": "failure"})
        return httpx.Response(200, json={"foods": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def item(name: str, kcal: float, p: float, c: float, f: float, **kw: Any) -> schemas.MealItemInput:
    return schemas.MealItemInput(name=name, kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, **kw)


def parsed(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(str(result.content))
    return data


async def seed_yogurt(session: AsyncSession) -> None:
    await repo.upsert_food(
        session,
        name="Greek yogurt 0%",
        brand="Fage",
        per_100g=Macros(kcal=57, protein_g=10.3, carbs_g=3.6, fat_g=0.2, fiber_g=0),
        source=ItemSource.label,
        fetched_at=NOW,
        serving_g=170,
        source_url="https://example.com/fage",
    )


# ------------------------------------------------------------------------------- search_food


async def test_search_food_returns_cache_hits_without_network(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_yogurt(session)
    tool_ctx.services["http"] = offline_http()
    result = parsed(
        await food_tools.search_food(tool_ctx, schemas.SearchFoodInput(name="Greek yogurt"))
    )
    assert result["hits"][0]["name"] == "Greek yogurt 0%"
    assert result["hits"][0]["per_100g"]["P"] == 10.3
    assert result["hits"][0]["per_serving"]["kcal"] == 97
    assert result["hits"][0]["url"] == "https://example.com/fage"


async def test_search_food_miss_is_an_honest_error(tool_ctx: ToolContext) -> None:
    tool_ctx.services["http"] = offline_http()
    result = await food_tools.search_food(tool_ctx, schemas.SearchFoodInput(name="xyzzy fruit"))
    assert result.is_error
    assert "web_research" in str(result.content)


async def test_search_food_restaurant_dish_is_cache_only(tool_ctx: ToolContext) -> None:
    tool_ctx.services["http"] = offline_http()
    result = await food_tools.search_food(
        tool_ctx, schemas.SearchFoodInput(name="tonkotsu ramen", restaurant="Kinoya")
    )
    assert result.is_error
    assert "cache only" in str(result.content)


# ---------------------------------------------------------------------------------- log_meal


async def test_log_meal_avocado_plate_returns_corrected_fat_and_flag_line(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    args = schemas.LogMealInput(items=[item("Chicken avocado plate", 303, 42, 18, 7)], slot="lunch")
    result = parsed(await food_tools.log_meal(tool_ctx, args))
    logged = result["items"][0]
    assert logged["F"] >= 15, "avocado alone carries 15 g fat"
    assert logged["kcal"] == 375
    assert any(line.startswith("implausible_fat") for line in logged["flags"])
    assert result["flags"] and "avocado" in result["flags"][0]
    assert result["day"]["totals"]["kcal"] == 375
    assert result["day"]["remaining"]["kcal"] == 2000 - 375
    assert result["day"]["remaining"]["P"] == 210 - 42
    assert result["ask_slot"] is False

    meal = await repo.get_meal(session, tool_ctx.user_id, result["meal_id"])
    assert meal is not None
    row = meal.items[0]
    assert row.model_estimate == {
        "kcal": 303.0,
        "protein_g": 42.0,
        "carbs_g": 18.0,
        "fat_g": 7.0,
        "fiber_g": 0.0,
        "sodium_mg": None,
        "alcohol_g": 0.0,
    }
    assert row.flags == ["implausible_fat"]
    assert row.source == ItemSource.model


async def test_log_meal_loose_food_gets_buffer_and_ask_slot(tool_ctx: ToolContext) -> None:
    args = schemas.LogMealInput(items=[item("tonkotsu ramen", 780, 38, 85, 30, countable=False)])
    result = parsed(await food_tools.log_meal(tool_ctx, args))
    logged = result["items"][0]
    assert logged["countable"] is False
    assert logged["kcal"] == 975  # +25 % buffer from settings
    assert result["ask_slot"] is True
    assert any("loose_under_report" in line for line in result["flags"])


async def test_log_meal_resolves_missing_numbers_from_cache(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_yogurt(session)
    tool_ctx.services["http"] = offline_http()
    args = schemas.LogMealInput(
        items=[item("Greek yogurt 0%", 0, 0, 0, 0, brand="Fage", grams=160)], slot="dinner"
    )
    result = parsed(await food_tools.log_meal(tool_ctx, args))
    logged = result["items"][0]
    assert logged["kcal"] == 91 and logged["P"] == 16.5
    assert logged["source"] == "label"
    assert "unresolved" not in result


async def test_log_meal_reports_unresolved_names(tool_ctx: ToolContext) -> None:
    tool_ctx.services["http"] = offline_http()
    args = schemas.LogMealInput(items=[item("mystery dish", 0, 0, 0, 0)])
    result = parsed(await food_tools.log_meal(tool_ctx, args))
    assert result["unresolved"] == ["mystery dish"]


async def test_log_meal_source_and_day_follow_the_message(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    tool_ctx.incoming = Incoming(
        user_id=tool_ctx.user_id,
        chat_id=1,
        message_id=77,
        received_at=NOW,
        attachments=[{"kind": "image", "sha256": "abc"}],  # type: ignore[list-item]
    )
    args = schemas.LogMealInput(
        items=[item("2 eggs", 140, 12, 1, 10, quantity=2, unit="piece")],
        slot="breakfast",
        eaten_at=datetime(2026, 9, 2, 23, 30),  # naive → local Dubai time → 2 Sep
    )
    result = parsed(await food_tools.log_meal(tool_ctx, args))
    assert result["date"] == "2026-09-02"
    meal = await repo.get_meal(session, tool_ctx.user_id, result["meal_id"])
    assert meal is not None
    assert meal.source == MealSource.photo
    assert meal.raw_ref == {"message_id": 77, "sha256": ["abc"]}


async def test_log_meal_through_registry(registry: Registry, tool_ctx: ToolContext) -> None:
    result = await registry.dispatch(
        tool_ctx,
        "log_meal",
        {
            "items": [
                {
                    "name": "banana",
                    "kcal": 105,
                    "protein_g": 1.3,
                    "carbs_g": 27,
                    "fat_g": 0.4,
                    "fiber_g": 3.1,
                }
            ]
        },
    )
    assert not result.is_error
    assert json.loads(str(result.content))["items"][0]["name"] == "banana"


# ------------------------------------------------------------------------------- update_meal


async def _log_plate(ctx: ToolContext) -> dict[str, Any]:
    args = schemas.LogMealInput(
        items=[item("beef bowl", 800, 60, 80, 24, grams=400, countable=True, source="user")],
        slot="dinner",
    )
    return parsed(await food_tools.log_meal(ctx, args))


async def test_update_meal_only_a_quarter_scales_macros(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    logged = await _log_plate(tool_ctx)
    item_id = logged["items"][0]["id"]
    result = parsed(
        await food_tools.update_meal(
            tool_ctx,
            schemas.UpdateMealInput(
                item_id=item_id,
                changes=schemas.MealItemChanges(grams=100),
                reason="only ate a quarter",
            ),
        )
    )
    assert result["factor"] == 0.25
    assert result["item"]["kcal"] == 200 and result["item"]["P"] == 15 and result["item"]["F"] == 6
    assert result["day"]["totals"]["kcal"] == 200
    row = await repo.get_meal_item(session, tool_ctx.user_id, item_id)
    assert row is not None
    assert row.source == ItemSource.user
    assert row.user_correction is not None
    assert row.user_correction["reason"] == "only ate a quarter"
    assert row.user_correction["before"]["kcal"] == 800


async def test_update_meal_explicit_macros_recompute_kcal(tool_ctx: ToolContext) -> None:
    logged = await _log_plate(tool_ctx)
    item_id = logged["items"][0]["id"]
    result = parsed(
        await food_tools.update_meal(
            tool_ctx,
            schemas.UpdateMealInput(
                item_id=item_id, changes=schemas.MealItemChanges(fat_g=40, name="beef bowl, oily")
            ),
        )
    )
    assert result["item"]["name"] == "beef bowl, oily"
    assert result["item"]["F"] == 40
    assert result["item"]["kcal"] == 60 * 4 + 80 * 4 + 40 * 9


async def test_update_meal_slot_and_time_on_the_meal(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    logged = await _log_plate(tool_ctx)
    result = parsed(
        await food_tools.update_meal(
            tool_ctx,
            schemas.UpdateMealInput(
                meal_id=logged["meal_id"],
                changes=schemas.MealItemChanges(slot="lunch", note="Kinoya"),
            ),
        )
    )
    assert result["slot"] == "lunch" and result["note"] == "Kinoya"
    # a one-item meal accepts a portion change addressed to the meal
    result = parsed(
        await food_tools.update_meal(
            tool_ctx,
            schemas.UpdateMealInput(
                meal_id=logged["meal_id"], changes=schemas.MealItemChanges(grams=200)
            ),
        )
    )
    assert result["item"]["kcal"] == 400


async def test_update_meal_errors_are_one_liners(tool_ctx: ToolContext) -> None:
    missing = await food_tools.update_meal(
        tool_ctx, schemas.UpdateMealInput(item_id=999, changes=schemas.MealItemChanges(grams=1))
    )
    assert missing.is_error and "999" in str(missing.content)
    neither = await food_tools.update_meal(
        tool_ctx, schemas.UpdateMealInput(changes=schemas.MealItemChanges(grams=1))
    )
    assert neither.is_error


async def test_update_meal_ignores_other_users_items(
    tool_ctx: ToolContext, session: AsyncSession, user: User
) -> None:
    logged = await _log_plate(tool_ctx)
    other, _ = await repo.get_or_create_user(
        session, telegram_id=999, chat_id=999, now=NOW, timezone="Asia/Dubai"
    )
    tool_ctx.user = other
    result = await food_tools.update_meal(
        tool_ctx,
        schemas.UpdateMealInput(
            item_id=logged["items"][0]["id"], changes=schemas.MealItemChanges(grams=1)
        ),
    )
    assert result.is_error


# --------------------------------------------------------------------- delete_meal / undo_last


async def test_delete_meal_and_undo_last_recompute_totals(tool_ctx: ToolContext) -> None:
    first = await _log_plate(tool_ctx)
    tool_ctx.clock.tick(60)  # type: ignore[attr-defined]
    second = parsed(
        await food_tools.log_meal(
            tool_ctx, schemas.LogMealInput(items=[item("apple", 95, 0.5, 25, 0.3)], slot="snack")
        )
    )
    assert second["day"]["totals"]["kcal"] == 895
    undone = parsed(await food_tools.undo_last(tool_ctx, schemas.UndoLastInput()))
    assert undone["undone_meal_id"] == second["meal_id"]
    assert undone["removed"] == ["apple"]
    assert undone["day"]["totals"]["kcal"] == 800
    deleted = parsed(
        await food_tools.delete_meal(tool_ctx, schemas.DeleteMealInput(meal_id=first["meal_id"]))
    )
    assert deleted["day"]["totals"]["kcal"] == 0
    again = await food_tools.delete_meal(
        tool_ctx, schemas.DeleteMealInput(meal_id=first["meal_id"])
    )
    assert again.is_error
    nothing = await food_tools.undo_last(tool_ctx, schemas.UndoLastInput())
    assert nothing.is_error and "nothing to undo" in str(nothing.content)
