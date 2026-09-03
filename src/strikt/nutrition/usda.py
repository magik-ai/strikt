"""USDA FoodData Central client (research/06 §2): generic foods by text search.

* ``GET https://api.nal.usda.gov/fdc/v1/foods/search?query=…&dataType=…&pageSize=…`` with the
  key in the ``X-Api-Key`` header (never in the URL, so it never reaches a log line).
* Search priority: ``Foundation,SR Legacy`` (analytical) → ``Survey (FNDDS)`` (cooked/mixed
  dishes) → ``Branded`` (label data; also used for barcode lookups with ``query=<gtinUpc>``).
* Nutrient mapping (ids / legacy numbers): 1008/208 kcal, 1003/203 protein, 1004/204 fat,
  1005/205 carbs, 1079/291 fibre, 1093/307 sodium (mg). The live search shape is
  ``{nutrientId, nutrientNumber, value}``; detail/abridged shapes use ``nutrient.id`` /
  ``number`` and ``amount`` — the parser reads whichever is present. Energy falls back
  2048 → 2047 → 1062 (kJ) → computed 4/4/9.
* Values are per 100 g. Branded rows carry ``servingSize`` + ``servingSizeUnit``.
* 1 000 req/h with a registered key; HTTP 429 → None. Never raises.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Final

import httpx
import structlog

from strikt.core.types import FoodHit, Macros
from strikt.nutrition.math import kcal_from_kj, kcal_from_macros
from strikt.nutrition.units import singularize

log = structlog.get_logger(__name__)

USDA_BASE: Final[str] = "https://api.nal.usda.gov/fdc/v1"
USDA_TIMEOUT_S: Final[float] = 6.0
USDA_PAGE_SIZE: Final[int] = 10
DEMO_KEY: Final[str] = "DEMO_KEY"

DATA_TYPE_PRIORITY: Final[tuple[tuple[str, ...], ...]] = (
    ("Foundation", "SR Legacy"),
    ("Survey (FNDDS)",),
    ("Branded",),
)

NUTRIENT_IDS: Final[dict[str, tuple[int, ...]]] = {
    "kcal": (1008,),
    "kcal_atwater": (2048, 2047),
    "kj": (1062,),
    "protein_g": (1003,),
    "fat_g": (1004,),
    "carbs_g": (1005,),
    "fiber_g": (1079,),
    "sodium_mg": (1093,),
}
NUTRIENT_NUMBERS: Final[dict[str, tuple[str, ...]]] = {
    "kcal": ("208",),
    "kcal_atwater": ("958", "957"),
    "kj": ("268",),
    "protein_g": ("203",),
    "fat_g": ("204",),
    "carbs_g": ("205",),
    "fiber_g": ("291",),
    "sodium_mg": ("307",),
}
CONFIDENCE_BY_TYPE: Final[dict[str, float]] = {
    "Foundation": 0.9,
    "SR Legacy": 0.9,
    "Survey (FNDDS)": 0.8,
    "Branded": 0.85,
}


def food_url(fdc_id: int | str) -> str:
    return f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _nutrient_key(row: dict[str, Any]) -> tuple[int | None, str | None]:
    nested = row.get("nutrient")
    nested = nested if isinstance(nested, dict) else {}
    raw_id = row.get("nutrientId", nested.get("id"))
    raw_number = row.get("nutrientNumber", row.get("number", nested.get("number")))
    nid = int(raw_id) if isinstance(raw_id, int | float) and not isinstance(raw_id, bool) else None
    number = str(raw_number) if raw_number is not None else None
    return nid, number


def extract_nutrients(rows: object) -> dict[str, float]:
    """Map a ``foodNutrients`` list (any FDC shape) to our nutrient keys, values per 100 g."""
    out: dict[str, float] = {}
    if not isinstance(rows, list):
        return out
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row: dict[str, Any] = raw
        nid, number = _nutrient_key(row)
        value = _num(row.get("value", row.get("amount")))
        if value is None:
            continue
        for key, ids in NUTRIENT_IDS.items():
            if (nid is not None and nid in ids) or (number in NUTRIENT_NUMBERS[key]):
                out.setdefault(key, value)
    return out


def macros_from_nutrients(values: dict[str, float]) -> Macros | None:
    protein = values.get("protein_g")
    fat = values.get("fat_g")
    carbs = values.get("carbs_g")
    if protein is None or fat is None or carbs is None:
        return None
    kcal = values.get("kcal")
    if kcal is None:
        kcal = values.get("kcal_atwater")
    if kcal is None and "kj" in values:
        kcal = kcal_from_kj(values["kj"])
    if kcal is None:
        kcal = kcal_from_macros(protein, carbs, fat)
    return Macros(
        kcal=kcal,
        protein_g=protein,
        carbs_g=carbs,
        fat_g=fat,
        fiber_g=values.get("fiber_g", 0.0),
        sodium_mg=values.get("sodium_mg"),
    )


def _serving(food: dict[str, Any]) -> tuple[float | None, str | None]:
    size = _num(food.get("servingSize"))
    unit = str(food.get("servingSizeUnit") or "").lower()
    desc = food.get("householdServingFullText")
    desc_text = str(desc).strip() if isinstance(desc, str) and desc.strip() else None
    if size is not None and unit in {"g", "ml", "grm", "mlt"}:
        return size, desc_text or f"{size:g} {unit[:2]}"
    return None, desc_text


def parse_search_food(food: dict[str, Any]) -> FoodHit | None:
    """One ``foods[]`` element from ``/foods/search`` → ``FoodHit`` (per 100 g)."""
    macros = macros_from_nutrients(extract_nutrients(food.get("foodNutrients")))
    if macros is None:
        return None
    description = str(food.get("description") or "").strip()
    if not description:
        return None
    data_type = str(food.get("dataType") or "")
    brand_owner = food.get("brandOwner")
    brand_name = food.get("brandName")
    brand = None
    for candidate in (brand_name, brand_owner):
        if isinstance(candidate, str) and candidate.strip():
            brand = candidate.strip().title()
            break
    gtin = food.get("gtinUpc")
    serving_g, serving_desc = _serving(food)
    fdc_id = food.get("fdcId")
    return FoodHit(
        name=description.capitalize() if description.isupper() else description,
        brand=brand,
        barcode=str(gtin) if gtin else None,
        per_100g=macros,
        serving_g=serving_g,
        serving_desc=serving_desc,
        source="usda",
        source_url=food_url(fdc_id) if fdc_id is not None else None,
        confidence=CONFIDENCE_BY_TYPE.get(data_type, 0.8),
    )


_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-zа-яё0-9%]+", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    """Lower-cased, singularized words ("Avocados, raw" → {"avocado", "raw"})."""
    return {singularize(word) for word in _WORD_RE.findall(text.lower())}


def _score(food: dict[str, Any], query_words: set[str]) -> tuple[int, int, int]:
    words = _tokens(str(food.get("description") or ""))
    overlap = len(query_words & words)
    # Prefer plain/raw/cooked descriptions over long processed variants: fewer words is better.
    penalty = len(words)
    raw_bonus = 1 if "raw" in words or "cooked" in words else 0
    return (overlap, raw_bonus, -penalty)


def pick_best(foods: Sequence[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """The result whose description shares most words with ``query`` (ties → shortest)."""
    if not foods:
        return None
    query_words = _tokens(query)
    return max(foods, key=lambda food: _score(food, query_words))


async def _search_page(
    client: httpx.AsyncClient,
    query: str,
    *,
    api_key: str,
    data_types: Sequence[str],
    page_size: int,
) -> list[dict[str, Any]] | None:
    params = {"query": query, "dataType": ",".join(data_types), "pageSize": str(page_size)}
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    try:
        response = await client.get(
            f"{USDA_BASE}/foods/search", params=params, headers=headers, timeout=USDA_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        log.warning("usda.transport", query=query, error=str(exc))
        return None
    if response.status_code == 429:
        log.warning(
            "usda.rate_limited",
            query=query,
            remaining=response.headers.get("x-ratelimit-remaining"),
        )
        return None
    if response.status_code >= 400:
        log.warning("usda.status", query=query, status=response.status_code)
        return None
    try:
        body: object = response.json()
    except ValueError:
        log.warning("usda.non_json", query=query)
        return None
    if not isinstance(body, dict):
        return None
    foods = body.get("foods")
    return [food for food in foods if isinstance(food, dict)] if isinstance(foods, list) else []


async def search_food(
    client: httpx.AsyncClient,
    query: str,
    *,
    api_key: str | None,
    brand: str | None = None,
    barcode: str | None = None,
    page_size: int = USDA_PAGE_SIZE,
) -> FoodHit | None:
    """Best generic hit for ``query`` following ``DATA_TYPE_PRIORITY``; branded/barcode last.

    With a barcode, only ``Branded`` is searched with ``query=<gtinUpc>`` and the hit must carry
    that GTIN. Without an API key ``DEMO_KEY`` is used (a smoke-test allowance only).
    """
    key = api_key or DEMO_KEY
    if barcode:
        digits = re.sub(r"\D", "", barcode)
        foods = await _search_page(
            client, digits, api_key=key, data_types=("Branded",), page_size=page_size
        )
        if not foods:
            return None
        matches = [
            f
            for f in foods
            if re.sub(r"\D", "", str(f.get("gtinUpc") or "")).lstrip("0") == digits.lstrip("0")
        ]
        best = matches[0] if matches else None
        return parse_search_food(best) if best is not None else None
    text = query.strip()
    if not text:
        return None
    for data_types in DATA_TYPE_PRIORITY:
        search_text = f"{brand} {text}".strip() if brand and "Branded" in data_types else text
        foods = await _search_page(
            client, search_text, api_key=key, data_types=data_types, page_size=page_size
        )
        if foods is None:
            return None  # transport/quota failure: stop, do not burn more quota
        if not foods:
            continue
        best = pick_best(foods, search_text)
        hit = parse_search_food(best) if best is not None else None
        if hit is not None:
            return hit
    return None
