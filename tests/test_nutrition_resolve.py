"""nutrition.resolve / off / usda / store with httpx.MockTransport (no network).

Response shapes are copied from research/06 §1.1, §1.3 (OFF v3) and §2.3–2.4 (USDA live search).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.config import Settings
from strikt.core.types import FoodHit, Macros
from strikt.db import repo
from strikt.nutrition import off, usda
from strikt.nutrition.resolve import resolve_food
from strikt.nutrition.store import cache_hit, get_cached, is_fresh, search_by_name

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
NUTELLA = "3017620422003"

OFF_V3_NUTELLA: dict[str, Any] = {
    "code": NUTELLA,
    "status": "success",
    "result": {"id": "product_found", "name": "Product found"},
    "errors": [],
    "warnings": [],
    "product": {
        "product_name": "Nutella",
        "brands": "Ferrero,Nutella",
        "serving_size": "15 g",
        "product_quantity": "400",
        "nutrition_data_per": "100g",
        "completeness": 0.7875,
        "nutriments": {
            "energy-kcal_100g": 539,
            "energy-kj_100g": 2252,
            "energy_100g": 2252,
            "energy_unit": "kJ",
            "proteins_100g": 6.3,
            "carbohydrates_100g": 57.5,
            "sugars_100g": 56.3,
            "fat_100g": 30.9,
            "saturated-fat_100g": 10.6,
            "sodium_100g": 0.0428,
            "salt_100g": 0.107,
        },
        "nutriments_estimated": {"fiber_100g": 3.675},
    },
}

OFF_V3_NOT_FOUND: dict[str, Any] = {
    "code": "0000000000000",
    "status": "failure",
    "result": {"id": "product_not_found", "name": "Product not found"},
    "errors": [{"message": {"id": "product_not_found"}}],
    "warnings": [],
}


def usda_nutrient(nid: int, number: str, name: str, unit: str, value: float) -> dict[str, Any]:
    return {
        "nutrientId": nid,
        "nutrientName": name,
        "nutrientNumber": number,
        "unitName": unit,
        "derivationCode": "A",
        "derivationDescription": "Analytical",
        "value": value,
    }


USDA_AVOCADO: dict[str, Any] = {
    "fdcId": 171705,
    "description": "Avocados, raw, all commercial varieties",
    "dataType": "SR Legacy",
    "foodCategory": "Fruits and Fruit Juices",
    "foodNutrients": [
        usda_nutrient(1008, "208", "Energy", "KCAL", 160),
        usda_nutrient(1003, "203", "Protein", "G", 2.0),
        usda_nutrient(1004, "204", "Total lipid (fat)", "G", 14.66),
        usda_nutrient(1005, "205", "Carbohydrate, by difference", "G", 8.53),
        usda_nutrient(1079, "291", "Fiber, total dietary", "G", 6.7),
        usda_nutrient(1093, "307", "Sodium, Na", "MG", 7.0),
    ],
}
USDA_AVOCADO_OIL: dict[str, Any] = {
    "fdcId": 173573,
    "description": "Oil, avocado",
    "dataType": "SR Legacy",
    "foodNutrients": [
        usda_nutrient(1008, "208", "Energy", "KCAL", 884),
        usda_nutrient(1003, "203", "Protein", "G", 0),
        usda_nutrient(1004, "204", "Total lipid (fat)", "G", 100),
        usda_nutrient(1005, "205", "Carbohydrate, by difference", "G", 0),
    ],
}
USDA_GREEK_YOGURT: dict[str, Any] = {
    "fdcId": 2_000_001,
    "description": "GREEK YOGURT, PLAIN",
    "dataType": "Branded",
    "brandOwner": "CHOBANI, LLC",
    "brandName": "CHOBANI",
    "gtinUpc": "0818290014405",
    "servingSize": 150,
    "servingSizeUnit": "g",
    "householdServingFullText": "1 cup",
    "foodNutrients": [
        usda_nutrient(1008, "208", "Energy", "KCAL", 59),
        usda_nutrient(1003, "203", "Protein", "G", 10.2),
        usda_nutrient(1004, "204", "Total lipid (fat)", "G", 0.4),
        usda_nutrient(1005, "205", "Carbohydrate, by difference", "G", 3.6),
        usda_nutrient(1093, "307", "Sodium, Na", "MG", 36),
    ],
}


class Router:
    """Routes OFF and USDA requests to canned responses; records every request."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.off_products: dict[str, dict[str, Any]] = {NUTELLA: OFF_V3_NUTELLA}
        self.usda_by_type: dict[str, list[dict[str, Any]]] = {
            "Foundation,SR Legacy": [USDA_AVOCADO_OIL, USDA_AVOCADO],
            "Survey (FNDDS)": [],
            "Branded": [USDA_GREEK_YOGURT],
        }
        self.off_v3_status: int | None = None  # force a v3 failure (e.g. 503 HTML)
        self.usda_status: int | None = None
        self.raise_transport = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.raise_transport:
            raise httpx.ConnectError("boom", request=request)
        host, path = request.url.host, request.url.path
        if host == "world.openfoodfacts.org":
            return self._off(request, path)
        if host == "api.nal.usda.gov":
            return self._usda(request)
        return httpx.Response(404, json={"error": f"no route for {host}{path}"})

    def _off(self, request: httpx.Request, path: str) -> httpx.Response:
        barcode = path.rsplit("/", 1)[-1].removesuffix(".json")
        if "/api/v3/" in path and self.off_v3_status is not None:
            return httpx.Response(
                self.off_v3_status,
                text="<html>Page temporarily unavailable</html>",
                headers={"content-type": "text/html"},
            )
        product = self.off_products.get(barcode)
        if product is None:
            return httpx.Response(404, json={**OFF_V3_NOT_FOUND, "code": barcode})
        if "/api/v2/" in path:
            return httpx.Response(
                200,
                json={
                    "code": barcode,
                    "status": 1,
                    "status_verbose": "product found",
                    "product": product["product"],
                },
            )
        return httpx.Response(200, json=product)

    def _usda(self, request: httpx.Request) -> httpx.Response:
        if self.usda_status is not None:
            body = {"error": {"code": "OVER_RATE_LIMIT", "message": "rate limit"}}
            return httpx.Response(
                self.usda_status, json=body, headers={"x-ratelimit-remaining": "0"}
            )
        data_type = request.url.params.get("dataType", "")
        query = request.url.params.get("query", "")
        foods = self.usda_by_type.get(data_type, [])
        if data_type == "Branded":
            foods = [
                f
                for f in foods
                if query.lstrip("0") in str(f.get("gtinUpc", "")).lstrip("0")
                or query.lower().split()[-1] in str(f["description"]).lower()
            ]
        return httpx.Response(
            200, json={"totalHits": len(foods), "currentPage": 1, "totalPages": 1, "foods": foods}
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def requests_to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.calls if r.url.host == host]


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def usda_settings() -> Settings:
    return Settings(_env_file=None, usda_api_key=SecretStr("test-key-123"))


