"""Sanity rules on stated food macros (PLAN §5 ``sanity.py``; brief §3.2). Pure, deterministic.

``check_item`` runs every rule on one :class:`FoodItemIn` and returns the corrected item plus the
:class:`Flag` list. Rules that change numbers carry the macros *as of after that rule* in
``Flag.corrected``; the returned item carries the final numbers. Rule order (it matters, because
corrections compound):

1. ``implausible_fiber`` — fibre-free foods claiming fibre; a fibre ceiling per ingredient.
2. ``implausible_fat``   — a dish naming a fatty ingredient cannot carry less fat than that
   ingredient alone (table of minimum fat per named ingredient).
3. ``portion_implausible`` — pasta/rice/noodles at a stated portion ≥ 200 g with < 40 g carbs.
4. ``vegetable_fat``     — a vegetable side at ≥ 6 g fat was cooked in oil (note, no correction);
   a roasted/fried vegetable side claiming < 3 g fat gets the oil added.
5. ``kcal_mismatch``     — |stated − 4/4/9(+7 alcohol)| > 10 % → kcal re-derived from macros.
   Runs after the macro corrections so the re-derivation uses the corrected grams, and before
   the buffer so the buffer is never undone.
6. ``loose_under_report`` — loose foods (pasta, rice, sauces, soups, curries, dressed salads…)
   get ``countable=False`` and ``+buffer`` on kcal and carbs (default 25 %, brief: 20–40 %).
7. ``sodium_high``       — ≥ 600 mg per serving or ≥ 1 500 mg per 100 g; processed meat carries a
   ``needs_health_context`` note (severity ``warn`` when the profile's health context mentions
   lipids / cardio, else ``info``).

Which sources are checked:

* ingredient corrections (1–4) are skipped for weighed/scanned sources (``label``, ``off``,
  ``usda``) — the rules target restaurant, delivery-app and model-estimated numbers, never
  analytical data;
* the buffer (6) applies only to ``model`` and ``web`` numbers — a user's own estimate is
  trusted (brief §3.2: "acknowledge when the user's estimate is better");
* the kcal check (5) and the sodium notes (7) run on every source.

Keyword matching is a substring test on the casefolded name padded with spaces, so a keyword
that starts with a space (``" oil"``) matches only at a word start ("olive oil", not "boiled").

``check_item`` is not idempotent for loose items (the buffer would compound); callers run it once
per entry and persist the flags with the meal item.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from strikt.core.types import Flag, FoodItemIn, FoodSource, Macros
from strikt.nutrition.math import (
    KCAL_PER_G_CARBS,
    KCAL_PER_G_FAT,
    computed_kcal,
    mismatch_ratio,
    round_macros,
)

DEFAULT_BUFFER: Final[float] = 0.25
KCAL_TOLERANCE: Final[float] = 0.10
FIBER_CAP_G: Final[float] = 20.0
FIBER_CAP_LEGUME_G: Final[float] = 45.0
VEGETABLE_FAT_THRESHOLD_G: Final[float] = 6.0
VEGETABLE_OIL_G: Final[float] = 7.0  # half a tablespoon of oil per side
SODIUM_SERVING_MG: Final[float] = 600.0
SODIUM_PER_100G_MG: Final[float] = 1500.0
SODIUM_PROCESSED_MEAT_PER_100G_MG: Final[float] = 500.0
PORTION_LARGE_G: Final[float] = 200.0
PORTION_CARBS_MIN_G: Final[float] = 40.0
ASSUMED_LARGE_PORTION_G: Final[float] = 250.0

TRUSTED_SOURCES: Final[frozenset[FoodSource]] = frozenset({"label", "off", "usda"})
"""Weighed or scanned numbers: no ingredient corrections, no buffer."""
BUFFERED_SOURCES: Final[frozenset[FoodSource]] = frozenset({"model", "web"})
"""Restaurant / delivery-app / model-estimated numbers: the loose-food buffer applies."""

# --------------------------------------------------------------------------- keyword tables


def _kw(*words: str) -> tuple[str, ...]:
    return tuple(w.casefold() for w in words)


LOOSE_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    "pasta": _kw(
        "pasta",
        "spaghetti",
        "penne",
        "fettuccine",
        "linguine",
        "tagliatelle",
        "lasagna",
        "lasagne",
        "macaroni",
        "mac and cheese",
        "gnocchi",
        "ravioli",
        "carbonara",
        "bolognese",
        "паста",
        "макарон",
        "спагетти",
        "лазань",
        "пенне",
    ),
    "rice": _kw(
        " rice",
        "risotto",
        "pilaf",
        " plov",
        "biryani",
        "machboos",
        "paella",
        " рис",
        "ризотто",
        " плов",
        "бирьяни",
    ),
    "noodles": _kw(
        "noodle",
        "ramen",
        " udon",
        " soba",
        " pho ",
        "pad thai",
        "lo mein",
        "chow mein",
        "vermicelli",
        "лапш",
        "рамен",
        " удон",
        "фунчоз",
    ),
    "porridge": _kw("porridge", "oatmeal", " oats", "congee", " grits", " каша", "овсянк", "гречк"),
    "mashed": _kw(" mash", "puree", " пюре", "hummus", " хумус", "guacamole", "гуакамоле"),
    "soup": _kw(
        " soup", " broth", "chowder", "bisque", " суп", "бульон", " борщ", " щи ", "солянк", "харчо"
    ),
    "curry": _kw(" curry", "masala", " korma", " dal ", " dhal", " daal", " карри", "масала"),
    "stew": _kw(
        " stew",
        "ragout",
        " ragu",
        "goulash",
        " chili",
        " chilli",
        "tagine",
        " рагу",
        "гуляш",
        " чили",
    ),
    "sauce": _kw(
        " sauce",
        " gravy",
        "dressing",
        " dip ",
        " dips",
        "aioli",
        " pesto",
        " соус",
        "подлив",
        "заправк",
        " дип",
    ),
    "dressed_salad": _kw(
        "caesar",
        "coleslaw",
        " slaw",
        "potato salad",
        "tuna salad",
        "chicken salad",
        "egg salad",
        "olivier",
        "оливье",
        "цезарь",
        "салат с майонез",
        "salad with dressing",
        "dressed salad",
        "крабовый салат",
        "мимоза",
        "под шубой",
        "waldorf",
    ),
    "smoothie": _kw("smoothie", " shake", " смузи", " шейк", "acai bowl", " асаи"),
    "plate": _kw(
        "shawarma plate",
        "shawarma platter",
        "mixed grill",
        "platter",
        " bowl",
        " poke",
        "шаурма на тарелке",
        " боул",
        " поке",
    ),
}
"""Loose foods: the stated numbers are usually under-reported (brief §3.2)."""

COUNTABLE_CATEGORIES: Final[Mapping[str, tuple[str, ...]]] = {
    "bun": _kw(
        " bun", " roll", "bagel", "croissant", "булоч", "бейгл", "круассан", " pita", " пита"
    ),
    "tortilla": _kw(
        "tortilla", " wrap", " taco", "burrito", "тортиль", " лаваш", " тако", "буррито"
    ),
    "fillet": _kw(
        "fillet",
        " filet",
        "breast",
        "steak",
        " chop ",
        " chops",
        "drumstick",
        "thigh",
        " wing",
        " филе",
        "грудк",
        "стейк",
        " бедр",
        " голен",
        " крыл",
    ),
    "egg": _kw(" egg ", " eggs", " яйц"),
    "patty": _kw(
        "patty", "burger", "cutlet", "meatball", "sausage", "котлет", "бургер", "сосиск", "фрикадел"
    ),
    "slice": _kw("slice", "toast", "bread", "pizza", "ломтик", " тост", " хлеб", " пицц"),
    "bar": _kw(" bar ", " bars", "батончик", "cookie", "печень", "cracker", "крекер"),
    "can": _kw(" can ", " cans", "canned", " tin ", " банк"),
    "bottle": _kw("bottle", "бутыл"),
    "piece": _kw(" piece", " pcs", " шт"),
}
"""Countable foods: buns, tortillas, fillets, eggs, patties, slices, bars, cans, bottles."""

_COUNTABLE_EXCEPTIONS: Final[tuple[str, ...]] = _kw(
    "rice cake",
    "rice cracker",
    "rice paper",
    "soup dumpling",
    "рисовые хлебцы",
    "хлебц",
)
"""Names that contain a loose keyword but are countable (checked first)."""

_LOOSE_SALAD_MARKERS: Final[tuple[str, ...]] = _kw(
    "dressing",
    "dressed",
    " mayo",
    "creamy",
    " ranch",
    "майонез",
    "заправк",
    "сметан",
)

FIBER_FREE: Final[tuple[str, ...]] = _kw(
    " egg ",
    " eggs",
    "chicken",
    " beef",
    "steak",
    " pork",
    " lamb",
    "turkey",
    " duck",
    "salmon",
    " tuna",
    " fish",
    "shrimp",
    "prawn",
    " cod ",
    "seabass",
    " meat",
    "cheese",
    "yogurt",
    "yoghurt",
    " milk",
    "butter",
    " whey",
    "cottage",
    "kefir",
    " cream",
    " skyr",
    " ghee",
    " яйц",
    " куриц",
    " курин",
    "говяд",
    "свинин",
    "баран",
    "индейк",
    "лосос",
    "сёмг",
    "семг",
    "тунец",
    " рыб",
    "кревет",
    " мяс",
    " сыр ",
    " сыра",
    "йогурт",
    "молок",
    "творог",
    "кефир",
    "сливк",
    "протеин",
)
"""Animal foods carry no dietary fibre."""

FIBER_CEILING_G: Final[Mapping[str, float]] = {
    "toast": 4.0,
    "bread": 4.0,
    " тост": 4.0,
    " хлеб": 4.0,
    "bagel": 3.0,
    "croissant": 2.0,
    "tortilla": 3.0,
    " wrap": 4.0,
    " pita": 3.0,
    " bun": 2.0,
    "булоч": 2.0,
    " rice": 3.0,
    " рис": 3.0,
    "pasta": 5.0,
    "паста": 5.0,
    "spaghetti": 5.0,
    "noodle": 3.0,
    "лапш": 3.0,
    "potato": 5.0,
    "картоф": 5.0,
    "fries": 4.0,
    "salad": 6.0,
    "салат": 6.0,
    "lettuce": 1.0,
    "cucumber": 1.0,
    "огурец": 1.0,
    "tomato": 2.0,
    "vegetable": 8.0,
    "veggie": 8.0,
    " овощ": 8.0,
    "greens": 4.0,
    "зелень": 4.0,
    "eggplant": 6.0,
    "aubergine": 6.0,
    "баклажан": 6.0,
    "fruit": 6.0,
    "фрукт": 6.0,
    "banana": 3.5,
    "банан": 3.5,
    "apple": 5.0,
    "яблок": 5.0,
    "orange": 3.5,
    "berries": 8.0,
    " ягод": 8.0,
    "berry": 8.0,
    " corn": 6.0,
    "кукуруз": 6.0,
    "granola": 8.0,
    "гранол": 8.0,
    "muesli": 8.0,
    "мюсли": 8.0,
    " nuts": 6.0,
    " орех": 6.0,
    "almond": 6.0,
    "миндал": 6.0,
    "walnut": 4.0,
    "peanut": 5.0,
    "арахис": 5.0,
    "dark chocolate": 5.0,
    "chocolate": 3.0,
    "шоколад": 3.0,
    "pizza": 6.0,
    " пицц": 6.0,
    "burger": 4.0,
    "бургер": 4.0,
    "sandwich": 6.0,
    "сэндвич": 6.0,
    "wholegrain": 10.0,
    "whole grain": 10.0,
    "wholemeal": 10.0,
    "цельнозерн": 10.0,
    "sourdough": 5.0,
    "cereal": 8.0,
    "хлопья": 8.0,
}
"""Maximum plausible fibre for one dish serving containing this ingredient (used when the item
names no legume/bran source). The ceiling for a dish is the maximum over matched keywords."""

FIBER_RICH: Final[tuple[str, ...]] = _kw(
    "lentil",
    " bean",
    "chickpea",
    "hummus",
    "edamame",
    " peas",
    "green pea",
    "split pea",
    " bran ",
    "oat bran",
    "wheat bran",
    "all-bran",
    "psyllium",
    " chia",
    " flax",
    " oats",
    "oatmeal",
    "avocado",
    "brussels",
    "broccoli",
    "artichoke",
    "quinoa",
    "raspberr",
    "blackberr",
    "fiber",
    "fibre",
    "inulin",
    "falafel",
    "чечевиц",
    "фасол",
    " нут ",
    " нута",
    " хумус",
    "эдамаме",
    " горох",
    "отруб",
    " чиа",
    " лён",
    " льня",
    "овсян",
    "авокадо",
    "брюссельск",
    "брокколи",
    "артишок",
    "киноа",
    " малин",
    "ежевик",
    "клетчатк",
    "фалафел",
)
"""Legumes, bran, seeds and a few vegetables that legitimately reach 20 g+ per dish."""

MIN_FAT_PER_100G: Final[Mapping[str, float]] = {
    "avocado": 15.0,
    "авокадо": 15.0,
    "guacamole": 14.0,
    "гуакамоле": 14.0,
    "almond": 50.0,
    "миндал": 50.0,
    "walnut": 65.0,
    "грецк": 65.0,
    "cashew": 44.0,
    "кешью": 44.0,
    "pistachio": 45.0,
    "фисташ": 45.0,
    "peanut": 49.0,
    "арахис": 49.0,
    "hazelnut": 61.0,
    "фундук": 61.0,
    "macadamia": 76.0,
    "pecan": 72.0,
    " nuts": 50.0,
    " орех": 50.0,
    "peanut butter": 50.0,
    "almond butter": 55.0,
    "арахисовая паста": 50.0,
    "olive oil": 100.0,
    "оливковое масло": 100.0,
    " oil": 100.0,
    " масл": 82.0,
    "butter": 81.0,
    " ghee": 99.0,
    "tahini": 54.0,
    "тахин": 54.0,
    " mayo": 75.0,
    "майонез": 75.0,
    "aioli": 70.0,
    " pesto": 45.0,
    " песто": 45.0,
    "salmon": 12.0,
    "лосос": 12.0,
    "сёмг": 12.0,
    "семг": 12.0,
    "mackerel": 14.0,
    "скумбри": 14.0,
    "sardine": 11.0,
    "сардин": 11.0,
    "cheese": 25.0,
    " сыр": 25.0,
    "cheddar": 33.0,
    "чеддер": 33.0,
    "mozzarella": 20.0,
    "моцарелл": 20.0,
    " feta": 21.0,
    " фета": 21.0,
    "parmesan": 28.0,
    "пармезан": 28.0,
    " brie": 28.0,
    "halloumi": 25.0,
    "халлуми": 25.0,
    "burrata": 25.0,
    "буррат": 25.0,
    " gouda": 27.0,
    "egg yolk": 27.0,
    " yolk": 27.0,
    "желток": 27.0,
    " egg ": 10.0,
    " eggs": 10.0,
    " яйц": 10.0,
    "bacon": 40.0,
    "бекон": 40.0,
    "sausage": 25.0,
    "сосиск": 25.0,
    "колбас": 25.0,
    "salami": 33.0,
    "салями": 33.0,
    "chorizo": 38.0,
    "pepperoni": 40.0,
    "пепперони": 40.0,
    " cream": 35.0,
    "сливк": 35.0,
    "sour cream": 20.0,
    "сметан": 20.0,
    "coconut milk": 20.0,
    "кокосовое молоко": 20.0,
    "coconut": 33.0,
    " chia": 31.0,
    " чиа": 31.0,
    " flax": 42.0,
    " лён": 42.0,
    " seeds": 45.0,
    "семеч": 45.0,
    "dark chocolate": 40.0,
    "chocolate": 30.0,
    "шоколад": 30.0,
    "hummus": 10.0,
    " хумус": 10.0,
    " olive": 11.0,
    " олив": 11.0,
}
"""Fat per 100 g of the named ingredient (USDA SR Legacy, rounded down)."""

TYPICAL_INGREDIENT_G: Final[Mapping[str, float]] = {
    "avocado": 100.0,
    "авокадо": 100.0,
    "guacamole": 60.0,
    "гуакамоле": 60.0,
    "almond": 30.0,
    "миндал": 30.0,
    "walnut": 30.0,
    "грецк": 30.0,
    "cashew": 30.0,
    "кешью": 30.0,
    "pistachio": 30.0,
    "фисташ": 30.0,
    "peanut": 30.0,
    "арахис": 30.0,
    "hazelnut": 30.0,
    "фундук": 30.0,
    "macadamia": 30.0,
    "pecan": 30.0,
    " nuts": 30.0,
    " орех": 30.0,
    "peanut butter": 32.0,
    "almond butter": 32.0,
    "арахисовая паста": 32.0,
    "olive oil": 14.0,
    "оливковое масло": 14.0,
    " oil": 14.0,
    " масл": 10.0,
    "butter": 10.0,
    " ghee": 10.0,
    "tahini": 15.0,
    "тахин": 15.0,
    " mayo": 15.0,
    "майонез": 15.0,
    "aioli": 15.0,
    " pesto": 30.0,
    " песто": 30.0,
    "salmon": 120.0,
    "лосос": 120.0,
    "сёмг": 120.0,
    "семг": 120.0,
    "mackerel": 100.0,
    "скумбри": 100.0,
    "sardine": 90.0,
    "сардин": 90.0,
    "cheese": 30.0,
    " сыр": 30.0,
    "cheddar": 30.0,
    "чеддер": 30.0,
    "mozzarella": 40.0,
    "моцарелл": 40.0,
    " feta": 30.0,
    " фета": 30.0,
    "parmesan": 15.0,
    "пармезан": 15.0,
    " brie": 30.0,
    "halloumi": 60.0,
    "халлуми": 60.0,
    "burrata": 60.0,
    "буррат": 60.0,
    " gouda": 30.0,
    "egg yolk": 17.0,
    " yolk": 17.0,
    "желток": 17.0,
    " egg ": 50.0,
    " eggs": 50.0,
    " яйц": 50.0,
    "bacon": 25.0,
    "бекон": 25.0,
    "sausage": 60.0,
    "сосиск": 60.0,
    "колбас": 40.0,
    "salami": 30.0,
    "салями": 30.0,
    "chorizo": 30.0,
    "pepperoni": 30.0,
    "пепперони": 30.0,
    " cream": 30.0,
    "сливк": 30.0,
    "sour cream": 30.0,
    "сметан": 30.0,
    "coconut milk": 100.0,
    "кокосовое молоко": 100.0,
    "coconut": 30.0,
    " chia": 15.0,
    " чиа": 15.0,
    " flax": 15.0,
    " лён": 15.0,
    " seeds": 20.0,
    "семеч": 20.0,
    "dark chocolate": 30.0,
    "chocolate": 30.0,
    "шоколад": 30.0,
    "hummus": 60.0,
    " хумус": 60.0,
    " olive": 25.0,
    " олив": 25.0,
}
"""Typical amount of the ingredient present when a dish names it (one avocado ≈ 100 g edible,
a handful of nuts 30 g, a tablespoon of oil 14 g, a fillet of salmon 120 g…)."""

_FAT_RULE_EXEMPT: Final[tuple[str, ...]] = _kw(
    "cottage",
    "творог",
    "0%",
    "0.5%",
    "0,5%",
    " 1%",
    " 2%",
    "fat-free",
    "fat free",
    "nonfat",
    "non-fat",
    "low-fat",
    "low fat",
    "lowfat",
    " skim",
    " light",
    " lite",
    "обезжир",
    "нежирн",
    "лёгк",
    "легк",
    "egg white",
    " белок",
    "white omelette",
    "yolk-free",
    "без желтк",
    "oil-free",
    "без масла",
    " spray",
    "cocoa",
    "какао",
    "protein bar",
    "протеиновый батончик",
    "smoked salmon",
    "копчён",
    "копчен",
    "flavo",
    "hot chocolate",
    "chocolate protein",
    "vanilla",
    "ваниль",
    " milk",
    "молок",
    "yogurt",
    "yoghurt",
    "йогурт",
    "coconut water",
    "кокосовая вода",
    "cheesecake",
    "чизкейк",
    "chocolate cake",
    "brownie",
)
"""Names whose fatty keyword is a flavour or a lean variant: the fat rule stays silent."""

_SINGLE_INGREDIENT_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "raw",
        "cooked",
        "fresh",
        "sliced",
        "diced",
        "chopped",
        "grilled",
        "baked",
        "roasted",
        "smoked",
        "organic",
        "half",
        "whole",
        "ripe",
        "medium",
        "large",
        "small",
        "piece",
        "pieces",
        "of",
        "a",
        "an",
        "the",
        "fillet",
        "filet",
        "steak",
        "breast",
        "slices",
        "slice",
        "portion",
        "handful",
        "tbsp",
        "tsp",
        "g",
        "grams",
        "ml",
        "cup",
        "cups",
        "x",
        "extra",
        "virgin",
        "atlantic",
        "farmed",
        "wild",
        "hass",
        "сырой",
        "свежий",
        "свежая",
        "жареный",
        "запечённый",
        "ломтик",
        "ломтики",
        "половина",
        "штук",
        "шт",
        "г",
        "гр",
        "мл",
        "кусок",
    }
)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-zа-яё%]+", re.IGNORECASE)
_MEAT_MULTI_PIECE: Final[tuple[str, ...]] = _kw(" egg", " яйц")

VEGETABLE_KEYWORDS: Final[tuple[str, ...]] = _kw(
    "brussels",
    "broccoli",
    "sprouts",
    "cauliflower",
    "zucchini",
    "courgette",
    "asparagus",
    "green beans",
    "spinach",
    " kale",
    "carrot",
    "pepper",
    "mushroom",
    "eggplant",
    "aubergine",
    "cabbage",
    "vegetable",
    "veggie",
    "greens",
    " leek",
    "beetroot",
    " beet",
    "pumpkin",
    "squash",
    " okra",
    "bok choy",
    "pak choi",
    "artichoke",
    "fennel",
    "celery",
    "tomato",
    "onion",
    "salad",
    " овощ",
    "брокколи",
    "брюссельск",
    "капуст",
    "цветн",
    "кабач",
    "цукини",
    "спарж",
    "шпинат",
    "морков",
    " перц",
    " перец",
    " гриб",
    "баклажан",
    "свекл",
    "свёкл",
    "тыкв",
    " лук",
    "салат",
    "стручков",
)
_NOT_ONLY_VEGETABLE: Final[tuple[str, ...]] = _kw(
    "chicken",
    " beef",
    "steak",
    " pork",
    " lamb",
    "salmon",
    " fish",
    "shrimp",
    " egg ",
    " eggs",
    "cheese",
    " tofu",
    " rice",
    "pasta",
    "noodle",
    "potato",
    "fries",
    "bread",
    "quinoa",
    "lentil",
    "black bean",
    "kidney bean",
    "baked bean",
    "refried",
    "chickpea",
    "avocado",
    " nuts",
    "halloumi",
    " feta",
    "burrata",
    "bacon",
    "sausage",
    "tempura",
    "fritter",
    "cutlet",
    "burger",
    " куриц",
    " курин",
    "говяд",
    "стейк",
    "свинин",
    "баран",
    "лосос",
    " рыб",
    "кревет",
    " яйц",
    " сыр",
    " тофу",
    " рис",
    "паста",
    "лапш",
    "картоф",
    " хлеб",
    "киноа",
    "чечевиц",
    "фасол",
    "авокадо",
    " орех",
    "бекон",
    "сосиск",
    " кляр",
    "котлет",
)
_OIL_COOKED: Final[tuple[str, ...]] = _kw(
    "roasted",
    " roast",
    "fried",
    "sauteed",
    "sautéed",
    "stir-fried",
    "stir fried",
    "crispy",
    "жарен",
    "обжарен",
    "запечён",
    "запечен",
    "тушён",
    "тушен",
)

PROCESSED_MEAT: Final[tuple[str, ...]] = _kw(
    "smoked",
    " ham ",
    " ham,",
    " hams",
    "bacon",
    "salami",
    "sausage",
    "pastrami",
    "prosciutto",
    "pepperoni",
    " deli ",
    "cured",
    "jerky",
    "hot dog",
    "hotdog",
    "chorizo",
    "mortadella",
    "bresaola",
    "cold cuts",
    "luncheon",
    " spam",
    "копчён",
    "копчен",
    "ветчин",
    "колбас",
    "бекон",
    "сосиск",
    "сардельк",
    "салями",
    "пастрами",
    "прошутто",
    "нарезк",
    "карбонад",
    "буженин",
)
_NOT_MEAT: Final[tuple[str, ...]] = _kw(
    "salmon",
    "trout",
    " fish",
    "mackerel",
    "tofu",
    "cheese",
    "paprika",
    "лосос",
    "сёмг",
    "семг",
    "форел",
    " рыб",
    "скумбри",
    "тофу",
    " сыр",
    "паприк",
)
_CV_CONTEXT: Final[tuple[str, ...]] = _kw(
    "lipid",
    "cardio",
    "cholesterol",
    " ldl",
    "apob",
    "apo b",
    "hypertension",
    "blood pressure",
    "statin",
    "heart",
    "cv risk",
    " cvd",
    "atheroscler",
    "triglycer",
    "холестерин",
    "липид",
    "давлен",
    "сердц",
    "гипертон",
    "статин",
    "атеросклер",
    "триглицерид",
)

_LARGE_PORTION: Final[tuple[str, ...]] = _kw(
    "large",
    " big ",
    " xl",
    "double",
    "family",
    "jumbo",
    "большая",
    "большой",
    "большую",
    "двойн",
)
STARCH_CARBS_PER_100G: Final[Mapping[str, float]] = {"pasta": 30.0, "rice": 28.0, "noodles": 25.0}
"""Carbohydrate per 100 g of the cooked starch (USDA SR Legacy, rounded)."""


# ------------------------------------------------------------------------------- helpers


def _norm(name: str) -> str:
    """Casefolded, single-spaced and padded with one space on each side (word-start matching)."""
    return " " + re.sub(r"\s+", " ", name.casefold()).strip() + " "


def _has(name: str, words: Sequence[str]) -> bool:
    return any(word in name for word in words)


def _matches(name: str, table: Mapping[str, float]) -> list[str]:
    return [key for key in table if key in name]


def _with(macros: Macros, **changes: float | None) -> Macros:
    return macros.model_copy(update=changes)


def _pct(ratio: float) -> str:
    return f"{ratio * 100:+.0f}%"


def classify_countable(name: str) -> tuple[bool, str]:
    """``(countable, category)`` from keyword tables.

    Loose wins over countable when both match ("chicken fillet with rice" is a loose plate);
    a plain "salad" is loose only when a dressing marker is present. Unknown names are treated
    as countable with category ``"unknown"`` (stated numbers trusted, no buffer).
    """
    lowered = _norm(name)
    if _has(lowered, _COUNTABLE_EXCEPTIONS):
        return True, "slice"
    for category, words in LOOSE_CATEGORIES.items():
        if _has(lowered, words):
            return False, category
    if ("salad" in lowered or "салат" in lowered) and _has(lowered, _LOOSE_SALAD_MARKERS):
        return False, "dressed_salad"
    for category, words in COUNTABLE_CATEGORIES.items():
        if _has(lowered, words):
            return True, category
    return True, "unknown"


def is_processed_meat(name: str) -> bool:
    lowered = _norm(name)
    return _has(lowered, PROCESSED_MEAT) and not _has(lowered, _NOT_MEAT)


def mentions_cv_risk(health_context: str | None) -> bool:
    """True when the profile's health context names lipids, cardio or blood-pressure markers."""
    return bool(health_context) and _has(_norm(health_context or ""), _CV_CONTEXT)


