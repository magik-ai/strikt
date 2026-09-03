"""nutrition.sanity: every rule in PLAN §5, with every case from brief §3.2."""

from __future__ import annotations

import pytest

from strikt.core.types import Flag, FoodItemIn, FoodSource, Macros
from strikt.nutrition.sanity import (
    check_item,
    check_items,
    classify_countable,
    is_processed_meat,
    mentions_cv_risk,
)


def item(
    name: str,
    kcal: float,
    p: float,
    c: float,
    f: float,
    *,
    fiber: float = 0,
    sodium: float | None = None,
    grams: float | None = None,
    quantity: float | None = None,
    source: FoodSource = "model",
    countable: bool = True,
) -> FoodItemIn:
    return FoodItemIn(
        name=name,
        grams=grams,
        quantity=quantity,
        macros=Macros(kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=fiber, sodium_mg=sodium),
        source=source,
        countable=countable,
    )


def codes(flags: list[Flag]) -> list[str]:
    return [flag.code for flag in flags]


# ------------------------------------------------------------------- brief §3.2 fixtures


def test_brief_chicken_avocado_plate_at_7g_fat_is_corrected() -> None:
    checked, flags = check_item(item("Chicken avocado plate", 303, 42, 18, 7))
    assert codes(flags) == ["implausible_fat"]
    assert checked.macros.fat_g == 15
    assert checked.macros.kcal == 303 + 8 * 9
    assert "avocado" in flags[0].message
    assert flags[0].corrected is not None
    assert flags[0].corrected.fat_g == 15
    assert checked.countable is True


def test_brief_egg_and_toast_at_15g_fiber_is_corrected() -> None:
    checked, flags = check_item(item("Eggs and toast", 320, 18, 28, 14, fiber=15))
    assert codes(flags) == ["implausible_fiber"]
    assert checked.macros.fiber_g == 4
    assert "no fibre" in flags[0].message
    assert checked.macros.kcal == 320  # fibre does not change energy under 4/4/9


def test_brief_large_pasta_at_26g_carbs_is_corrected_from_the_portion() -> None:
    checked, flags = check_item(item("Large pasta with tomato sauce", 260, 10, 26, 8, grams=300))
    assert codes(flags) == ["portion_implausible"]
    assert checked.macros.carbs_g == 90
    assert checked.macros.kcal == 260 + (90 - 26) * 4
    assert checked.countable is False  # loose, but the buffer is not stacked on the correction


def test_brief_large_pasta_without_grams_assumes_a_large_portion() -> None:
    checked, flags = check_item(item("Large pasta bolognese", 300, 14, 26, 10))
    assert "portion_implausible" in codes(flags)
    assert checked.macros.carbs_g == 75  # 250 g × 30 g/100 g, inside the brief's 60–80 g
    assert "large" in flags[0].message


def test_brief_roasted_brussels_sprouts_at_9g_fat_get_the_oil_note() -> None:
    checked, flags = check_item(item("Roasted brussels sprouts", 140, 4, 12, 9, fiber=4))
    assert codes(flags) == ["vegetable_fat"]
    assert flags[0].severity == "info"
    assert flags[0].corrected is None
    assert "oil" in flags[0].message
    assert checked.macros.fat_g == 9  # the number stands; it is just not "free"


def test_brief_lentil_soup_mix_at_3_4g_sodium_per_100g_is_flagged() -> None:
    checked, flags = check_item(
        item("Lentil soup mix", 170, 12, 28, 1.5, fiber=6, sodium=1700, grams=50, source="label")
    )
    assert codes(flags) == ["sodium_high"]
    assert flags[0].severity == "warn"
    assert "3.4 g sodium per 100 g" in flags[0].message
    assert "1700 mg sodium per serving" in flags[0].message
    assert flags[0].needs_health_context is False
    assert checked.macros.kcal == 170  # label source: no buffer even though soup is loose


def test_brief_smoked_turkey_560mg_flagged_for_cv_risk_profile() -> None:
    turkey = item("Smoked turkey breast", 105, 18, 2, 3, sodium=560, grams=100)
    _, flags = check_item(turkey, health_context="Elevated LDL, cardiovascular risk markers")
    assert codes(flags) == ["sodium_high"]
    assert flags[0].severity == "warn"
    assert flags[0].needs_health_context is True
    assert "560 mg" in flags[0].message
    assert "processed meat" in flags[0].message