# ------------------------------------------------------------------------------------ off


def test_off_parse_product_maps_nutriments_per_100g() -> None:
    hit = off.parse_product(OFF_V3_NUTELLA, NUTELLA)
    assert hit is not None
    assert hit.name == "Nutella"
    assert hit.brand == "Ferrero"
    assert hit.barcode == NUTELLA
    assert hit.source == "off"
    assert hit.source_url == "https://world.openfoodfacts.org/product/3017620422003"
    assert hit.per_100g.kcal == 539
    assert hit.per_100g.protein_g == 6.3
    assert hit.per_100g.carbs_g == 57.5
    assert hit.per_100g.fat_g == 30.9
    assert hit.per_100g.sodium_mg == pytest.approx(42.8)
    assert hit.per_100g.fiber_g == pytest.approx(3.675)  # estimated fibre used as last resort
    assert hit.serving_g == 15
    assert hit.serving_desc == "15 g"
    assert 0.6 < hit.confidence < 0.95


def test_off_parse_product_falls_back_to_kj_and_salt() -> None:
    payload = json.loads(json.dumps(OFF_V3_NUTELLA))
    nutriments = payload["product"]["nutriments"]
    del nutriments["energy-kcal_100g"], nutriments["sodium_100g"]
    del payload["product"]["nutriments_estimated"]
    hit = off.parse_product(payload, NUTELLA)
    assert hit is not None
    assert hit.per_100g.kcal == pytest.approx(2252 / 4.184)
    assert hit.per_100g.sodium_mg == pytest.approx(0.107 / 2.5 * 1000)
    assert hit.per_100g.fiber_g == 0