def _starch_category(name: str) -> str | None:
    for category in STARCH_CARBS_PER_100G:
        if _has(name, LOOSE_CATEGORIES[category]):
            return category
    return None


def _is_single_ingredient(name: str, key: str) -> bool:
    """True when the name is just the ingredient plus descriptors ("sliced avocado")."""
    remaining = name.replace(key.strip(), " ")
    tokens = [t for t in _TOKEN_RE.findall(remaining) if t not in _SINGLE_INGREDIENT_STOPWORDS]
    return not tokens


# --------------------------------------------------------------------------------- rules


def _rule_fiber(name: str, item: FoodItemIn, macros: Macros) -> tuple[Macros, Flag | None]:
    fiber = macros.fiber_g
    if fiber <= 0:
        return macros, None
    rich = _has(name, FIBER_RICH)
    plant_matches = _matches(name, FIBER_CEILING_G)
    animal = _has(name, FIBER_FREE)
    if rich:
        ceiling = FIBER_CAP_LEGUME_G
        reason = "even a legume/bran dish does not reach that"
    elif plant_matches:
        ceiling = max(FIBER_CEILING_G[key] for key in plant_matches)
        if (
            item.quantity
            and item.quantity > 1
            and _has(name, ("toast", "bread", "slice", " тост", " хлеб"))
        ):
            ceiling = max(ceiling, 2.0 * item.quantity)
        plants = ", ".join(key.strip() for key in plant_matches[:2])
        reason = f"{'eggs/meat/dairy have no fibre; ' if animal else ''}{plants} carries at most ~{ceiling:g} g"
    elif animal:
        ceiling = 0.0
        reason = "eggs, meat and dairy have no fibre"
    else:
        ceiling = FIBER_CAP_G
        reason = "no single dish without legumes or bran reaches 20 g"
    if fiber <= ceiling + 0.5:
        return macros, None
    corrected = _with(macros, fiber_g=ceiling)
    return corrected, Flag(
        code="implausible_fiber",
        severity="warn",
        message=f"fibre {fiber:g} g → {ceiling:g} g: {reason}",
        corrected=round_macros(corrected),
    )


