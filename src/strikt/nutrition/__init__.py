"""Nutrition engine (PLAN §5): arithmetic, unit conversion, sanity rules and food resolution.

Pure, deterministic pieces (``math``, ``units``, ``sanity``) have no IO. ``resolve`` talks to the
``foods`` cache and, through ``off`` / ``usda``, to Open Food Facts and USDA FoodData Central;
it never raises into the agent.
"""

from __future__ import annotations

from strikt.nutrition.math import (
    kcal_from_macros,
    kcal_from_macros_eu,
    mismatch_ratio,
    per_serving,
    round_macros,
    scale_per_100g,
    sum_macros,
)
from strikt.nutrition.resolve import resolve_food
from strikt.nutrition.sanity import check_item, check_items, classify_countable
from strikt.nutrition.units import parse_quantity, to_grams

__all__ = [
    "check_item",
    "check_items",
    "classify_countable",
    "kcal_from_macros",
    "kcal_from_macros_eu",
    "mismatch_ratio",
    "parse_quantity",
    "per_serving",
    "resolve_food",
    "round_macros",
    "scale_per_100g",
    "sum_macros",
    "to_grams",
]