def test_smoked_turkey_without_cv_context_is_an_info_note_for_the_agent() -> None:
    turkey = item("Smoked turkey breast", 105, 18, 2, 3, sodium=560, grams=100)
    _, flags = check_item(turkey, health_context="none")
    assert codes(flags) == ["sodium_high"]
    assert flags[0].severity == "info"
    assert flags[0].needs_health_context is True


def test_countable_item_passes_untouched() -> None:
    chicken = item("Grilled chicken breast", 280, 52, 0, 7, grams=200)
    checked, flags = check_item(chicken)
    assert flags == []
    assert checked == chicken


def test_kcal_mismatch_over_10_percent_is_corrected() -> None:
    checked, flags = check_item(item("Protein pancakes", 500, 30, 40, 10))
    assert codes(flags) == ["kcal_mismatch"]
    assert checked.macros.kcal == 370
    assert "using 370" in flags[0].message
    assert "+35%" in flags[0].message


def test_kcal_within_10_percent_is_not_flagged() -> None:
    checked, flags = check_item(item("Protein pancakes", 400, 30, 40, 10))
    assert flags == []
    assert checked.macros.kcal == 400


# ------------------------------------------------------------------- classify_countable


@pytest.mark.parametrize(
    ("name", "countable", "category"),
    [
        ("Spaghetti carbonara", False, "pasta"),
        ("Chicken fillet with rice", False, "rice"),
        ("Mushroom risotto", False, "rice"),
        ("Tonkotsu ramen", False, "noodles"),
        ("Chicken curry", False, "curry"),
        ("Beef stew", False, "stew"),
        ("Oat porridge", False, "porridge"),
        ("Mashed potatoes", False, "mashed"),
        ("Tomato soup", False, "soup"),
        ("Hollandaise sauce", False, "sauce"),
        ("Caesar salad", False, "dressed_salad"),
        ("Green salad with ranch dressing", False, "dressed_salad"),
        ("Berry smoothie", False, "smoothie"),
        ("Chicken shawarma plate", False, "plate"),
        ("Салат оливье", False, "dressed_salad"),
        ("Плов с бараниной", False, "rice"),
        ("Greek salad", True, "unknown"),
        ("Burger bun", True, "bun"),
        ("Flour tortilla", True, "tortilla"),
        ("Salmon fillet", True, "fillet"),
        ("Boiled eggs", True, "egg"),
        ("Beef patty", True, "patty"),
        ("Slice of sourdough", True, "slice"),
        ("Protein bar", True, "bar"),
        ("Can of tuna", True, "can"),
        ("Bottle of kefir", True, "bottle"),
        ("Rice cake", True, "slice"),
        ("Grilled chicken", True, "unknown"),
        ("Roasted eggplant", True, "unknown"),
        ("Boiled potatoes", True, "unknown"),
        ("Hamburger", True, "patty"),
    ],
)
def test_classify_countable(name: str, countable: bool, category: str) -> None:
    assert classify_countable(name) == (countable, category)


# -------------------------------------------------------------------- loose_under_report


def test_loose_buffer_applies_to_kcal_and_carbs_only() -> None:
    checked, flags = check_item(item("Fried rice", 400, 10, 60, 12))
    assert codes(flags) == ["loose_under_report"]
    assert checked.countable is False
    assert checked.macros.kcal == 500
    assert checked.macros.carbs_g == 75
    assert checked.macros.protein_g == 10
    assert checked.macros.fat_g == 12
    assert flags[0].corrected is not None
    assert flags[0].corrected.kcal == 500
    assert "+25%" in flags[0].message


def test_loose_buffer_is_configurable() -> None:
    checked, _ = check_item(item("Fried rice", 400, 10, 60, 12), buffer=0.4)
    assert checked.macros.kcal == 560


def test_loose_buffer_off_still_marks_the_item_loose() -> None:
    checked, flags = check_item(item("Fried rice", 400, 10, 60, 12), buffer=0)
    assert checked.countable is False
    assert checked.macros.kcal == 400
    assert flags[0].code == "loose_under_report"
    assert flags[0].corrected is None