def _min_fat_for(name: str, item: FoodItemIn) -> tuple[float, str] | None:
    matches = _matches(name, MIN_FAT_PER_100G)
    if not matches:
        return None
    best_key = max(matches, key=lambda key: (len(key.strip()), MIN_FAT_PER_100G[key]))
    label = best_key.strip()
    per100 = MIN_FAT_PER_100G[best_key]
    if item.grams and _is_single_ingredient(name, best_key):
        minimum = per100 * item.grams / 100.0 * 0.9
        return minimum, f"{item.grams:g} g of {label} alone is ≥ {minimum:.0f} g fat"
    typical = TYPICAL_INGREDIENT_G[best_key]
    if item.quantity and item.quantity > 1 and _has(best_key, _MEAT_MULTI_PIECE):
        typical *= item.quantity
    minimum = per100 * typical / 100.0
    return minimum, f"{label} alone ({typical:g} g) is ≥ {minimum:.0f} g fat"


def _rule_fat(name: str, item: FoodItemIn, macros: Macros) -> tuple[Macros, Flag | None]:
    if _has(name, _FAT_RULE_EXEMPT):
        return macros, None
    found = _min_fat_for(name, item)
    if found is None:
        return macros, None
    minimum, reason = found
    minimum = float(round(minimum))
    if macros.fat_g >= minimum - 0.5:
        return macros, None
    delta = minimum - macros.fat_g
    corrected = _with(macros, fat_g=minimum, kcal=macros.kcal + delta * KCAL_PER_G_FAT)
    return corrected, Flag(
        code="implausible_fat",
        severity="warn",
        message=f"fat {macros.fat_g:g} g → {minimum:g} g: {reason}",
        corrected=round_macros(corrected),
    )


