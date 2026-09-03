"""Nutrition arithmetic (PLAN §5 ``math.py``). Pure functions, no IO.

Energy factors follow research/06 §7.1: Atwater general factors 4/4/9 kcal per gram of
protein/carbohydrate/fat and 7 kcal per gram of alcohol (US labels count total carbohydrate,
fibre included). EU/GCC labels exclude fibre from "carbohydrate" and count it at 2 kcal/g, so
``kcal_from_macros_eu`` adds ``2 × fibre``.
"""

from __future__ import annotations

from collections.abc import Iterable

from strikt.core.types import Macros

KCAL_PER_G_PROTEIN = 4.0
KCAL_PER_G_CARBS = 4.0
KCAL_PER_G_FAT = 9.0
KCAL_PER_G_ALCOHOL = 7.0
KCAL_PER_G_FIBER_EU = 2.0
KJ_PER_KCAL = 4.184


def kcal_from_macros(
    protein_g: float, carbs_g: float, fat_g: float, alcohol_g: float = 0.0
) -> float:
    """Energy by the US (Atwater general) convention: 4P + 4C + 9F + 7A."""
    return (
        protein_g * KCAL_PER_G_PROTEIN
        + carbs_g * KCAL_PER_G_CARBS
        + fat_g * KCAL_PER_G_FAT
        + alcohol_g * KCAL_PER_G_ALCOHOL
    )


def kcal_from_macros_eu(
    protein_g: float, carbs_g: float, fat_g: float, fiber_g: float = 0.0, alcohol_g: float = 0.0
) -> float:
    """Energy by the EU 1169/2011 convention: carbs exclude fibre, fibre counts 2 kcal/g."""
    return kcal_from_macros(protein_g, carbs_g, fat_g, alcohol_g) + fiber_g * KCAL_PER_G_FIBER_EU


def computed_kcal(macros: Macros, *, convention: str = "us") -> float:
    """Energy re-derived from a ``Macros`` row (``convention`` is ``"us"`` or ``"eu"``)."""
    if convention == "eu":
        return kcal_from_macros_eu(
            macros.protein_g, macros.carbs_g, macros.fat_g, macros.fiber_g, macros.alcohol_g
        )
    return kcal_from_macros(macros.protein_g, macros.carbs_g, macros.fat_g, macros.alcohol_g)


def kcal_from_kj(kj: float) -> float:
    return kj / KJ_PER_KCAL


def scale_per_100g(per100: Macros, grams: float) -> Macros:
    """Macros of ``grams`` of a food whose values are given per 100 g (or per 100 ml)."""
    return per100.scaled(grams / 100.0)


def scale(per_100g: Macros, grams: float) -> Macros:
    """PLAN §5 name for :func:`scale_per_100g`."""
    return scale_per_100g(per_100g, grams)


def per_serving(label: Macros, serving_g: float) -> Macros:
    """Per-serving macros from a per-100 g label and the serving weight."""
    return scale_per_100g(label, serving_g)


def sum_macros(items: Iterable[Macros]) -> Macros:
    """Sum any number of ``Macros``; sodium stays ``None`` only when every input lacks it."""
    total = Macros.zero()
    for item in items:
        total = total + item
    return total


def mismatch_ratio(stated_kcal: float, computed: float) -> float:
    """|stated − computed| / computed. Both zero → 0; computed zero with a stated value → 1."""
    if computed <= 0:
        return 0.0 if stated_kcal <= 0 else 1.0
    return abs(stated_kcal - computed) / computed


def round_kcal(value: float) -> float:
    """Whole kilocalories (the card never shows decimals on energy)."""
    return float(round(value))


def round_g(value: float, ndigits: int = 1) -> float:
    return round(value, ndigits)


def round_macros(macros: Macros) -> Macros:
    """kcal to whole numbers, grams to one decimal, sodium to whole milligrams."""
    return Macros(
        kcal=round_kcal(macros.kcal),
        protein_g=round_g(macros.protein_g),
        carbs_g=round_g(macros.carbs_g),
        fat_g=round_g(macros.fat_g),
        fiber_g=round_g(macros.fiber_g),
        sodium_mg=None if macros.sodium_mg is None else float(round(macros.sodium_mg)),
        alcohol_g=round_g(macros.alcohol_g),
    )
