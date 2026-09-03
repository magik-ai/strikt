"""Food tools: search_food, log_meal, update_meal, delete_meal, undo_last (PLAN §5, §6.4).

The number is the product (brief §3.2): every write returns per-item macros after the sanity
layer, the flags in one line each, the day totals and what remains, plus the ids the model needs
for corrections. Every query filters by ``ctx.user_id``; repo functions flush, the turn loop
commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.tools.common import (
    build_state,
    clip,
    fail,
    flag_lines,
    health_context,
    macros_dict,
    meal_day,
    ok,
    rnd,
    state_numbers,
    to_utc,
)
from strikt.core.types import FoodHit, FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import ItemSource, Meal, MealItem, MealSlot, MealSource, SecretService
from strikt.nutrition import store
from strikt.nutrition.math import kcal_from_macros, round_macros, scale_per_100g
from strikt.nutrition.resolve import resolve_food
from strikt.nutrition.sanity import check_item
from strikt.nutrition.units import to_grams

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)

MAX_CACHE_HITS = 3
MAX_ITEMS_PER_MEAL = 40


# ------------------------------------------------------------------------------------ helpers


def _hit_dict(hit: FoodHit) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": hit.name,
        "per_100g": macros_dict(hit.per_100g),
        "source": hit.source,
        "confidence": rnd(hit.confidence, 2),
    }
    if hit.brand:
        out["brand"] = hit.brand
    if hit.restaurant:
        out["restaurant"] = hit.restaurant
    if hit.serving_g:
        out["serving_g"] = rnd(hit.serving_g)
        out["per_serving"] = macros_dict(scale_per_100g(hit.per_100g, hit.serving_g))
    if hit.serving_desc:
        out["serving"] = hit.serving_desc
    if hit.source_url:
        out["url"] = hit.source_url
    return out


def _item_dict(item: MealItem, flags: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": item.id,
        "name": item.name,
        **macros_dict(repo.item_macros(item)),
        "countable": item.countable,
        "source": item.source.value,
    }
    if item.grams is not None:
        out["grams"] = rnd(item.grams)
    if item.quantity is not None:
        out["qty"] = rnd(item.quantity, 2)
        if item.unit:
            out["unit"] = item.unit
    if flags:
        out["flags"] = flags
    elif item.flags:
        out["flags"] = [str(f) for f in item.flags]
    return out


def _macros_missing(item: FoodItemIn) -> bool:
    m = item.macros
    return m.kcal <= 0 and m.protein_g <= 0 and m.carbs_g <= 0 and m.fat_g <= 0


def _meal_source(ctx: ToolContext) -> MealSource:
    incoming = ctx.incoming
    if incoming is None:
        return MealSource.text
    if incoming.forwarded_from:
        return MealSource.forwarded
    kinds = {a.kind for a in incoming.attachments}
    if "image" in kinds or "document" in kinds:
        return MealSource.photo
    if "voice" in kinds:
        return MealSource.voice
    return MealSource.text


def _raw_ref(ctx: ToolContext) -> dict[str, Any] | None:
    incoming = ctx.incoming
    if incoming is None:
        return None
    ref: dict[str, Any] = {"message_id": incoming.message_id}
    hashes = [a.sha256 for a in incoming.attachments if a.sha256]
    if hashes:
        ref["sha256"] = hashes
    if incoming.forwarded_from:
        ref["forwarded_from"] = incoming.forwarded_from
    return ref


async def _usda_key(ctx: ToolContext) -> str | None:
    """The user's own USDA key when they gave one: their rate limit, not the server's."""
    cipher = ctx.services.get("cipher")
    if cipher is None:
        return None
    return await repo.get_user_secret(ctx.session, ctx.user_id, SecretService.usda, cipher)


async def _resolve_missing(ctx: ToolContext, item: FoodItemIn) -> FoodItemIn:
    """A name without numbers: look it up (cache → OFF → USDA) and scale to the portion."""
    hit = await resolve_food(
        ctx.session,
        item.name,
        brand=item.brand,
        restaurant=item.restaurant,
        http=ctx.services.get("http"),
        settings=ctx.settings,
        usda_key=await _usda_key(ctx),
        now=ctx.clock.now(),
    )
    if hit is None:
        return item
    grams = item.grams
    if grams is None and item.quantity is not None:
        grams = to_grams(item.quantity, item.unit, food=item.name, serving_g=hit.serving_g)
    if grams is None:
        grams = hit.serving_g or 100.0
    return item.model_copy(
        update={
            "grams": grams,
            "macros": round_macros(scale_per_100g(hit.per_100g, grams)),
            "source": hit.source,
            "source_url": hit.source_url,
            "confidence": hit.confidence,
        }
    )


# ---------------------------------------------------------------------------------- handlers


async def search_food(ctx: ToolContext, args: schemas.SearchFoodInput) -> ToolResult:
    """Cache → Open Food Facts (barcode) → USDA, plus up to three fuzzy cache matches."""
    if not args.name.strip() and not args.barcode:
        return fail("search_food: give a name or a barcode")
    hit = await resolve_food(
        ctx.session,
        args.name,
        brand=args.brand,
        restaurant=args.restaurant,
        barcode=args.barcode,
        http=ctx.services.get("http"),
        settings=ctx.settings,
        usda_key=await _usda_key(ctx),
        now=ctx.clock.now(),
    )
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    if hit is not None:
        hits.append(_hit_dict(hit))
        seen.add(repo.make_food_key(hit.name, hit.brand, hit.restaurant))
    for food in await store.search_by_name(ctx.session, args.name, limit=MAX_CACHE_HITS + 1):
        if food.key in seen or len(hits) >= MAX_CACHE_HITS + 1:
            continue
        seen.add(food.key)
        hits.append(_hit_dict(store.food_to_hit(food)))
    if not hits:
        where = "restaurant dish: cache only" if args.restaurant else "cache, OFF and USDA"
        return fail(
            f"no match for '{args.name}' in {where}; use web_research or estimate from "
            "ingredients and say so"
        )
    return ok({"query": args.name, "hits": hits[: MAX_CACHE_HITS + 1]})


async def log_meal(ctx: ToolContext, args: schemas.LogMealInput) -> ToolResult:
    """Resolve, sanity-check, persist, and return the numbers the reply must state."""
    if not args.items:
        return fail("log_meal: no items given")
    if len(args.items) > MAX_ITEMS_PER_MEAL:
        return fail(f"log_meal: too many items ({len(args.items)}); split the meal")
    now = ctx.clock.now()
    eaten_at = to_utc(args.eaten_at, ctx.tz) if args.eaten_at is not None else now
    day = meal_day(ctx, eaten_at)
    buffer = float(getattr(ctx.settings, "loose_food_buffer", 0.25))
    context = health_context(ctx)

    originals: list[FoodItemIn] = []
    corrected: list[FoodItemIn] = []
    flag_codes: dict[int, list[str]] = {}
    flag_text: list[list[str]] = []
    unresolved: list[str] = []
    for position, raw in enumerate(args.items):
        item = raw.to_food_item()
        if _macros_missing(item):
            item = await _resolve_missing(ctx, item)
            if _macros_missing(item):
                unresolved.append(item.name)
        originals.append(item)
        checked, flags = check_item(item, health_context=context, buffer=buffer)
        corrected.append(checked)
        if flags:
            flag_codes[position] = [f.code for f in flags]
        flag_text.append(flag_lines(flags))

    meal = await repo.add_meal_with_items(
        ctx.session,
        ctx.user_id,
        day_date=day,
        items=corrected,
        slot=args.slot or MealSlot.unknown,
        source=_meal_source(ctx),
        logged_at=now,
        eaten_at=eaten_at,
        raw_ref=_raw_ref(ctx),
        note=args.note,
        item_flags=flag_codes,
    )
    for row, original in zip(meal.items, originals, strict=True):
        row.model_estimate = original.macros.model_dump()
    await repo.get_or_open_day(ctx.session, ctx.user_id, day, now=now)
    await ctx.session.flush()

    state = await build_state(ctx, day)
    items_out = [_item_dict(row, flags) for row, flags in zip(meal.items, flag_text, strict=True)]
    all_flags = [
        f"{row.name}: {line}"
        for row, lines in zip(meal.items, flag_text, strict=True)
        for line in lines
    ]
    result: dict[str, Any] = {
        "meal_id": meal.id,
        "slot": meal.slot.value,
        "date": day.isoformat(),
        "items": items_out,
        "meal_total": macros_dict(repo.meal_macros(meal)),
        "flags": all_flags,
        "day": state_numbers(state),
        "ask_slot": meal.slot == MealSlot.unknown,
    }
    if unresolved:
        result["unresolved"] = unresolved
        result["hint"] = "no numbers found for these; estimate from ingredients and update_meal"
    log.info("meal_logged", user_id=ctx.user_id, meal_id=meal.id, items=len(meal.items))
    return ok(result)


def _scaled(item: MealItem, changes: schemas.MealItemChanges) -> tuple[Macros, float | None]:
    """Macros after a portion change (grams or quantity), before explicit macro overrides."""
    macros = repo.item_macros(item)
    factor: float | None = None
    if changes.grams is not None and item.grams:
        factor = changes.grams / item.grams
    elif changes.quantity is not None and item.quantity:
        factor = changes.quantity / item.quantity
    return (macros.scaled(factor) if factor is not None else macros), factor


async def _update_item(
    ctx: ToolContext, item_id: int, changes: schemas.MealItemChanges, reason: str | None
) -> ToolResult:
    item = await repo.get_meal_item(ctx.session, ctx.user_id, item_id)
    if item is None:
        return fail(f"item {item_id} not found")
    before = repo.item_macros(item)
    macros, factor = _scaled(item, changes)
    explicit = {
        key: value
        for key, value in (
            ("protein_g", changes.protein_g),
            ("carbs_g", changes.carbs_g),
            ("fat_g", changes.fat_g),
            ("fiber_g", changes.fiber_g),
            ("sodium_mg", changes.sodium_mg),
        )
        if value is not None
    }
    if explicit:
        macros = macros.model_copy(update=explicit)
    if changes.kcal is not None:
        macros = macros.model_copy(update={"kcal": changes.kcal})
    elif explicit and any(k in explicit for k in ("protein_g", "carbs_g", "fat_g")):
        macros = macros.model_copy(
            update={
                "kcal": kcal_from_macros(
                    macros.protein_g, macros.carbs_g, macros.fat_g, macros.alcohol_g
                )
            }
        )
    macros = round_macros(macros)
    numbers_changed = macros != before

    fields: dict[str, Any] = {
        "kcal": macros.kcal,
        "protein_g": macros.protein_g,
        "carbs_g": macros.carbs_g,
        "fat_g": macros.fat_g,
        "fiber_g": macros.fiber_g,
        "sodium_mg": macros.sodium_mg,
    }
    if changes.name is not None:
        fields["name"] = clip(changes.name, 200)
    if changes.grams is not None:
        fields["grams"] = changes.grams
    if changes.quantity is not None:
        fields["quantity"] = changes.quantity
    if changes.unit is not None:
        fields["unit"] = changes.unit
    if changes.countable is not None:
        fields["countable"] = changes.countable
    if numbers_changed:
        fields["source"] = ItemSource.user
        fields["confidence"] = 0.9
    correction = {
        "reason": reason,
        "changes": changes.model_dump(exclude_none=True, mode="json"),
        "before": before.model_dump(),
        "factor": factor,
    }
    await repo.update_meal_item(
        ctx.session, ctx.user_id, item_id, fields, user_correction=correction
    )
    meal = await repo.get_meal(ctx.session, ctx.user_id, item.meal_id, include_deleted=True)
    if meal is None:
        return fail(f"meal {item.meal_id} not found")
    if changes.slot is not None or changes.eaten_at is not None or changes.note is not None:
        await _apply_meal_changes(ctx, meal, changes)
    await ctx.session.refresh(item)
    state = await build_state(ctx, meal.day_date)
    return ok(
        {
            "item": _item_dict(item),
            "before": macros_dict(before),
            "factor": rnd(factor, 3) if factor is not None else None,
            "meal_id": meal.id,
            "meal_total": macros_dict(repo.meal_macros(meal)),
            "day": state_numbers(state),
        }
    )


async def _apply_meal_changes(
    ctx: ToolContext, meal: Meal, changes: schemas.MealItemChanges
) -> None:
    eaten_at = to_utc(changes.eaten_at, ctx.tz) if changes.eaten_at is not None else None
    day_date = meal_day(ctx, eaten_at) if eaten_at is not None else None
    await repo.update_meal(
        ctx.session,
        ctx.user_id,
        meal.id,
        slot=changes.slot,
        eaten_at=eaten_at,
        note=changes.note,
        day_date=day_date,
    )


async def update_meal(ctx: ToolContext, args: schemas.UpdateMealInput) -> ToolResult:
    """Corrections: an item's portion/macros (item_id) or a meal's slot/time/note (meal_id)."""
    if args.item_id is not None:
        return await _update_item(ctx, args.item_id, args.changes, args.reason)
    if args.meal_id is None:
        return fail("update_meal: give item_id (portion/macros) or meal_id (slot/time/note)")
    meal = await repo.get_meal(ctx.session, ctx.user_id, args.meal_id)
    if meal is None:
        return fail(f"meal {args.meal_id} not found (deleted or not yours)")
    changes = args.changes
    if len(meal.items) == 1 and any(
        v is not None
        for v in (
            changes.grams,
            changes.quantity,
            changes.kcal,
            changes.protein_g,
            changes.carbs_g,
            changes.fat_g,
            changes.fiber_g,
            changes.name,
            changes.countable,
        )
    ):
        # One-item meal: portion/macro changes addressed to the meal go to its only item.
        return await _update_item(ctx, meal.items[0].id, changes, args.reason)
    if changes.slot is None and changes.eaten_at is None and changes.note is None:
        return fail(
            "update_meal: for a multi-item meal give item_id to change portions/macros; "
            f"items: {[(i.id, i.name) for i in meal.items]}"
        )
    await _apply_meal_changes(ctx, meal, changes)
    await ctx.session.refresh(meal)
    state = await build_state(ctx, meal.day_date)
    return ok(
        {
            "meal_id": meal.id,
            "slot": meal.slot.value,
            "date": meal.day_date.isoformat(),
            "note": meal.note,
            "items": [_item_dict(i) for i in meal.items],
            "meal_total": macros_dict(repo.meal_macros(meal)),
            "day": state_numbers(state),
        }
    )


async def delete_meal(ctx: ToolContext, args: schemas.DeleteMealInput) -> ToolResult:
    meal = await repo.get_meal(ctx.session, ctx.user_id, args.meal_id)
    if meal is None:
        return fail(f"meal {args.meal_id} not found or already deleted")
    await repo.soft_delete_meal(ctx.session, ctx.user_id, args.meal_id, now=ctx.clock.now())
    state = await build_state(ctx, meal.day_date)
    return ok(
        {
            "deleted_meal_id": meal.id,
            "removed": [i.name for i in meal.items],
            "removed_total": macros_dict(repo.meal_macros(meal)),
            "day": state_numbers(state),
        }
    )


async def undo_last(ctx: ToolContext, args: schemas.UndoLastInput) -> ToolResult:
    meal = await repo.last_meal(ctx.session, ctx.user_id)
    if meal is None:
        return fail("nothing to undo: no logged meals")
    await repo.soft_delete_meal(ctx.session, ctx.user_id, meal.id, now=ctx.clock.now())
    state = await build_state(ctx, meal.day_date)
    return ok(
        {
            "undone_meal_id": meal.id,
            "slot": meal.slot.value,
            "removed": [i.name for i in meal.items],
            "removed_total": macros_dict(repo.meal_macros(meal)),
            "day": state_numbers(state),
        }
    )