def _rule_portion(name: str, item: FoodItemIn, macros: Macros) -> tuple[Macros, Flag | None]:
    starch = _starch_category(name)
    if starch is None:
        return macros, None
    grams = item.grams
    assumed = False
    if grams is None and _has(name, _LARGE_PORTION):
        grams, assumed = ASSUMED_LARGE_PORTION_G, True
    if grams is None or grams < PORTION_LARGE_G or macros.carbs_g >= PORTION_CARBS_MIN_G:
        return macros, None
    expected = STARCH_CARBS_PER_100G[starch] * grams / 100.0
    delta = expected - macros.carbs_g
    corrected = _with(macros, carbs_g=expected, kcal=macros.kcal + delta * KCAL_PER_G_CARBS)
    portion = f"~{grams:g} g (large)" if assumed else f"{grams:g} g"
    return corrected, Flag(
        code="portion_implausible",
        severity="warn",
        message=(
            f"carbs {macros.carbs_g:g} g → {expected:.0f} g: {portion} of cooked {starch} is "
            f"~{STARCH_CARBS_PER_100G[starch]:g} g carbs per 100 g"
        ),
        corrected=round_macros(corrected),
    )


def _rule_vegetable_fat(name: str, macros: Macros, *, trusted: bool) -> tuple[Macros, Flag | None]:
    if not _has(name, VEGETABLE_KEYWORDS) or _has(name, _NOT_ONLY_VEGETABLE):
        return macros, None
    if macros.fat_g >= VEGETABLE_FAT_THRESHOLD_G:
        oil = macros.fat_g / 14.0 * 15.0
        return macros, Flag(
            code="vegetable_fat",
            severity="info",
            message=(
                f"{macros.fat_g:g} g fat in a vegetable side = roasted in oil (~{oil:.0f} ml); "
                "counted, not free"
            ),
        )
    if not trusted and macros.fat_g < 3.0 and _has(name, _OIL_COOKED):
        delta = VEGETABLE_OIL_G - macros.fat_g
        corrected = _with(macros, fat_g=VEGETABLE_OIL_G, kcal=macros.kcal + delta * KCAL_PER_G_FAT)
        return corrected, Flag(
            code="vegetable_fat",
            severity="warn",
            message=(
                f"fat {macros.fat_g:g} g → {VEGETABLE_OIL_G:g} g: roasted/fried vegetables carry "
                "about half a tablespoon of oil"
            ),
            corrected=round_macros(corrected),
        )
    return macros, None


