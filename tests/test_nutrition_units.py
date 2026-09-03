"""nutrition.units: quantity parsing and gram conversion."""

from __future__ import annotations

import pytest

from strikt.nutrition.units import (
    PIECE_GRAMS,
    UNIT_TO_GRAMS,
    grams_from_text,
    normalize_unit,
    parse_quantity,
    piece_grams,
    singularize,
    slice_grams,
    to_grams,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("200 g", (200.0, "g")),
        ("2 eggs", (2.0, "egg")),
        ("160g", (160.0, "g")),
        ("1.5 cups rice", (1.5, "cup")),
        ("1,5 kg", (1.5, "kg")),
        ("1/2 avocado", (0.5, "avocado")),
        ("½ banana", (0.5, "banana")),
        ("1½ cups", (1.5, "cup")),
        ("3", (3.0, "piece")),
        ("2 x eggs", (2.0, "egg")),
        ("200 мл", (200.0, "ml")),
        ("2 ст.л оливкового масла", (2.0, "tbsp")),
        ("3 шт", (3.0, "piece")),
        ("half an avocado", (0.5, "avocado")),
        ("two slices of bread", (2.0, "slice")),
        ("a banana", (1.0, "banana")),
        ("полбанана", (0.5, "банана")),
        ("2 tomatoes", (2.0, "tomato")),
        ("12 oz steak", (12.0, "oz")),
        ("1 lb", (1.0, "lb")),
    ],
)
def test_parse_quantity(text: str, expected: tuple[float, str]) -> None:
    parsed = parse_quantity(text)
    assert parsed is not None
    assert parsed[0] == pytest.approx(expected[0])
    assert parsed[1] == expected[1]


@pytest.mark.parametrize("text", ["some rice", "", "bowl of pasta", "много"])
def test_parse_quantity_without_a_number(text: str) -> None:
    assert parse_quantity(text) is None


@pytest.mark.parametrize(
    ("word", "expected"),
    [("eggs", "egg"), ("slices", "slice"), ("tomatoes", "tomato"), ("berries", "berry"), ("glass", "glass"), ("cups", "cup"), ("g", "g")],
)
def test_singularize(word: str, expected: str) -> None:
    assert singularize(word) == expected


def test_normalize_unit_aliases() -> None:
    assert normalize_unit("grams") == "g"
    assert normalize_unit("Gr") == "g"
    assert normalize_unit("tablespoons") == "tbsp"
    assert normalize_unit("pcs") == "piece"
    assert normalize_unit(None) == "piece"
    assert normalize_unit("eggs") == "egg"


def test_unit_table_values() -> None:
    assert UNIT_TO_GRAMS["kg"] == 1000
    assert UNIT_TO_GRAMS["oz"] == pytest.approx(28.35, abs=0.01)
    assert UNIT_TO_GRAMS["lb"] == pytest.approx(453.6, abs=0.01)
    assert UNIT_TO_GRAMS["cup"] == 240
    assert UNIT_TO_GRAMS["tbsp"] == 15
    assert UNIT_TO_GRAMS["tsp"] == 5
    assert PIECE_GRAMS["egg"] == 50
    assert PIECE_GRAMS["banana"] == 118
    assert PIECE_GRAMS["bread"] == 30
    assert PIECE_GRAMS["chicken breast"] == 170


@pytest.mark.parametrize(
    ("quantity", "unit", "food", "expected"),
    [
        (200, "g", None, 200.0),
        (1, "kg", None, 1000.0),
        (1, "oz", None, 28.3495),
        (250, "ml", None, 250.0),
        (1, "cup", None, 240.0),
        (2, "egg", None, 100.0),
        (2, "piece", "boiled eggs", 100.0),
        (1, "piece", "egg white", 33.0),
        (1, "piece", "banana", 118.0),
        (1, "piece", "grilled chicken breast", 170.0),
        (2, "slice", "sourdough bread", 60.0),
        (1, "slice", "pepperoni pizza", 110.0),
        (1, "slice", "cheddar cheese", 20.0),
        (1, "scoop", "whey protein", 30.0),
        (1, "tortilla", None, 45.0),
        (0.5, "avocado", None, 75.0),
    ],
)
def test_to_grams(quantity: float, unit: str, food: str | None, expected: float) -> None:
    assert to_grams(quantity, unit, food=food) == pytest.approx(expected)


def test_to_grams_volume_uses_density() -> None:
    assert to_grams(1, "cup", density=0.5) == 120
    assert to_grams(100, "ml", density=1.03) == pytest.approx(103)


def test_to_grams_serving_needs_serving_size() -> None:
    assert to_grams(2, "serving", serving_g=45) == 90
    assert to_grams(2, "serving") is None


def test_to_grams_unknown_piece_is_none() -> None:
    assert to_grams(1, "piece", food="quantum foam") is None
    assert to_grams(1, "widget") is None


def test_piece_and_slice_lookups() -> None:
    assert piece_grams("2 large eggs") == 50
    assert piece_grams(None) is None
    assert piece_grams("nothing known") is None
    assert slice_grams("ham") == 25
    assert slice_grams("unknown") == 30


def test_grams_from_text() -> None:
    assert grams_from_text("2 eggs") == 100
    assert grams_from_text("160g", food="rice") == 160
    assert grams_from_text("1/2 avocado") == 75
    assert grams_from_text("bowl") is None
    assert grams_from_text("2 pieces", food="falafel") == 34