def test_off_parse_product_computes_energy_when_absent() -> None:
    payload = json.loads(json.dumps(OFF_V3_NUTELLA))
    for key in ("energy-kcal_100g", "energy-kj_100g", "energy_100g"):
        del payload["product"]["nutriments"][key]
    hit = off.parse_product(payload, NUTELLA)
    assert hit is not None
    assert hit.per_100g.kcal == pytest.approx(6.3 * 4 + 57.5 * 4 + 30.9 * 9 + 3.675 * 2)


def test_off_parse_product_rejects_implausible_and_missing() -> None:
    assert off.parse_product(OFF_V3_NOT_FOUND, "0") is None
    assert off.parse_product({"status": "success", "product": {}}, "1") is None
    bad = json.loads(json.dumps(OFF_V3_NUTELLA))
    bad["product"]["nutriments"]["fat_100g"] = 130
    assert off.parse_product(bad, NUTELLA) is None
    partial = json.loads(json.dumps(OFF_V3_NUTELLA))
    del partial["product"]["nutriments"]["proteins_100g"]
    assert off.parse_product(partial, NUTELLA) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("38g", 38.0),
        ("250 ml", 250.0),
        ("2 biscuits (30 g)", 30.0),
        ("1 portion", None),
        (None, None),
        ("15,5 g", 15.5),
    ],
)
def test_off_parse_serving_size(text: str | None, expected: float | None) -> None:
    assert off.parse_serving_size(text) == expected


async def test_off_fetch_product_sends_user_agent_and_uses_v3(router: Router) -> None:
    async with router.client() as client:
        hit = await off.fetch_product(client, NUTELLA, user_agent="Strikt/0.1 (test@example.com)")
    assert hit is not None
    assert hit.name == "Nutella"
    request = router.calls[0]
    assert request.headers["user-agent"] == "Strikt/0.1 (test@example.com)"
    assert request.url.path == f"/api/v3/product/{NUTELLA}"
    assert "fields=" in str(request.url)
    assert request.extensions["timeout"]["connect"] == 6.0


async def test_off_fetch_product_not_found_returns_none_without_retry(router: Router) -> None:
    async with router.client() as client:
        assert await off.fetch_product(client, "0000000000000", user_agent="ua") is None
    assert len(router.calls) == 1


async def test_off_fetch_product_falls_back_to_v2_on_html_503(router: Router) -> None:
    router.off_v3_status = 503
    async with router.client() as client:
        hit = await off.fetch_product(client, NUTELLA, user_agent="ua")
    assert hit is not None
    assert [r.url.path for r in router.calls] == [
        f"/api/v3/product/{NUTELLA}",
        f"/api/v2/product/{NUTELLA}.json",
    ]


async def test_off_fetch_product_transport_error_returns_none(router: Router) -> None:
    router.raise_transport = True
    async with router.client() as client:
        assert await off.fetch_product(client, NUTELLA, user_agent="ua") is None
    assert len(router.calls) == 2  # v3 then v2, both failed gracefully


async def test_off_fetch_product_strips_non_digits(router: Router) -> None:
    async with router.client() as client:
        assert await off.fetch_product(client, "", user_agent="ua") is None
        hit = await off.fetch_product(client, "3017 6204 22003", user_agent="ua")
    assert hit is not None and hit.barcode == NUTELLA


# ----------------------------------------------------------------------------------- usda


def test_usda_extract_nutrients_search_shape() -> None:
    values = usda.extract_nutrients(USDA_AVOCADO["foodNutrients"])
    assert values == {
        "kcal": 160,
        "protein_g": 2.0,
        "fat_g": 14.66,
        "carbs_g": 8.53,
        "fiber_g": 6.7,
        "sodium_mg": 7.0,
    }