def _rule_kcal(item: FoodItemIn, macros: Macros) -> tuple[Macros, Flag | None]:
    us = computed_kcal(macros, convention="us")
    eu = computed_kcal(macros, convention="eu")
    ratio_us = mismatch_ratio(macros.kcal, us)
    ratio_eu = mismatch_ratio(macros.kcal, eu)
    if min(ratio_us, ratio_eu) <= KCAL_TOLERANCE:
        return macros, None
    if us <= 0 and macros.kcal <= 0:
        return macros, None
    target = eu if item.source == "off" else us
    signed = (macros.kcal - target) / target if target > 0 else 1.0
    corrected = _with(macros, kcal=target)
    return corrected, Flag(
        code="kcal_mismatch",
        severity="warn",
        message=(
            f"kcal {macros.kcal:.0f} stated vs {target:.0f} from 4/4/9 ({_pct(signed)}, "
            f"tolerance ±{KCAL_TOLERANCE * 100:.0f}%); using {target:.0f}"
        ),
        corrected=round_macros(corrected),
    )


def _rule_loose(category: str, macros: Macros, buffer: float) -> tuple[Macros, Flag]:
    if buffer <= 0:
        return macros, Flag(
            code="loose_under_report",
            severity="info",
            message=f"{category}: loose food, stated numbers are usually under-reported (buffer off)",
        )
    corrected = _with(
        macros, kcal=macros.kcal * (1 + buffer), carbs_g=macros.carbs_g * (1 + buffer)
    )
    return corrected, Flag(
        code="loose_under_report",
        severity="info",
        message=(
            f"{category}: loose food, typically under-reported by 20–40%; "
            f"+{buffer * 100:.0f}% on kcal and carbs ({macros.kcal:.0f} → {corrected.kcal:.0f} kcal)"
        ),
        corrected=round_macros(corrected),
    )


