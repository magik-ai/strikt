"""Open Food Facts client (research/06 §1): product by barcode over the v3 endpoint.

* ``GET https://world.openfoodfacts.org/api/v3/product/{barcode}?fields=…`` — envelope
  ``{"status": "success", "result": {"id": "product_found"}, "product": {...}}``; the v2 URL is
  the fallback when v3 is unavailable (both carry identical ``nutriments`` keys).
* Nutriments are per 100 g (or 100 ml): ``energy-kcal_100g`` (else ``energy-kj_100g`` / 4.184),
  ``proteins_100g``, ``carbohydrates_100g``, ``fat_100g``, ``fiber_100g`` (often absent),
  ``sodium_100g`` in **grams** (else ``salt_100g`` / 2.5).
* Always sends the custom ``User-Agent`` (``settings.off_user_agent``); read limit 15 req/min/IP.
* Never raises: any transport error, non-JSON body (OFF returns HTML 5xx pages) or implausible
  numbers (research/06 §7.2 bounds) → ``None`` with a structlog warning.
"""

from __future__ import annotations

import re
from typing import Any, Final

import httpx
import structlog

from strikt.core.types import FoodHit, Macros
from strikt.nutrition.math import kcal_from_kj, kcal_from_macros_eu

log = structlog.get_logger(__name__)

OFF_BASE: Final[str] = "https://world.openfoodfacts.org"
OFF_FIELDS: Final[str] = (
    "product_name,brands,serving_size,product_quantity,nutrition_data_per,nutriments,completeness"
)
OFF_TIMEOUT_S: Final[float] = 6.0
OFF_CONFIDENCE: Final[float] = 0.85

_SERVING_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(g|ml|гр?|мл)", re.IGNORECASE
)


def product_url(barcode: str) -> str:
    return f"{OFF_BASE}/product/{barcode}"


def api_url(barcode: str, *, version: int = 3) -> str:
    if version == 2:
        return f"{OFF_BASE}/api/v2/product/{barcode}.json?fields={OFF_FIELDS}"
    return f"{OFF_BASE}/api/v3/product/{barcode}?fields={OFF_FIELDS}"


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


def parse_serving_size(text: object) -> float | None:
    """``"38g"`` / ``"250 ml"`` / ``"2 biscuits (30 g)"`` → grams; None when unparseable."""
    if not isinstance(text, str):
        return None
    match = _SERVING_RE.search(text)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def _bounds_ok(macros: Macros) -> bool:
    """research/06 §7.2: each macro ≤ 100 g/100 g, sum ≤ 105, kcal ≤ 900, sodium ≤ 40 g."""
    if any(
        v < 0 for v in (macros.kcal, macros.protein_g, macros.carbs_g, macros.fat_g, macros.fiber_g)
    ):
        return False
    if max(macros.protein_g, macros.carbs_g, macros.fat_g, macros.fiber_g) > 100:
        return False
    if macros.protein_g + macros.carbs_g + macros.fat_g + macros.fiber_g > 105:
        return False
    if macros.kcal > 900:
        return False
    return not (macros.sodium_mg is not None and macros.sodium_mg > 40_000)


def parse_product(payload: dict[str, Any], barcode: str) -> FoodHit | None:
    """Turn a v2/v3 product envelope into a ``FoodHit`` (per 100 g), or None when unusable."""
    status = payload.get("status")
    found = status in {"success", 1, "1"}
    product = payload.get("product")
    if not found or not isinstance(product, dict):
        return None
    nutriments = product.get("nutriments")
    if not isinstance(nutriments, dict):
        return None
    protein = _num(nutriments.get("proteins_100g"))
    carbs = _num(nutriments.get("carbohydrates_100g"))
    fat = _num(nutriments.get("fat_100g"))
    if protein is None or carbs is None or fat is None:
        return None
    fiber = _num(nutriments.get("fiber_100g"))
    if fiber is None:
        estimated = product.get("nutriments_estimated")
        fiber = _num(estimated.get("fiber_100g")) if isinstance(estimated, dict) else None
    fiber = fiber or 0.0
    kcal = _num(nutriments.get("energy-kcal_100g"))
    if kcal is None:
        kj = _num(nutriments.get("energy-kj_100g")) or _num(nutriments.get("energy_100g"))
        kcal = kcal_from_kj(kj) if kj is not None else None
    if kcal is None:
        kcal = kcal_from_macros_eu(protein, carbs, fat, fiber)
    sodium_g = _num(nutriments.get("sodium_100g"))
    if sodium_g is None:
        salt = _num(nutriments.get("salt_100g"))
        sodium_g = salt / 2.5 if salt is not None else None
    alcohol = _num(nutriments.get("alcohol_100g")) or 0.0
    macros = Macros(
        kcal=kcal,
        protein_g=protein,
        carbs_g=carbs,
        fat_g=fat,
        fiber_g=fiber,
        sodium_mg=None if sodium_g is None else sodium_g * 1000.0,
        alcohol_g=alcohol,
    )
    if not _bounds_ok(macros):
        log.warning("off.implausible", barcode=barcode)
        return None
    name = product.get("product_name") or product.get("product_name_en") or f"barcode {barcode}"
    brands = product.get("brands")
    brand = brands.split(",")[0].strip() if isinstance(brands, str) and brands.strip() else None
    serving_desc = (
        product.get("serving_size") if isinstance(product.get("serving_size"), str) else None
    )
    serving_g = parse_serving_size(serving_desc)
    completeness = _num(product.get("completeness"))
    confidence = OFF_CONFIDENCE if completeness is None else min(0.95, 0.6 + 0.35 * completeness)
    return FoodHit(
        name=str(name).strip(),
        brand=brand,
        barcode=barcode,
        per_100g=macros,
        serving_g=serving_g,
        serving_desc=serving_desc,
        source="off",
        source_url=product_url(barcode),
        confidence=confidence,
    )


def _json_body(response: httpx.Response) -> dict[str, Any] | None:
    if "json" not in response.headers.get("content-type", "").lower():
        return None
    try:
        body: object = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def fetch_product(
    client: httpx.AsyncClient, barcode: str, *, user_agent: str
) -> FoodHit | None:
    """Product by barcode via v3, falling back to v2 on a non-404 failure. Never raises."""
    digits = re.sub(r"\D", "", barcode)
    if not digits:
        return None
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    for version in (3, 2):
        try:
            response = await client.get(
                api_url(digits, version=version), headers=headers, timeout=OFF_TIMEOUT_S
            )
        except httpx.HTTPError as exc:
            log.warning("off.transport", barcode=digits, version=version, error=str(exc))
            continue
        if response.status_code == 404:
            body = _json_body(response)
            if body is not None and version == 3 and body.get("status") == "success":
                return parse_product(body, digits)
            log.info("off.not_found", barcode=digits)
            return None
        if response.status_code >= 400:
            log.warning("off.status", barcode=digits, version=version, status=response.status_code)
            continue
        body = _json_body(response)
        if body is None:
            log.warning("off.non_json", barcode=digits, version=version)
            continue
        return parse_product(body, digits)
    return None
