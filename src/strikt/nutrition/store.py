"""The ``foods`` cache seen from the nutrition engine (PLAN §3 ``foods``, research/06 §8).

Reads go through ``strikt.db.repo`` where a query exists (key, barcode); the name-search fallback
is a SQLAlchemy query defined here. TTLs: OFF 90 days, USDA 365 days; rows written by the agent
(``web``/``model``/``user``/``label``) never expire on their own — the agent overwrites them.
Stale rows are still returned as ``stale=True`` so the resolver can serve them when the network
is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc
from strikt.core.types import FoodHit, Macros
from strikt.db import repo
from strikt.db.models import Food, ItemSource

TTL_BY_SOURCE: Final[dict[str, timedelta]] = {
    "off": timedelta(days=90),
    "usda": timedelta(days=365),
}
NAME_SEARCH_LIMIT: Final[int] = 5


@dataclass(frozen=True, slots=True)
class CacheHit:
    hit: FoodHit
    stale: bool


def food_to_hit(food: Food) -> FoodHit:
    return FoodHit(
        name=food.name,
        brand=food.brand,
        restaurant=food.restaurant,
        barcode=food.barcode,
        per_100g=Macros.model_validate(food.per_100g),
        serving_g=food.serving_g,
        serving_desc=food.serving_desc,
        source=food.source.value,
        source_url=food.source_url,
        confidence=food.confidence,
    )


def is_fresh(food: Food, now: datetime) -> bool:
    ttl = TTL_BY_SOURCE.get(food.source.value)
    if ttl is None:
        return True
    return ensure_utc(food.fetched_at) + ttl > ensure_utc(now)


async def search_by_name(
    session: AsyncSession, query: str, *, limit: int = NAME_SEARCH_LIMIT
) -> list[Food]:
    """Case-insensitive substring match on ``foods.name`` (exact name first, then shortest)."""
    needle = query.strip().casefold()
    if not needle:
        return []
    stmt = (
        select(Food)
        .where(func.lower(Food.name).contains(needle))
        .order_by(func.length(Food.name), Food.fetched_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def get_cached(
    session: AsyncSession,
    *,
    name: str,
    brand: str | None = None,
    restaurant: str | None = None,
    barcode: str | None = None,
    now: datetime | None = None,
) -> CacheHit | None:
    """Cache lookup: barcode → exact key (name+brand+restaurant) → name substring."""
    now = now or datetime.now(UTC)
    food: Food | None = None
    if barcode:
        food = await repo.get_food_by_barcode(session, barcode)
    if food is None and name.strip():
        food = await repo.get_food_by_key(session, repo.make_food_key(name, brand, restaurant))
    if food is None and name.strip() and not barcode:
        candidates = await search_by_name(session, name)
        if brand:
            wanted = brand.casefold()
            candidates = [
                c for c in candidates if (c.brand or "").casefold() == wanted
            ] or candidates
        food = candidates[0] if candidates else None
    if food is None:
        return None
    return CacheHit(hit=food_to_hit(food), stale=not is_fresh(food, now))


async def cache_hit(session: AsyncSession, hit: FoodHit, *, now: datetime | None = None) -> Food:
    """Upsert a resolved hit into ``foods`` (flushes; the caller commits)."""
    return await repo.upsert_food(
        session,
        name=hit.name,
        per_100g=hit.per_100g,
        source=ItemSource(hit.source),
        fetched_at=now or datetime.now(UTC),
        brand=hit.brand,
        restaurant=hit.restaurant,
        barcode=hit.barcode,
        serving_g=hit.serving_g,
        serving_desc=hit.serving_desc,
        source_url=hit.source_url,
        confidence=hit.confidence,
    )
