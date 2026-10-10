"""``/streak``: consecutive coaching days on target. A pure read, no model call.

A day counts when kcal, protein, fat and carbs are each within ``STREAK_TOLERANCE`` of the active
targets. Today counts only once it is closed; an open today neither counts nor breaks the streak.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.types import Macros
from strikt.db import repo
from strikt.db.models import User
from strikt.telegram.copy import resolve_lang, t

STREAK_TOLERANCE = 0.10
FIRST_LOOKBACK_DAYS = 32
_EPS = 1e-9  # 10% of 210 g is 21.000000000000004 in floats; exactly 10% is inside


def _pairs(totals: Macros, targets: Macros) -> tuple[tuple[float, float], ...]:
    return (
        (totals.kcal, targets.kcal),
        (totals.protein_g, targets.protein_g),
        (totals.fat_g, targets.fat_g),
        (totals.carbs_g, targets.carbs_g),
    )


def has_targets(targets: Macros) -> bool:
    return all(target > 0 for _, target in _pairs(targets, targets))


def day_in_range(totals: Macros, targets: Macros) -> bool:
    """Each of kcal, protein, fat and carbs within 10% of its target, both ends inclusive."""
    return has_targets(targets) and all(
        abs(actual - target) <= target * STREAK_TOLERANCE + _EPS
        for actual, target in _pairs(totals, targets)
    )


def count_streak(
    totals: Mapping[date, Macros], targets: Macros, *, today: date, today_closed: bool
) -> int:
    """Qualifying days in a row, back from today (closed) or yesterday (today still open).
    ``totals`` holds the days with food; a missing day breaks the streak."""
    day = today if today_closed else today - timedelta(days=1)
    count = 0
    while day in totals and day_in_range(totals[day], targets):
        count += 1
        day -= timedelta(days=1)
    return count


async def _day_totals(
    session: AsyncSession, user_id: int, date_from: date, date_to: date
) -> dict[date, Macros]:
    totals: dict[date, Macros] = {}
    for meal in await repo.list_meals_range(session, user_id, date_from, date_to):
        totals[meal.day_date] = totals.get(meal.day_date, Macros.zero()) + repo.meal_macros(meal)
    return totals


async def current_streak(
    session: AsyncSession, user_id: int, *, today: date, targets: Macros
) -> int:
    """``count_streak`` over the stored meals; reads a wider window only while the streak fills it."""
    if not has_targets(targets):
        return 0
    day = await repo.get_day(session, user_id, today)
    closed = day is not None and day.closed_at is not None
    span = FIRST_LOOKBACK_DAYS
    while True:
        totals = await _day_totals(session, user_id, today - timedelta(days=span), today)
        count = count_streak(totals, targets, today=today, today_closed=closed)
        if count < span:
            return count
        span *= 2


async def streak_text(session: AsyncSession, user: User, *, today: date) -> str:
    """The one-line reply; ``today`` is the caller's ``coaching_today``."""
    lang = resolve_lang(user.language)
    targets = repo.protocol_targets(await repo.get_active_protocol(session, user.id))
    if not has_targets(targets):
        return t(lang, "streak.no_targets")
    count = await current_streak(session, user.id, today=today, targets=targets)
    if count == 0:
        return t(lang, "streak.zero")
    return t(lang, "streak.days", days=count)