def test_usda_extract_nutrients_detail_and_abridged_shapes() -> None:
    detail = [
        {
            "type": "FoodNutrient",
            "id": 1,
            "nutrient": {"id": 1008, "number": "208", "name": "Energy", "unitName": "kcal"},
            "amount": 89,
        },
        {
            "type": "FoodNutrient",
            "id": 2,
            "nutrient": {"id": 1003, "number": "203", "name": "Protein", "unitName": "g"},
            "amount": 1.1,
        },
    ]
    abridged = [
        {"number": "204", "name": "Total lipid (fat)", "amount": 0.3, "unitName": "G"},
        {"number": "205", "name": "Carbohydrate", "amount": 22.8, "unitName": "G"},
    ]
    assert usda.extract_nutrients(detail) == {"kcal": 89, "protein_g": 1.1}
    assert usda.extract_nutrients(abridged) == {"fat_g": 0.3, "carbs_g": 22.8}
    assert usda.extract_nutrients(None) == {}
    assert usda.extract_nutrients([{"nutrientId": 1008}, "junk"]) == {}


def test_usda_macros_energy_fallbacks() -> None:
    base = {"protein_g": 10.0, "fat_g": 5.0, "carbs_g": 20.0}
    assert usda.macros_from_nutrients({**base, "kcal": 170.0}) is not None
    atwater = usda.macros_from_nutrients({**base, "kcal_atwater": 168.0})
    assert atwater is not None and atwater.kcal == 168
    kj = usda.macros_from_nutrients({**base, "kj": 4184.0})
    assert kj is not None and kj.kcal == pytest.approx(1000)
    computed = usda.macros_from_nutrients(base)
    assert computed is not None and computed.kcal == 165
    assert usda.macros_from_nutrients({"protein_g": 1.0}) is None


def test_usda_parse_search_food_branded() -> None:
    hit = usda.parse_search_food(USDA_GREEK_YOGURT)
    assert hit is not None
    assert hit.name == "Greek yogurt, plain"
    assert hit.brand == "Chobani"
    assert hit.barcode == "0818290014405"
    assert hit.serving_g == 150
    assert hit.serving_desc == "1 cup"
    assert hit.source == "usda"
    assert hit.source_url == "https://fdc.nal.usda.gov/food-details/2000001/nutrients"
    assert hit.confidence == 0.85
    assert hit.per_100g.fiber_g == 0
    assert usda.parse_search_food({"description": "x", "foodNutrients": []}) is None


def test_usda_pick_best_prefers_word_overlap_then_shortest() -> None:
    assert usda.pick_best([USDA_AVOCADO_OIL, USDA_AVOCADO], "avocado") is USDA_AVOCADO
    assert usda.pick_best([USDA_AVOCADO_OIL, USDA_AVOCADO], "avocado oil") is USDA_AVOCADO_OIL
    assert usda.pick_best([], "x") is None


async def test_usda_search_food_uses_header_key_and_type_priority(router: Router) -> None:
    async with router.client() as client:
        hit = await usda.search_food(client, "avocado", api_key="k-1")
    assert hit is not None
    assert hit.name == "Avocados, raw, all commercial varieties"
    assert hit.confidence == 0.9
    request = router.calls[0]
    assert request.headers["x-api-key"] == "k-1"
    assert "api_key" not in str(request.url)
    assert request.url.params["dataType"] == "Foundation,SR Legacy"
    assert request.url.params["pageSize"] == "10"
    assert request.extensions["timeout"]["connect"] == 6.0
    assert len(router.calls) == 1


async def test_usda_search_food_falls_through_to_branded_with_brand(router: Router) -> None:
    router.usda_by_type["Foundation,SR Legacy"] = []
    async with router.client() as client:
        hit = await usda.search_food(client, "greek yogurt", api_key=None, brand="Chobani")
    assert hit is not None and hit.brand == "Chobani"
    types = [r.url.params["dataType"] for r in router.calls]
    assert types == ["Foundation,SR Legacy", "Survey (FNDDS)", "Branded"]
    assert router.calls[-1].url.params["query"] == "Chobani greek yogurt"
    assert router.calls[0].headers["x-api-key"] == usda.DEMO_KEY


async def test_usda_search_food_by_barcode_requires_gtin_match(router: Router) -> None:
    async with router.client() as client:
        hit = await usda.search_food(client, "", api_key="k", barcode="818290014405")
        miss = await usda.search_food(client, "", api_key="k", barcode="111111111111")
    assert hit is not None and hit.barcode == "0818290014405"
    assert miss is None
    assert all(r.url.params["dataType"] == "Branded" for r in router.calls)