@pytest.mark.parametrize("source", ["user", "label", "usda", "off"])
def test_loose_buffer_never_inflates_weighed_or_user_numbers(source: FoodSource) -> None:
    checked, flags = check_item(item("Basmati rice", 260, 5, 56, 1, grams=200, source=source))
    assert "loose_under_report" not in codes(flags)
    assert checked.macros.kcal == 260
    assert checked.countable is False


def test_model_can_mark_an_unknown_dish_loose() -> None:
    checked, flags = check_item(item("Chef's special", 400, 20, 40, 15, countable=False))
    assert codes(flags) == ["loose_under_report"]
    assert "loose:" in flags[0].message
    assert checked.countable is False


# ------------------------------------------------------------------------ implausible_fiber


def test_fiber_on_animal_only_food_goes_to_zero() -> None:
    checked, flags = check_item(item("Grilled salmon", 280, 30, 0, 17, fiber=3))
    assert codes(flags) == ["implausible_fiber"]
    assert checked.macros.fiber_g == 0


def test_fiber_over_20g_without_legumes_is_capped() -> None:
    checked, flags = check_item(item("Protein pancakes", 370, 30, 40, 10, fiber=25))
    assert codes(flags) == ["implausible_fiber"]
    assert checked.macros.fiber_g == 20


def test_legume_dish_may_carry_high_fiber() -> None:
    _, flags = check_item(item("Lentil dal", 380, 20, 60, 5, fiber=25))
    assert "implausible_fiber" not in codes(flags)


def test_fiber_ceiling_scales_with_slices_of_toast() -> None:
    checked, flags = check_item(item("Eggs and toast", 400, 20, 45, 15, fiber=7, quantity=3))
    assert codes(flags) == ["implausible_fiber"]
    assert checked.macros.fiber_g == 6


def test_fiber_within_ceiling_is_untouched() -> None:
    _, flags = check_item(item("Eggs and toast", 320, 18, 28, 14, fiber=3))
    assert flags == []


# -------------------------------------------------------------------------- implausible_fat


def test_single_ingredient_minimum_fat_uses_grams() -> None:
    checked, flags = check_item(item("Sliced avocado", 100, 2, 8, 5, grams=100))
    assert codes(flags) == ["implausible_fat"]
    assert checked.macros.fat_g == 14  # 15 g/100 g × 100 g × 0.9, rounded
    assert "100 g of avocado" in flags[0].message


def test_two_eggs_cannot_have_four_grams_of_fat() -> None:
    checked, flags = check_item(item("2 eggs", 100, 12, 1, 4, quantity=2))
    assert codes(flags) == ["implausible_fat"]
    assert checked.macros.fat_g == 10


def test_fat_rule_uses_the_fattiest_named_ingredient_once() -> None:
    checked, flags = check_item(item("Salad with avocado, walnuts and olive oil", 330, 6, 12, 26))
    assert flags == []  # 26 g ≥ walnuts alone (30 g × 65 % ≈ 20 g)
    assert checked.macros.fat_g == 26


@pytest.mark.parametrize(
    "name",
    [
        "Cottage cheese 0.5%",
        "Egg white omelette",
        "Almond milk",
        "Smoked salmon",
        "Boiled potatoes",
        "Chocolate protein shake",
    ],
)
def test_fat_rule_exemptions_and_false_positive_guards(name: str) -> None:
    _, flags = check_item(item(name, 120, 12, 6, 1))
    assert "implausible_fat" not in codes(flags)


@pytest.mark.parametrize("source", ["label", "usda", "off"])
def test_trusted_sources_skip_ingredient_corrections(source: FoodSource) -> None:
    checked, flags = check_item(item("Avocado", 60, 2, 8, 2, grams=100, source=source))
    assert "implausible_fat" not in codes(flags)
    assert checked.macros.fat_g == 2


# ------------------------------------------------------------------------- vegetable_fat


def test_roasted_vegetable_side_claiming_no_fat_gets_the_oil() -> None:
    checked, flags = check_item(item("Roasted broccoli", 35, 3, 6, 0.5))
    assert codes(flags) == ["vegetable_fat"]
    assert flags[0].severity == "warn"
    assert checked.macros.fat_g == 7
    assert checked.macros.kcal == round(35 + 6.5 * 9)


