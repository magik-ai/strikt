"""nutrition.math: energy factors, scaling, sums, mismatch ratio, rounding."""

from __future__ import annotations

import pytest

from strikt.core.types import Macros
from strikt.nutrition.math import (
    computed_kcal,
    kcal_from_kj,
    kcal_from_macros,
    kcal_from_macros_eu,
    mismatch_ratio,
    per_serving,
    round_g,
    round_kcal,
    round_macros,
    scale,
    scale_per_100g,
    sum_macros,
)


def test_kcal_from_macros_uses_4_4_9() -> None:
    assert kcal_from_macros(30, 40, 10) == 30 * 4 + 40 * 4 + 10 * 9


def test_kcal_from_macros_counts_alcohol_at_7() -> None:
    assert kcal_from_macros(0, 0, 0, alcohol_g=14) == 98


def test_eu_convention_adds_two_kcal_per_gram_of_fibre() -> None:
    assert kcal_from_macros_eu(10, 20, 5, fiber_g=8) == kcal_from_macros(10, 20, 5) + 16
    assert kcal_from_macros_eu(10, 20, 5, fiber_g=8, alcohol_g=1) == kcal_from_macros(10, 20, 5) + 16 + 7


def test_computed_kcal_conventions() -> None:
    macros = Macros(kcal=0, protein_g=10, carbs_g=20, fat_g=5, fiber_g=8, alcohol_g=2)
    assert computed_kcal(macros) == 40 + 80 + 45 + 14
    assert computed_kcal(macros, convention="eu") == 40 + 80 + 45 + 14 + 16


def test_kcal_from_kj() -> None:
    assert kcal_from_kj(4184) == pytest.approx(1000)


def test_scale_per_100g_scales_every_field_and_keeps_missing_sodium() -> None:
    per100 = Macros(kcal=100, protein_g=10, carbs_g=20, fat_g=2, fiber_g=3, sodium_mg=400, alcohol_g=1)
    scaled = scale_per_100g(per100, 250)
    assert scaled == Macros(kcal=250, protein_g=25, carbs_g=50, fat_g=5, fiber_g=7.5, sodium_mg=1000, alcohol_g=2.5)
    assert scale_per_100g(Macros(kcal=100, protein_g=1, carbs_g=1, fat_g=1), 50).sodium_mg is None
    assert scale(per100, 250) == scaled


def test_per_serving_is_scaling_a_label() -> None:
    label = Macros(kcal=539, protein_g=6.3, carbs_g=57.5, fat_g=30.9)
    serving = per_serving(label, 15)
    assert serving.kcal == pytest.approx(80.85)
    assert serving.fat_g == pytest.approx(4.635)


def test_sum_macros_sums_and_tracks_sodium_presence() -> None:
    a = Macros(kcal=100, protein_g=10, carbs_g=5, fat_g=2, fiber_g=1)
    b = Macros(kcal=50, protein_g=1, carbs_g=10, fat_g=1, fiber_g=2, sodium_mg=300)
    total = sum_macros([a, b])
    assert total == Macros(kcal=150, protein_g=11, carbs_g=15, fat_g=3, fiber_g=3, sodium_mg=300, alcohol_g=0)
    assert sum_macros([a, a]).sodium_mg is None
    assert sum_macros([]) == Macros.zero()


@pytest.mark.parametrize(
    ("stated", "computed", "expected"),
    [(110, 100, 0.1), (90, 100, 0.1), (100, 100, 0.0), (0, 0, 0.0), (50, 0, 1.0), (0, 200, 1.0)],
)
def test_mismatch_ratio(stated: float, computed: float, expected: float) -> None:
    assert mismatch_ratio(stated, computed) == pytest.approx(expected)


def test_round_helpers() -> None:
    assert round_kcal(123.4) == 123
    assert round_kcal(123.6) == 124
    assert round_g(1.26) == 1.3
    assert round_g(1.234, 2) == 1.23
    rounded = round_macros(
        Macros(kcal=99.5, protein_g=1.25, carbs_g=2.26, fat_g=0.04, fiber_g=3.99, sodium_mg=12.6, alcohol_g=0.15)
    )
    assert rounded == Macros(kcal=100, protein_g=1.2, carbs_g=2.3, fat_g=0.0, fiber_g=4.0, sodium_mg=13, alcohol_g=0.1)
    assert round_macros(Macros(kcal=1, protein_g=1, carbs_g=1, fat_g=1)).sodium_mg is None