async def test_usda_search_food_rate_limited_returns_none_and_stops(router: Router) -> None:
    router.usda_status = 429
    async with router.client() as client:
        assert await usda.search_food(client, "avocado", api_key="k") is None
    assert len(router.calls) == 1


async def test_usda_search_food_transport_error_and_empty_query(router: Router) -> None:
    router.raise_transport = True
    async with router.client() as client:
        assert await usda.search_food(client, "avocado", api_key="k") is None
        assert await usda.search_food(client, "   ", api_key="k") is None


# ---------------------------------------------------------------------------------- store


async def test_store_cache_roundtrip_and_name_search(session: AsyncSession) -> None:
    hit = FoodHit(
        name="Greek yogurt 0%",
        brand="Fage",
        per_100g=Macros(kcal=57, protein_g=10, carbs_g=3, fat_g=0),
        serving_g=170,
        source="label",
        source_url=None,
        confidence=0.95,
    )
    await cache_hit(session, hit, now=NOW)
    await session.commit()
    cached = await get_cached(session, name="Greek yogurt 0%", brand="Fage", now=NOW)
    assert cached is not None and not cached.stale
    assert cached.hit == hit
    by_name = await get_cached(session, name="yogurt", now=NOW)
    assert by_name is not None and by_name.hit.name == "Greek yogurt 0%"
    assert (
        await get_cached(session, name="yogurt", brand="Chobani", now=NOW) is not None
    )  # falls back
    assert await get_cached(session, name="nothing", now=NOW) is None
    assert [f.name for f in await search_by_name(session, "YOGURT")] == ["Greek yogurt 0%"]
    assert await search_by_name(session, "  ") == []


async def test_store_freshness_by_source(session: AsyncSession) -> None:
    per100 = Macros(kcal=100, protein_g=1, carbs_g=1, fat_g=1)
    old_off = await repo.upsert_food(
        session, name="old off", per_100g=per100, source="off", fetched_at=NOW - timedelta(days=91)
    )
    fresh_off = await repo.upsert_food(
        session,
        name="fresh off",
        per_100g=per100,
        source="off",
        fetched_at=NOW - timedelta(days=89),
    )
    old_usda = await repo.upsert_food(
        session,
        name="old usda",
        per_100g=per100,
        source="usda",
        fetched_at=NOW - timedelta(days=366),
    )
    old_model = await repo.upsert_food(
        session,
        name="old model",
        per_100g=per100,
        source="model",
        fetched_at=NOW - timedelta(days=1000),
    )
    assert not is_fresh(old_off, NOW)
    assert is_fresh(fresh_off, NOW)
    assert not is_fresh(old_usda, NOW)
    assert is_fresh(old_model, NOW)


# -------------------------------------------------------------------------------- resolve