def test_vegetable_rule_ignores_dishes_with_protein_or_starch() -> None:
    _, flags = check_item(item("Chicken and broccoli", 400, 45, 10, 18))
    assert "vegetable_fat" not in codes(flags)


def test_steamed_vegetables_with_little_fat_are_fine() -> None:
    _, flags = check_item(item("Steamed green beans", 40, 2, 8, 0.3))
    assert flags == []


# ------------------------------------------------------------------------- kcal_mismatch


def test_kcal_zero_with_macros_is_a_mismatch() -> None:
    checked, flags = check_item(item("Mystery dish", 0, 10, 10, 10))
    assert codes(flags) == ["kcal_mismatch"]
    assert checked.macros.kcal == 170


def test_off_source_uses_the_eu_convention() -> None:
    checked, flags = check_item(item("Bran cereal", 260, 0, 50, 0, fiber=10, source="off"))
    assert codes(flags) == ["kcal_mismatch"]
    assert checked.macros.kcal == 220  # 4 × 50 + 2 × 10


def test_kcal_check_accepts_either_convention() -> None:
    _, flags = check_item(item("Bran cereal", 218, 0, 50, 0, fiber=10, source="label"))
    assert flags == []


def test_alcohol_counts_seven_kcal_per_gram() -> None:
    beer = FoodItemIn(
        name="Beer 500 ml", macros=Macros(kcal=215, protein_g=2, carbs_g=18, fat_g=0, alcohol_g=19)
    )
    _, flags = check_item(beer)
    assert flags == []


# ----------------------------------------------------------------------------- sodium_high


def test_sodium_per_serving_without_grams() -> None:
    _, flags = check_item(item("Instant noodles", 380, 8, 52, 15, sodium=1800))
    assert "sodium_high" in codes(flags)
    assert flags[-1].needs_health_context is False


def test_moderate_sodium_is_silent() -> None:
    _, flags = check_item(item("Grilled chicken breast", 280, 52, 0, 7, sodium=300, grams=200))
    assert flags == []


def test_processed_meat_with_low_sodium_is_silent() -> None:
    _, flags = check_item(item("Smoked turkey breast", 105, 18, 2, 3, sodium=200, grams=100))
    assert flags == []


def test_processed_meat_with_unknown_sodium_still_gets_the_note() -> None:
    _, flags = check_item(item("Ham sandwich", 350, 20, 35, 14))
    assert codes(flags) == ["sodium_high"]
    assert flags[0].needs_health_context is True


def test_processed_meat_and_cv_helpers() -> None:
    assert is_processed_meat("Salami pizza")
    assert is_processed_meat("Копчёная колбаса")
    assert not is_processed_meat("Hamburger")
    assert not is_processed_meat("Smoked salmon")
    assert mentions_cv_risk("LDL 4.2, on statins")
    assert mentions_cv_risk("повышенный холестерин")
    assert not mentions_cv_risk(None)
    assert not mentions_cv_risk("knee surgery 2019")


# ----------------------------------------------------------------------- composition


def test_corrections_compound_in_order_and_each_flag_carries_its_snapshot() -> None:
    checked, flags = check_item(item("Chicken avocado rice bowl", 420, 40, 45, 7))
    assert codes(flags) == ["implausible_fat", "loose_under_report"]
    assert flags[0].corrected is not None
    assert flags[0].corrected.fat_g == 15
    assert flags[0].corrected.kcal == 420 + 72
    assert flags[1].corrected is not None
    assert flags[1].corrected.kcal == round((420 + 72) * 1.25)
    assert checked.macros == flags[1].corrected


def test_check_item_is_deterministic() -> None:
    plate = item("Chicken avocado plate", 303, 42, 18, 7)
    assert check_item(plate) == check_item(plate)


def test_check_items_preserves_order() -> None:
    results = check_items(
        [item("Fried rice", 400, 10, 60, 12), item("Boiled eggs", 140, 12, 1, 10)]
    )
    assert [r[0].name for r in results] == ["Fried rice", "Boiled eggs"]
    assert codes(results[0][1]) == ["loose_under_report"]
    assert results[1][1] == []
