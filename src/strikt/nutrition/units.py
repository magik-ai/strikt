"""Quantity parsing and gram conversion (PLAN §5 ``units.py``). Pure, no IO.

Two tables drive everything:

* :data:`UNIT_TO_GRAMS` — mass and volume units to grams (volume assumes density 1.0 unless the
  caller passes one; cup/tbsp/tsp are the US customary approximations).
* :data:`PIECE_GRAMS` — default weight of one *piece* of a common food, keyed by a keyword
  found in the food name ("2 eggs" → 2 × 50 g). Values are typical edible portions, not
  precise; the sanity layer and the user correct them.

``parse_quantity`` turns "200 g" / "2 eggs" / "160g" / "1/2 avocado" / "полстакана" into
``(quantity, unit)``; ``to_grams`` turns that into grams when the unit (or the food) is known.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

# ------------------------------------------------------------------------------- units

UNIT_TO_GRAMS: Final[Mapping[str, float]] = {
    "g": 1.0,
    "mg": 0.001,
    "kg": 1000.0,
    "oz": 28.3495,
    "lb": 453.592,
    "ml": 1.0,  # density 1.0 by default; see ``to_grams(density=)``
    "l": 1000.0,
    "floz": 29.5735,
    "cup": 240.0,
    "tbsp": 15.0,
    "tsp": 5.0,
    "glass": 250.0,
    "shot": 44.0,
}
"""Grams per unit. Volume units are converted at density 1.0 unless ``density`` is given."""

_VOLUME_UNITS: Final[frozenset[str]] = frozenset(
    {"ml", "l", "floz", "cup", "tbsp", "tsp", "glass", "shot"}
)

UNIT_ALIASES: Final[Mapping[str, str]] = {
    "g": "g",
    "gr": "g",
    "gram": "g",
    "grams": "g",
    "gramm": "g",
    "г": "g",
    "гр": "g",
    "грамм": "g",
    "грамма": "g",
    "граммов": "g",
    "mg": "mg",
    "мг": "mg",
    "kg": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "кг": "kg",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "ml": "ml",
    "мл": "ml",
    "l": "l",
    "liter": "l",
    "litre": "l",
    "л": "l",
    "floz": "floz",
    "cup": "cup",
    "cups": "cup",
    "стакан": "glass",
    "стакана": "glass",
    "стаканов": "glass",
    "glass": "glass",
    "glasses": "glass",
    "tbsp": "tbsp",
    "tbs": "tbsp",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "стл": "tbsp",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "чл": "tsp",
    "shot": "shot",
    "shots": "shot",
    "pc": "piece",
    "pcs": "piece",
    "piece": "piece",
    "pieces": "piece",
    "шт": "piece",
    "штук": "piece",
    "штуки": "piece",
    "штука": "piece",
    "x": "piece",
    "slice": "slice",
    "slices": "slice",
    "ломтик": "slice",
    "ломтика": "slice",
    "ломтиков": "slice",
    "кусок": "slice",
    "куска": "slice",
    "scoop": "scoop",
    "scoops": "scoop",
    "серв": "serving",
    "serving": "serving",
    "servings": "serving",
    "portion": "serving",
    "порция": "serving",
    "порции": "serving",
}
"""Spelling variants (en/ru) → canonical unit name. Canonical names not in ``UNIT_TO_GRAMS``
(``piece``, ``slice``, ``scoop``, ``serving``) are resolved through the food name."""

# ------------------------------------------------------------------------------ pieces

PIECE_GRAMS: Final[Mapping[str, float]] = {
    # eggs & dairy
    "egg white": 33.0,
    "egg yolk": 17.0,
    "yolk": 17.0,
    "egg": 50.0,
    "яйц": 50.0,
    "cheese slice": 20.0,
    # fruit
    "banana": 118.0,
    "банан": 118.0,
    "apple": 182.0,
    "яблок": 182.0,
    "orange": 131.0,
    "апельсин": 131.0,
    "mandarin": 74.0,
    "clementine": 74.0,
    "мандарин": 74.0,
    "pear": 178.0,
    "груш": 178.0,
    "peach": 150.0,
    "персик": 150.0,
    "nectarine": 142.0,
    "kiwi": 75.0,
    "киви": 75.0,
    "plum": 66.0,
    "слив": 66.0,
    "apricot": 35.0,
    "абрикос": 35.0,
    "fig": 50.0,
    "инжир": 50.0,
    "date": 24.0,
    "финик": 24.0,
    "avocado": 150.0,
    "авокадо": 150.0,
    "strawberry": 12.0,
    "grape": 5.0,
    "olive": 4.0,
    "олив": 4.0,
    # vegetables
    "tomato": 123.0,
    "помидор": 123.0,
    "cucumber": 200.0,
    "огурец": 200.0,
    "potato": 170.0,
    "картош": 170.0,
    "картофел": 170.0,
    "sweet potato": 130.0,
    "батат": 130.0,
    "carrot": 61.0,
    "морков": 61.0,
    "onion": 110.0,
    "лук": 110.0,
    "bell pepper": 120.0,
    "pepper": 120.0,
    "перец": 120.0,
    "garlic clove": 3.0,
    "clove": 3.0,
    "corn cob": 100.0,
    # bread & bakery
    "bagel": 90.0,
    "бейгл": 90.0,
    "croissant": 60.0,
    "круассан": 60.0,
    "burger bun": 55.0,
    "bun": 50.0,
    "булоч": 50.0,
    "tortilla": 45.0,
    "тортиль": 45.0,
    "pita": 60.0,
    "пита": 60.0,
    "wrap": 70.0,
    "toast": 30.0,
    "тост": 30.0,
    "bread": 30.0,
    "хлеб": 30.0,
    "muffin": 110.0,
    "маффин": 110.0,
    "donut": 60.0,
    "doughnut": 60.0,
    "пончик": 60.0,
    "pancake": 40.0,
    "блин": 40.0,
    "waffle": 40.0,
    "вафл": 40.0,
    "cookie": 15.0,
    "печень": 15.0,
    "cracker": 5.0,
    "крекер": 5.0,
    "rice cake": 9.0,
    # protein
    "chicken breast": 170.0,
    "куриная грудка": 170.0,
    "грудк": 170.0,
    "chicken thigh": 100.0,
    "бедр": 100.0,
    "drumstick": 80.0,
    "голен": 80.0,
    "wing": 30.0,
    "крыл": 30.0,
    "nugget": 18.0,
    "наггетс": 18.0,
    "salmon fillet": 150.0,
    "fillet": 150.0,
    "филе": 150.0,
    "steak": 200.0,
    "стейк": 200.0,
    "patty": 110.0,
    "котлет": 110.0,
    "sausage": 60.0,
    "сосиск": 60.0,
    "колбаск": 60.0,
    "hot dog": 50.0,
    "meatball": 30.0,
    "фрикадел": 30.0,
    "тефтел": 30.0,
    "shrimp": 12.0,
    "prawn": 12.0,
    "кревет": 12.0,
    "dumpling": 20.0,
    "пельмен": 12.0,
    "falafel": 17.0,
    "фалафел": 17.0,
    "samosa": 50.0,
    "sushi": 30.0,
    "суши": 30.0,
    "roll": 30.0,
    "taco": 100.0,
    "тако": 100.0,
    "shawarma": 250.0,
    "шаурм": 250.0,
    "шаверм": 250.0,
    "burger": 220.0,
    "бургер": 220.0,
    "pizza slice": 110.0,
    # nuts & snacks
    "almond": 1.2,
    "миндал": 1.2,
    "walnut": 4.0,
    "cashew": 1.5,
    "кешью": 1.5,
    "pistachio": 0.7,
    "фисташ": 0.7,
    "protein bar": 60.0,
    "bar": 50.0,
    "батончик": 50.0,
    "chocolate square": 5.0,
    # packaged
    "can": 330.0,
    "банк": 330.0,
    "bottle": 500.0,
    "бутыл": 500.0,
    "scoop": 30.0,
    "скуп": 30.0,
    "sachet": 25.0,
    "пакетик": 25.0,
}
"""Default grams for one piece of a food, keyed by a substring of the food name (multi-word keys
listed before their shorter prefixes so "egg white" wins over "egg")."""

_SLICE_GRAMS: Final[Mapping[str, float]] = {
    "pizza": 110.0,
    "пицц": 110.0,
    "cheese": 20.0,
    "сыр": 20.0,
    "ham": 25.0,
    "ветчин": 25.0,
    "turkey": 25.0,
    "индейк": 25.0,
    "salami": 10.0,
    "салями": 10.0,
    "bacon": 12.0,
    "бекон": 12.0,
    "cake": 90.0,
    "торт": 90.0,
    "watermelon": 280.0,
    "арбуз": 280.0,
    "lemon": 8.0,
    "лимон": 8.0,
}
DEFAULT_SLICE_GRAMS: Final[float] = 30.0  # a slice of bread
DEFAULT_SCOOP_GRAMS: Final[float] = 30.0  # protein powder scoop

# ------------------------------------------------------------------------------- parsing

_FRACTIONS: Final[Mapping[str, float]] = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3}
_WORD_NUMBERS: Final[Mapping[str, float]] = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "half": 0.5,
    "quarter": 0.25,
    "один": 1.0,
    "одна": 1.0,
    "одно": 1.0,
    "два": 2.0,
    "две": 2.0,
    "три": 3.0,
    "четыре": 4.0,
    "пять": 5.0,
    "половина": 0.5,
    "половину": 0.5,
    "полстакана": 0.5,
    "пол": 0.5,
    "четверть": 0.25,
}

_QTY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<num>\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½¼¾⅓⅔]|\d+\s*[½¼¾])"
    r"\s*(?:x\s*)?(?P<unit>[a-zA-Zа-яА-ЯёЁ]+(?:\.[a-zA-Zа-яА-ЯёЁ]+)*)?",
    re.UNICODE,
)
_WORD_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<word>[a-zA-Zа-яА-ЯёЁ]+)\s+(?:of\s+|an?\s+)?(?P<unit>[a-zA-Zа-яА-ЯёЁ]+)?",
    re.UNICODE,
)


def _parse_number(raw: str) -> float:
    raw = raw.strip().replace(",", ".")
    if raw in _FRACTIONS:
        return _FRACTIONS[raw]
    if raw and raw[-1] in _FRACTIONS:  # "1½"
        return float(raw[:-1].strip() or 0) + _FRACTIONS[raw[-1]]
    if "/" in raw:
        num, den = (part.strip() for part in raw.split("/", 1))
        return float(num) / float(den) if float(den) else 0.0
    return float(raw)


def singularize(word: str) -> str:
    """Cheap English singular: eggs→egg, slices→slice, tomatoes→tomato, berries→berry."""
    lowered = word.lower()
    if len(lowered) <= 3 or lowered.endswith("ss"):
        return lowered
    if lowered.endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.endswith("oes"):
        return lowered[:-2]
    if lowered.endswith("es") and lowered[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return lowered[:-2]
    if lowered.endswith("s"):
        return lowered[:-1]
    return lowered


def normalize_unit(unit: str | None) -> str:
    """Canonical unit name; unknown words are singularized and returned as-is (a food noun)."""
    if not unit:
        return "piece"
    cleaned = unit.strip().lower().replace(".", "").replace(" ", "")
    if cleaned in UNIT_ALIASES:
        return UNIT_ALIASES[cleaned]
    return singularize(cleaned)


def parse_quantity(text: str) -> tuple[float, str] | None:
    """``"200 g"`` → ``(200, "g")``; ``"2 eggs"`` → ``(2, "egg")``; ``"160g"`` → ``(160, "g")``.

    Returns ``None`` when the text does not start with a number (or a number word). A number
    without a unit is a count of pieces: ``"3"`` → ``(3, "piece")``.
    """
    match = _QTY_RE.match(text)
    if match:
        return _parse_number(match.group("num")), normalize_unit(match.group("unit"))
    word_match = _WORD_RE.match(text)
    if word_match:
        word = word_match.group("word").lower()
        if word in _WORD_NUMBERS:
            return _WORD_NUMBERS[word], normalize_unit(word_match.group("unit"))
        if word.startswith("пол") and len(word) > 3:  # "полбанана", "полстакана"
            return 0.5, normalize_unit(word[3:])
    return None


# ---------------------------------------------------------------------------- to grams


def piece_grams(food: str | None) -> float | None:
    """Default weight of one piece of ``food`` from :data:`PIECE_GRAMS`; None when unknown."""
    if not food:
        return None
    lowered = food.lower()
    best: tuple[int, float] | None = None
    for key, grams in PIECE_GRAMS.items():
        if key in lowered and (best is None or len(key) > best[0]):
            best = (len(key), grams)
    return None if best is None else best[1]


def slice_grams(food: str | None) -> float:
    lowered = (food or "").lower()
    for key, grams in _SLICE_GRAMS.items():
        if key in lowered:
            return grams
    return DEFAULT_SLICE_GRAMS


def to_grams(
    quantity: float,
    unit: str | None,
    *,
    food: str | None = None,
    density: float = 1.0,
    serving_g: float | None = None,
) -> float | None:
    """Grams for ``quantity`` of ``unit``; piece-like units resolve through ``food``.

    ``density`` (g/ml) applies to volume units. ``serving_g`` resolves the ``serving`` unit.
    Returns ``None`` when nothing in the tables applies (the caller then keeps grams unknown).
    """
    canonical = normalize_unit(unit)
    if canonical in UNIT_TO_GRAMS:
        factor = UNIT_TO_GRAMS[canonical]
        if canonical in _VOLUME_UNITS:
            factor *= density
        return quantity * factor
    if canonical == "slice":
        return quantity * slice_grams(food)
    if canonical == "scoop":
        return quantity * DEFAULT_SCOOP_GRAMS
    if canonical == "serving":
        return None if serving_g is None else quantity * serving_g
    if canonical == "piece":
        per_piece = piece_grams(food)
        return None if per_piece is None else quantity * per_piece
    # The unit is a food noun ("2 eggs" → unit "egg"): look it up, then fall back to the food.
    per_piece = piece_grams(canonical) or piece_grams(food)
    return None if per_piece is None else quantity * per_piece


def grams_from_text(text: str, *, food: str | None = None, density: float = 1.0) -> float | None:
    """Convenience: ``parse_quantity`` + ``to_grams`` on a free-text quantity like "2 eggs"."""
    parsed = parse_quantity(text)
    if parsed is None:
        return None
    quantity, unit = parsed
    return to_grams(quantity, unit, food=food or text, density=density)
