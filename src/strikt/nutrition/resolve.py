"""Food resolution (PLAN §5 ``resolve.py``): cache → Open Food Facts (barcode) → USDA → None.

``resolve_food`` never raises into the agent: any exception is logged as a structlog warning and
the function returns ``None`` (the agent then falls back to ``web_research`` or its own estimate
and stores that into ``foods`` with ``source=web|model``). Every network hit is upserted into the
``foods`` cache with its source and source URL; a stale cache row is served when the network
lookup fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.config import Settings, get_settings
from strikt.core.types import FoodHit
from strikt.nutrition import off, usda
from strikt.nutrition.store import CacheHit, cache_hit, get_cached

log = structlog.get_logger(__name__)

HTTP_TIMEOUT_S: Final[float] = 6.0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True)


async def _lookup_network(
    client: httpx.AsyncClient,
    query: str,
    *,
    brand: str | None,
    barcode: str | None,
    settings: Settings,
) -> FoodHit | None:
    user_agent = getattr(
        settings, "off_user_agent", "Strikt/0.1 (https://github.com/magik-ai/bomiso)"
    )
    key_secret = getattr(settings, "usda_api_key", None)
    api_key = key_secret.get_secret_value() if key_secret is not None else None
    if barcode:
        hit = await off.fetch_product(client, barcode, user_agent=user_agent)
        if hit is not None:
            return hit
        hit = await usda.search_food(client, query, api_key=api_key, barcode=barcode)
        if hit is not None:
            return hit
    if query.strip():
        return await usda.search_food(client, query, api_key=api_key, brand=brand)
    return None


async def resolve_food(
    session: AsyncSession,
    query: str,
    *,
    brand: str | None = None,
    restaurant: str | None = None,
    barcode: str | None = None,
    http: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> FoodHit | None:
    """Resolve ``query`` (and/or ``barcode``) to per-100 g macros, caching every network hit.

    Order: ``foods`` cache (fresh) → OFF by barcode → USDA Branded by GTIN → USDA generic search
    (Foundation/SR Legacy, then FNDDS, then Branded with the brand in the query) → stale cache
    row → ``None``. Restaurant dishes are cache-only here (the agent researches them on the web).
    """
    now = now or datetime.now(UTC)
    settings = settings or get_settings()
    try:
        cached: CacheHit | None = await get_cached(
            session, name=query, brand=brand, restaurant=restaurant, barcode=barcode, now=now
        )
        if cached is not None and not cached.stale:
            return cached.hit
        if restaurant and not barcode:
            return cached.hit if cached is not None else None
        if http is None:
            async with _client() as client:
                hit = await _lookup_network(
                    client, query, brand=brand, barcode=barcode, settings=settings
                )
        else:
            hit = await _lookup_network(
                http, query, brand=brand, barcode=barcode, settings=settings
            )
        if hit is not None:
            if restaurant:
                hit = hit.model_copy(update={"restaurant": restaurant})
            await cache_hit(session, hit, now=now)
            return hit
        if cached is not None:
            log.info("resolve.stale_cache", query=query, barcode=barcode, source=cached.hit.source)
            return cached.hit
        return None
    except Exception as exc:
        log.warning(
            "resolve.failed",
            query=query,
            barcode=barcode,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None