async def test_resolve_barcode_hits_off_and_caches(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    async with router.client() as client:
        hit = await resolve_food(
            session, "nutella", barcode=NUTELLA, http=client, settings=settings, now=NOW
        )
        assert hit is not None and hit.source == "off"
        assert router.calls[0].headers["user-agent"] == settings.off_user_agent
        assert len(router.calls) == 1
        await session.commit()
        row = await repo.get_food_by_barcode(session, NUTELLA)
        assert row is not None
        assert row.source.value == "off"
        assert row.source_url == "https://world.openfoodfacts.org/product/3017620422003"
        assert row.per_100g["kcal"] == 539
        again = await resolve_food(
            session, "whatever", barcode=NUTELLA, http=client, settings=settings, now=NOW
        )
    assert again == hit
    assert len(router.calls) == 1  # served from the cache


async def test_resolve_generic_query_goes_to_usda_and_caches(
    session: AsyncSession, router: Router, usda_settings: Settings
) -> None:
    async with router.client() as client:
        hit = await resolve_food(session, "avocado", http=client, settings=usda_settings, now=NOW)
    assert hit is not None and hit.source == "usda"
    assert router.requests_to("world.openfoodfacts.org") == []
    assert router.calls[0].headers["x-api-key"] == "test-key-123"
    row = await repo.get_food_by_key(
        session, repo.make_food_key("Avocados, raw, all commercial varieties")
    )
    assert row is not None and row.source.value == "usda"
    assert row.source_url == "https://fdc.nal.usda.gov/food-details/171705/nutrients"


async def test_resolve_barcode_unknown_to_off_tries_usda_branded(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    async with router.client() as client:
        hit = await resolve_food(
            session,
            "greek yogurt",
            barcode="0818290014405",
            http=client,
            settings=settings,
            now=NOW,
        )
    assert hit is not None and hit.source == "usda" and hit.barcode == "0818290014405"
    hosts = [r.url.host for r in router.calls]
    assert hosts == ["world.openfoodfacts.org", "api.nal.usda.gov"]


async def test_resolve_returns_none_when_everything_misses(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    router.usda_by_type = {key: [] for key in router.usda_by_type}
    async with router.client() as client:
        assert (
            await resolve_food(session, "machboos", http=client, settings=settings, now=NOW) is None
        )
    assert await repo.get_food_by_key(session, repo.make_food_key("machboos")) is None


async def test_resolve_never_raises(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    router.raise_transport = True
    async with router.client() as client:
        assert (
            await resolve_food(
                session, "avocado", barcode=NUTELLA, http=client, settings=settings, now=NOW
            )
            is None
        )

    class BrokenSession:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("db down")

    async with router.client() as client:
        broken = cast(AsyncSession, BrokenSession())
        assert (
            await resolve_food(broken, "avocado", http=client, settings=settings, now=NOW) is None
        )


async def test_resolve_serves_stale_cache_when_network_fails(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    per100 = Macros(kcal=539, protein_g=6.3, carbs_g=57.5, fat_g=30.9)
    await repo.upsert_food(
        session,
        name="Nutella",
        brand="Ferrero",
        barcode=NUTELLA,
        per_100g=per100,
        source="off",
        fetched_at=NOW - timedelta(days=120),
    )
    await session.commit()
    router.raise_transport = True
    async with router.client() as client:
        hit = await resolve_food(
            session, "nutella", barcode=NUTELLA, http=client, settings=settings, now=NOW
        )
    assert hit is not None and hit.name == "Nutella" and hit.source == "off"
    # It tried the network first: OFF v3, OFF v2, USDA Branded by GTIN, USDA generic.
    assert [r.url.host for r in router.calls] == [
        "world.openfoodfacts.org",
        "world.openfoodfacts.org",
        "api.nal.usda.gov",
        "api.nal.usda.gov",
    ]


async def test_resolve_refreshes_a_stale_row(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    per100 = Macros(kcal=500, protein_g=6, carbs_g=50, fat_g=30)
    await repo.upsert_food(
        session,
        name="Nutella",
        brand="Ferrero",
        barcode=NUTELLA,
        per_100g=per100,
        source="off",
        fetched_at=NOW - timedelta(days=120),
    )
    await session.commit()
    async with router.client() as client:
        hit = await resolve_food(
            session, "nutella", barcode=NUTELLA, http=client, settings=settings, now=NOW
        )
    assert hit is not None and hit.per_100g.kcal == 539
    row = await repo.get_food_by_barcode(session, NUTELLA)
    assert (
        row is not None
        and row.per_100g["kcal"] == 539
        and row.fetched_at.replace(tzinfo=UTC) == NOW
    )


async def test_resolve_restaurant_dish_is_cache_only(
    session: AsyncSession, router: Router, settings: Settings
) -> None:
    async with router.client() as client:
        assert (
            await resolve_food(
                session,
                "chicken shawarma",
                restaurant="Kinoya",
                http=client,
                settings=settings,
                now=NOW,
            )
            is None
        )
        await cache_hit(
            session,
            FoodHit(
                name="chicken shawarma",
                restaurant="Kinoya",
                per_100g=Macros(kcal=180, protein_g=15, carbs_g=12, fat_g=8),
                source="web",
                source_url="https://example.com/menu",
            ),
            now=NOW,
        )
        hit = await resolve_food(
            session,
            "chicken shawarma",
            restaurant="Kinoya",
            http=client,
            settings=settings,
            now=NOW,
        )
    assert hit is not None and hit.source == "web" and hit.restaurant == "Kinoya"
    assert router.calls == []


async def test_resolve_builds_its_own_client_when_none_given(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = Router()
    monkeypatch.setattr("strikt.nutrition.resolve._client", router.client)
    hit = await resolve_food(session, "nutella", barcode=NUTELLA, settings=settings, now=NOW)
    assert hit is not None and hit.name == "Nutella"
    assert len(router.calls) == 1
