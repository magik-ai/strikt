"""set_day_food / save_my_food: the whole day as one list, numbers stored as given."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.tools import ToolContext, dayfood, schemas
from strikt.db import repo
from strikt.db.models import NoteKind


def line(name: str, kcal: float, p: float, c: float, f: float, fib: float, **kw: Any) -> Any:
    kw.setdefault("slot", "breakfast")
    kw.setdefault("source", "model")
    return schemas.DayFoodItem(
        name=name, kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=fib, **kw
    )


def parsed(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(str(result.content))
    return data


async def put(ctx: ToolContext, items: list[Any], change: str = "x", **kw: Any) -> dict[str, Any]:
    return parsed(
        await dayfood.set_day_food(ctx, schemas.SetDayFoodInput(eaten=items, change=change, **kw))
    )


async def stored(ctx: ToolContext, session: AsyncSession) -> list[tuple[str, float, float]]:
    meals = await repo.list_meals_for_date(session, ctx.user_id, ctx.local_date)
    return [(i.name, i.kcal, i.fiber_g) for m in meals for i in m.items]


async def test_numbers_are_stored_exactly_as_given(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    """A single candy at 55 kcal stays 55: nothing raises its fat to a floor any more."""
    result = await put(
        tool_ctx,
        [
            line("шоколадная конфета", 55, 0.5, 6, 3.5, 0.3, slot="snack"),
            line("полавокадо", 160, 2, 9, 15, 7),
            line("чили 300 г", 316, 34, 22, 10, 5.5, slot="lunch", portion="300 г"),
        ],
    )
    assert await stored(tool_ctx, session) == [
        ("шоколадная конфета", 55, 0.3),
        ("полавокадо", 160, 7),
        ("чили 300 г", 316, 5.5),
    ]
    assert result["day"]["totals"]["kcal"] == 531
    assert len(result["added"]) == 3 and result["removed"] == []


async def test_the_list_replaces_and_the_diff_says_what_moved(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await put(tool_ctx, [line("wrap", 207, 38, 7, 3, 0), line("salmon", 321, 39, 5, 16, 3)])
    result = await put(
        tool_ctx,
        [line("wrap", 386, 55, 10, 14, 0), line("omelette platter", 205, 20, 0.4, 14, 2)],
        change="salmon not eaten, wrap 300 g, omelette from photo 1",
    )
    assert [a.split(":")[0] for a in result["added"]] == ["omelette platter"]
    assert [r.split(":")[0] for r in result["removed"]] == ["salmon"]
    assert (
        result["changed"][0].startswith("wrap: 207 kcal") and "-> 386 kcal" in result["changed"][0]
    )
    assert await stored(tool_ctx, session) == [("wrap", 386, 0), ("omelette platter", 205, 2)]
    assert "note_on_removed" in result


async def test_an_empty_list_never_wipes_the_day_by_accident(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await put(tool_ctx, [line("eggs", 210, 18, 1, 15, 0)])
    result = await dayfood.set_day_food(tool_ctx, schemas.SetDayFoodInput(eaten=[], change="oops"))
    assert result.is_error and "clear_day" in str(result.content)
    assert await stored(tool_ctx, session) == [("eggs", 210, 0)]
    await put(tool_ctx, [], change="user wiped the day", clear_day=True)
    assert await stored(tool_ctx, session) == []


async def test_a_plan_is_kept_apart_and_not_counted(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    result = await put(
        tool_ctx,
        [line("eggs", 210, 18, 1, 15, 0)],
        planned=["обед: курица 300 г + салат (~550 ккал)"],
    )
    assert result["planned"] == ["обед: курица 300 г + салат (~550 ккал)"]
    assert result["day"]["totals"]["kcal"] == 210
    # omitting `planned` keeps it, [] clears it
    assert (await put(tool_ctx, [line("eggs", 210, 18, 1, 15, 0)]))["planned"] != []
    assert (await put(tool_ctx, [line("eggs", 210, 18, 1, 15, 0)], planned=[]))["planned"] == []


async def test_checks_speak_but_change_nothing(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    result = await put(
        tool_ctx,
        [
            line("Slice Avocado", 96, 1, 5, 9, 0),
            line("mystery bowl", 900, 10, 10, 10, 0),
            line("шейк", 140, 27, 3, 2, 0),
            line("шейк", 140, 27, 3, 2, 0),
        ],
    )
    checks = " ".join(result["check"])
    assert "Slice Avocado: fiber 0" in checks
    assert "mystery bowl: 900 kcal but its macros give" in checks
    assert "'шейк' is in the list 2 times" in checks
    assert ("mystery bowl", 900, 0) in await stored(tool_ctx, session)


async def test_changing_a_closed_day_reopens_it(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    day = tool_ctx.local_date - timedelta(days=1)
    await dayfood.set_day_food(
        tool_ctx,
        schemas.SetDayFoodInput(date=day, eaten=[line("eggs", 140, 12, 1, 10, 0)], change="x"),
    )
    await repo.close_day(
        session, tool_ctx.user_id, day, verdict="1500 kcal", now=tool_ctx.clock.now()
    )
    result = parsed(
        await dayfood.set_day_food(
            tool_ctx,
            schemas.SetDayFoodInput(
                date=day, eaten=[line("eggs", 210, 18, 1, 15, 0)], change="3 eggs, not 2"
            ),
        )
    )
    assert "reopened" in result
    row = await repo.get_day(session, tool_ctx.user_id, day)
    assert row is not None and row.closed_at is None and row.verdict is None


async def test_old_and_future_days_are_refused(tool_ctx: ToolContext) -> None:
    for delta in (timedelta(days=30), timedelta(days=-3)):
        result = await dayfood.set_day_food(
            tool_ctx,
            schemas.SetDayFoodInput(
                date=tool_ctx.local_date - delta, eaten=[line("x", 1, 0, 0, 0, 0)], change="x"
            ),
        )
        assert result.is_error


async def test_save_my_food_replaces_by_name(tool_ctx: ToolContext, session: AsyncSession) -> None:
    first = parsed(
        await dayfood.save_my_food(
            tool_ctx,
            schemas.SaveMyFoodInput(
                name="шейк на миндальном",
                portion="1 шейк",
                kcal=140,
                protein_g=27,
                carbs_g=3,
                fat_g=2,
                fiber_g=0,
            ),
        )
    )
    assert first["text"] == "шейк на миндальном | 1 шейк | 140 kcal P27 C3 F2 fib0"
    second = parsed(
        await dayfood.save_my_food(
            tool_ctx,
            schemas.SaveMyFoodInput(
                name="Шейк на миндальном",
                portion="1 шейк",
                kcal=150,
                protein_g=27,
                carbs_g=4,
                fat_g=3,
                fiber_g=0,
            ),
        )
    )
    foods = await repo.list_active_notes(session, tool_ctx.user_id, kinds=[NoteKind.food])
    assert [n.id for n in foods] == [second["saved_food_id"]]
    assert "150 kcal" in foods[0].text
