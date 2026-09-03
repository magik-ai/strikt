"""``agent/numbers.py``: the day totals a reply claims, in Russian and English."""

from __future__ import annotations

import pytest

from strikt.agent.numbers import ClaimedTotals, extract_numbers, is_total_line


def test_russian_total_line_with_thin_spaces_and_cyrillic_labels() -> None:
    text = (
        "Записал.\n"
        "Куриная грудка 200 г — 330 ккал / Б 62 / У 0 / Ж 7\n"
        "Итого за день: 1 240 ккал | Б 118 | У 60 | Ж 45 | клетчатка 12\n"
        "Осталось: 760 ккал | Б 92 | У 15 | Ж 60"
    )
    claimed = extract_numbers(text)
    assert claimed == ClaimedTotals(kcal=1240, protein_g=118, carbs_g=60, fat_g=45, fiber_g=12)


def test_english_total_with_comma_thousands_and_gram_words() -> None:
    text = (
        "Logged.\n"
        "Total so far: 1,240 kcal · 118 g protein · 60 g carbs · 45 g fat · 12 g fiber\n"
        "Left: 760 kcal · 92 P"
    )
    claimed = extract_numbers(text)
    assert claimed == ClaimedTotals(kcal=1240, protein_g=118, carbs_g=60, fat_g=45, fiber_g=12)


def test_single_letter_labels_before_and_after_numbers() -> None:
    assert extract_numbers("Total 1500 kcal, P 118, C 60, F 45") == ClaimedTotals(
        kcal=1500, protein_g=118, carbs_g=60, fat_g=45
    )
    assert extract_numbers("Day total: 620 kcal | 40P | 30C | 20F") == ClaimedTotals(
        kcal=620, protein_g=40, carbs_g=30, fat_g=20
    )


def test_per_item_and_remaining_lines_are_ignored() -> None:
    text = "Chicken 330 kcal P 62\nSalad 90 kcal P 2\nRemaining 900 kcal · 120 P"
    assert not extract_numbers(text).any


def test_target_lines_are_not_totals() -> None:
    assert not is_total_line("Target for the day: 2000 kcal")
    assert not is_total_line("Норма на день 2000 ккал")
    assert is_total_line("Итого 1240 ккал")
    assert is_total_line("**Total** 1 240 kcal")


def test_last_total_line_wins() -> None:
    text = "Total 1000 kcal\nrecount…\nTotal 1240 kcal, P 118"
    claimed = extract_numbers(text)
    assert claimed.kcal == 1240
    assert claimed.protein_g == 118


def test_markdown_bold_is_stripped_before_matching() -> None:
    claimed = extract_numbers("**Итого:** 1 240 ккал | **Б** 118")
    assert claimed.kcal == 1240
    assert claimed.protein_g == 118


def test_decimal_values() -> None:
    claimed = extract_numbers("Total 1240.5 kcal, fiber 12.4 g")
    assert claimed.kcal == pytest.approx(1240.5)
    assert claimed.fiber_g == pytest.approx(12.4)


def test_items_lists_only_present_fields() -> None:
    claimed = ClaimedTotals(kcal=100, fat_g=3)
    assert claimed.items() == [("kcal", 100), ("fat_g", 3)]
    assert claimed.any
    assert not ClaimedTotals().any