def _rule_sodium(
    name: str, item: FoodItemIn, macros: Macros, health_context: str | None
) -> Flag | None:
    sodium = macros.sodium_mg
    per100: float | None = None
    if sodium is not None and item.grams:
        per100 = sodium / item.grams * 100.0
    processed = is_processed_meat(name)
    cv = mentions_cv_risk(health_context)
    parts: list[str] = []
    if sodium is not None and sodium >= SODIUM_SERVING_MG:
        parts.append(f"{sodium:.0f} mg sodium per serving (≥ {SODIUM_SERVING_MG:.0f})")
    if per100 is not None and per100 >= SODIUM_PER_100G_MG:
        parts.append(
            f"{per100 / 1000:.1f} g sodium per 100 g (≥ {SODIUM_PER_100G_MG / 1000:.1f} g)"
        )
    if parts:
        message = "; ".join(parts) + " — salty day, expect water weight tomorrow"
        if processed:
            message += "; processed meat: fine as an episode, not as a daily base"
        return Flag(
            code="sodium_high",
            severity="warn",
            message=message,
            needs_health_context=processed,
        )
    if processed and (per100 is None or per100 >= SODIUM_PROCESSED_MEAT_PER_100G_MG):
        detail = f", {per100:.0f} mg sodium per 100 g" if per100 is not None else ""
        return Flag(
            code="sodium_high",
            severity="warn" if cv else "info",
            message=(
                f"processed meat{detail}: fine as an episode, not as a daily base"
                + (" (profile carries cardiovascular risk markers)" if cv else "")
            ),
            needs_health_context=True,
        )
    return None


