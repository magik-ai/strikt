"""The day's food as one list: ``set_day_food`` and ``save_my_food``.

A month of the owner's chat (1085 turns) showed where the per-item tools broke: nineteen edits
landed on the wrong item (a meal id passed as an item id hit food from two weeks earlier), plans
were logged as meals and never removed, and the sanity layer rewrote 72 items after the model had
named its numbers, so the reply said one figure and the database held another. This module
replaces all of that for the coach:

* ``set_day_food`` takes the COMPLETE list of what was eaten on a day and makes it the day's food.
  There are no ids to get wrong: the model always sends the whole list, the code works out what
  was added, removed and changed, and says so in the result (and in the stored ``<actions>``).
  Numbers are stored exactly as given. The checks that used to rewrite them now only speak: a
  kcal figure that does not match its macros, fiber 0 on an avocado, the same dish twice.
* Plans ("возьму салат на обед") go to ``planned`` - kept on the day, shown every turn, never
  counted.
* ``save_my_food`` keeps the user's regular foods with fixed numbers (a ``food`` note), shown in
  ``<my_foods>`` every turn, so the same shake stops getting a new number every week.

A day that changes after it was closed is reopened: the nightly job closes it again with a verdict
written from the new numbers, instead of the old verdict living on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.tools.common import build_state, fail, ok, rnd, state_numbers, to_utc
from strikt.core.clock import ensure_utc
from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import MealItem, MealSlot, MealSource, NoteKind
from strikt.memory.daystate import FOOD_PLAN_KEY
from strikt.nutrition.math import computed_kcal

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult
    from strikt.db.models import Meal

log = structlog.get_logger(__name__)

PLAN_KEY = FOOD_PLAN_KEY
#: A kcal figure further than this from 4/4/9 (+7 alcohol) gets a note (never a correction).
KCAL_NOTE_REL = 0.15
KCAL_NOTE_ABS = 25.0
#: Hours after midnight that still belong to the evening's coaching day for an explicit time.
NIGHT_HOURS = 6
#: How far back a day's list may be rewritten (older days are history, not a log).
MAX_DAYS_BACK = 14

_FIBER_FOODS = re.compile(
    r"(?i)(авокад|avocad|капуст|брюссел|brussel|брокол|brocc|фасол|bean|чечевиц|lentil|нут\b"
    r"|chickpea|хумус|hummus|горох|pea\b|овощ|vegetab|кабач|цукин|zucchin|баклажан|eggplant"
    r"|артишок|artichok|шпинат|spinach|кейл|kale|руккол|аругул|arugul|ягод|berr|малин|ежевик"
    r"|черник|клубник|псил|psyll|чиа|chia|льн|flax|отруб|bran|овсян|oat|гречк|buckwheat"
    r"|перловк|barley|цельнозерн|whole ?grain|морков|carrot|грибы|mushroom|яблок|apple|груш"
    r"|pear|киви|kiwi|эдамам|edamame|чили|chili|салат)"
)


def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s%.]", " ", name.casefold()).split())


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    match = re.match(r"^\s*(\d{1,2})[:.](\d{2})", value)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def _eaten_at(day: date, at: time, tz: str) -> datetime:
    """Local ``at`` on coaching day ``day``: 01:30 is the night after ``day``, not its morning."""
    calendar = day + timedelta(days=1) if at.hour < NIGHT_HOURS else day
    return to_utc(datetime.combine(calendar, at), tz)


def _label(name: str, portion: str | None) -> str:
    return f"{name} ({portion})" if portion else name


def _numbers(m: Macros) -> str:
    text = f"{rnd(m.kcal, 0)} kcal P{rnd(m.protein_g)} C{rnd(m.carbs_g)} F{rnd(m.fat_g)}"
    text += f" fib{rnd(m.fiber_g)}"
    if m.alcohol_g:
        text += f" alc{rnd(m.alcohol_g)}"
    return text


def _item_macros(item: schemas.DayFoodItem) -> Macros:
    return Macros(
        kcal=item.kcal,
        protein_g=item.protein_g,
        carbs_g=item.carbs_g,
        fat_g=item.fat_g,
        fiber_g=item.fiber_g,
        alcohol_g=item.alcohol_g,
    )


def _differs(a: Macros, b: Macros) -> bool:
    return (
        abs(a.kcal - b.kcal) >= 1
        or abs(a.protein_g - b.protein_g) >= 0.5
        or abs(a.carbs_g - b.carbs_g) >= 0.5
        or abs(a.fat_g - b.fat_g) >= 0.5
        or abs(a.fiber_g - b.fiber_g) >= 0.5
    )


def advisory_notes(items: Sequence[schemas.DayFoodItem]) -> list[str]:
    """What looks off in the list. Said to the model; nothing is changed."""
    notes: list[str] = []
    seen: dict[str, int] = {}
    for item in items:
        m = _item_macros(item)
        derived = computed_kcal(m)
        if abs(derived - m.kcal) > max(KCAL_NOTE_ABS, KCAL_NOTE_REL * max(m.kcal, derived)):
            notes.append(
                f"{item.name}: {rnd(m.kcal, 0)} kcal but its macros give {rnd(derived, 0)}"
                " (4/4/9) - check which one is right"
            )
        if m.fiber_g <= 0 and _FIBER_FOODS.search(item.name):
            notes.append(f"{item.name}: fiber 0 - this food has fiber, estimate it")
        key = _norm(item.name)
        seen[key] = seen.get(key, 0) + 1
    notes += [
        f"'{name}' is in the list {count} times - really {count} portions?"
        for name, count in seen.items()
        if count > 1
    ]
    return notes


def _old_line(row: MealItem) -> tuple[str, Macros, str | None]:
    return row.name, repo.item_macros(row), row.unit


def diff_lists(
    old: Sequence[MealItem], new: Sequence[schemas.DayFoodItem]
) -> tuple[list[str], list[str], list[str], int]:
    """(added, removed, changed, unchanged count) between the stored list and the new one."""
    pool: dict[str, list[MealItem]] = {}
    for row in old:
        pool.setdefault(_norm(row.name), []).append(row)
    added: list[str] = []
    changed: list[str] = []
    unchanged = 0
    for item in new:
        candidates = pool.get(_norm(item.name))
        m = _item_macros(item)
        if not candidates:
            added.append(f"{_label(item.name, item.portion)}: {_numbers(m)}")
            continue
        row = candidates.pop(0)
        before = repo.item_macros(row)
        if _differs(before, m) or (item.portion or None) != (row.unit or None):
            changed.append(
                f"{_label(item.name, item.portion)}: {_numbers(before)} -> {_numbers(m)}"
            )
        else:
            unchanged += 1
    removed = [
        f"{_label(row.name, row.unit)}: {_numbers(repo.item_macros(row))}"
        for rows in pool.values()
        for row in rows
    ]
    return added, removed, changed, unchanged


def _meal_source(ctx: ToolContext) -> MealSource:
    incoming = ctx.incoming
    if incoming is None:
        return MealSource.text
    kinds = {a.kind for a in incoming.attachments}
    if incoming.forwarded_from:
        return MealSource.forwarded
    if "image" in kinds or "document" in kinds:
        return MealSource.photo
    if "voice" in kinds:
        return MealSource.voice
    return MealSource.text


def _groups(
    items: Sequence[schemas.DayFoodItem],
) -> list[tuple[str, list[schemas.DayFoodItem]]]:
    """Consecutive items of one slot form one meal (the card and the history read meals)."""
    groups: list[tuple[str, list[schemas.DayFoodItem]]] = []
    for item in items:
        if groups and groups[-1][0] == item.slot:
            groups[-1][1].append(item)
        else:
            groups.append((item.slot, [item]))
    return groups


async def set_day_food(ctx: ToolContext, args: schemas.SetDayFoodInput) -> ToolResult:
    today = ctx.local_date
    day = args.date or today
    if day > today + timedelta(days=1):
        return fail(f"set_day_food: {day} is in the future; food goes on the day it was eaten")
    if day < today - timedelta(days=MAX_DAYS_BACK):
        return fail(f"set_day_food: {day} is more than {MAX_DAYS_BACK} days ago")
    now = ctx.clock.now()
    old_meals: list[Meal] = await repo.list_meals_for_date(ctx.session, ctx.user_id, day)
    old_items = [row for meal in old_meals for row in meal.items]
    if not args.eaten and old_items and not args.clear_day:
        return fail(
            f"set_day_food: `eaten` is empty, which would remove all {len(old_items)} items of"
            f" {day}. Send the full list with the change; set clear_day only if the user asked"
            " to wipe the day."
        )

    added, removed, changed, unchanged = diff_lists(old_items, args.eaten)
    # keep the time a dish was eaten when the model did not restate it
    old_times: dict[str, datetime] = {}
    for meal in old_meals:
        when = meal.eaten_at or meal.logged_at
        for row in meal.items:
            old_times.setdefault(_norm(row.name), ensure_utc(when))
    default_at = now if day == today else to_utc(datetime.combine(day, time(12, 0)), ctx.tz)

    for meal in old_meals:
        await repo.soft_delete_meal(ctx.session, ctx.user_id, meal.id, now=now)
    source = _meal_source(ctx)
    raw_ref: dict[str, Any] | None = None
    if ctx.incoming is not None:
        raw_ref = {"message_id": ctx.incoming.message_id, "via": "set_day_food"}
    for slot, group in _groups(args.eaten):
        first = group[0]
        at = _parse_time(first.time)
        if at is not None:
            eaten_at = _eaten_at(day, at, ctx.tz)
        else:
            eaten_at = old_times.get(_norm(first.name), default_at)
        await repo.add_meal_with_items(
            ctx.session,
            ctx.user_id,
            day_date=day,
            items=[
                FoodItemIn(
                    name=item.name,
                    unit=(item.portion or None) and item.portion[:32],
                    grams=item.grams,
                    macros=_item_macros(item),
                    source=item.source,
                    confidence=0.9 if item.source in {"label", "user"} else 0.7,
                )
                for item in group
            ],
            slot=MealSlot(slot),
            source=source,
            logged_at=now,
            eaten_at=eaten_at,
            raw_ref=raw_ref,
            note=args.change[:500] if args.change else None,
        )

    day_row = await repo.get_or_open_day(ctx.session, ctx.user_id, day, now=now)
    if args.planned is not None:
        plan = dict(day_row.plan or {})
        if args.planned:
            plan[PLAN_KEY] = [" ".join(p.split()) for p in args.planned if p.strip()]
        else:
            plan.pop(PLAN_KEY, None)
        day_row.plan = plan or None
    reopened = day_row.closed_at is not None and bool(added or removed or changed)
    if reopened:
        # the nightly job closes it again with a verdict written from these numbers
        day_row.closed_at = None
        day_row.verdict = None
    await ctx.session.flush()

    state = await build_state(ctx, day)
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "change": args.change,
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "eaten": [
            f"{item.slot} {_label(item.name, item.portion)}: {_numbers(_item_macros(item))}"
            for item in args.eaten
        ],
        "planned": list((day_row.plan or {}).get(PLAN_KEY) or []),
        "day": state_numbers(state),
    }
    notes = advisory_notes(args.eaten)
    if notes:
        result["check"] = notes
    if removed:
        result["note_on_removed"] = (
            "these lines are gone from the day; if the user did not ask to remove them, send the"
            " list again with them"
        )
    if reopened:
        result["reopened"] = "the day was closed; it is open again and will be re-closed tonight"
    log.info(
        "day_food_set",
        user_id=ctx.user_id,
        day=str(day),
        items=len(args.eaten),
        added=len(added),
        removed=len(removed),
        changed=len(changed),
    )
    return ok(result)


def food_note_text(args: schemas.SaveMyFoodInput) -> str:
    m = Macros(
        kcal=args.kcal,
        protein_g=args.protein_g,
        carbs_g=args.carbs_g,
        fat_g=args.fat_g,
        fiber_g=args.fiber_g,
    )
    return f"{' '.join(args.name.split())} | {' '.join(args.portion.split())} | {_numbers(m)}"


async def save_my_food(ctx: ToolContext, args: schemas.SaveMyFoodInput) -> ToolResult:
    if not args.name.strip() or not args.portion.strip():
        return fail("save_my_food: give a name and the portion the numbers are for")
    text = food_note_text(args)
    now = ctx.clock.now()
    if args.replaces_id is not None:
        old = await repo.get_note(ctx.session, ctx.user_id, args.replaces_id)
        if old is None or old.kind != NoteKind.food:
            return fail(f"save_my_food: saved food {args.replaces_id} not found")
        note = await repo.supersede_note(
            ctx.session, ctx.user_id, args.replaces_id, text=text, confidence=1.0, now=now
        )
        if note is None:
            return fail(f"save_my_food: saved food {args.replaces_id} not found")
    else:
        existing = await repo.list_active_notes(ctx.session, ctx.user_id, kinds=[NoteKind.food])
        same = next((n for n in existing if _norm(n.text.split("|")[0]) == _norm(args.name)), None)
        if same is not None:
            note = await repo.supersede_note(
                ctx.session, ctx.user_id, same.id, text=text, confidence=1.0, now=now
            )
            if note is None:
                return fail("save_my_food: could not replace the saved food")
        else:
            note = await repo.add_note(
                ctx.session, ctx.user_id, kind=NoteKind.food, text=text, confidence=1.0, now=now
            )
    log.info("my_food_saved", user_id=ctx.user_id, note_id=note.id)
    return ok({"saved_food_id": note.id, "text": text})