# ---------------------------------------------------------------------------- entry point


def check_item(
    item: FoodItemIn,
    *,
    health_context: str | None = None,
    buffer: float = DEFAULT_BUFFER,
) -> tuple[FoodItemIn, list[Flag]]:
    """Run every sanity rule on one item; return the corrected item and its flags.

    ``buffer`` is the loose-food under-report buffer (0.25 = +25 %; brief: 20–40 %).
    ``health_context`` is the profile's free-text health context; it only changes the severity
    of the processed-meat note, which always carries ``needs_health_context=True``.
    """
    name = _norm(item.name)
    trusted = item.source in TRUSTED_SOURCES
    flags: list[Flag] = []
    macros = item.macros
    classified_countable, category = classify_countable(item.name)
    countable = item.countable and classified_countable

    def apply(result: tuple[Macros, Flag | None]) -> None:
        nonlocal macros
        macros, flag = result
        if flag is not None:
            flags.append(flag)

    if not trusted:
        apply(_rule_fiber(name, item, macros))
        apply(_rule_fat(name, item, macros))
        apply(_rule_portion(name, item, macros))
    apply(_rule_vegetable_fat(name, macros, trusted=trusted))
    apply(_rule_kcal(item, macros))
    portion_fixed = any(flag.code == "portion_implausible" for flag in flags)
    if not countable and item.source in BUFFERED_SOURCES and not portion_fixed:
        # A portion correction already replaced the stated carbs with a grams-based estimate;
        # buffering on top of it would overshoot (brief: a real large pasta is 60–80 g carbs).
        apply(_rule_loose(category if not classified_countable else "loose", macros, buffer))
    sodium_flag = _rule_sodium(name, item, macros, health_context)
    if sodium_flag is not None:
        flags.append(sodium_flag)

    corrected_item = item.model_copy(
        update={"macros": round_macros(macros), "countable": countable}
    )
    return corrected_item, flags


def check_items(
    items: Sequence[FoodItemIn],
    *,
    health_context: str | None = None,
    buffer: float = DEFAULT_BUFFER,
) -> list[tuple[FoodItemIn, list[Flag]]]:
    """``check_item`` over a meal, preserving order."""
    return [check_item(item, health_context=health_context, buffer=buffer) for item in items]
