"""Extract the day totals a reply *claims* (kcal, protein, carbs, fat, fiber) in ru and en.

The verify step (``agent/verify.py``) compares these with the database. Only lines that present
themselves as a day total are read ("Total", "Итого", "за день", "so far"…); per-item lines and
"remaining / осталось" lines are ignored on purpose — a false mismatch would trigger a paid
rewrite of a correct reply, while a missed claim only skips the check.

Number formats accepted: ``1240``, ``1 240`` (space or thin space), ``1,240``, ``1240.5``.
Macro labels accepted: ``P/Б/protein/белок``, ``C/У/carbs/углеводы``, ``F/Ж/fat/жиры``,
``fiber/fibre/клетчатка``, in either order with the number (``P 118``, ``118 P``, ``118 g protein``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

_THOUSANDS = re.compile(r"(?<=\d)[     ,](?=\d{3}\b)")
_NUMBER = r"(\d{1,3}(?:[     ,]\d{3})+|\d+)(?:[.,](\d+))?"

# Deliberately no bare "day" / "today" / "сегодня": an advice line ("Today you still need 60 g
# protein") or a per-meal line ("Dinner today: 640 kcal") is not a day total. The coach prompt
# mandates a line starting with Total/Итого; the other markers are the explicit phrasings.
TOTAL_MARKERS = re.compile(
    r"(?i)(\btotal\b|\bso far\b|\bday total\b|\bsum\b|\bitog\b"
    r"|итог|всего|за день|за сегодня|сумма|в сумме|набрано|получается)"
)
REMAINING_MARKERS = re.compile(
    r"(?i)(\bremain\w*\b|\bleft\b|\bbudget\b|\bto go\b|\bunder\b|\bover by\b|\bover\b"
    r"|остал|остаток|бюджет|до цели|до нормы|перебор|недобор|запас)"
)
TARGET_MARKERS = re.compile(r"(?i)(\btarget\b|\bgoal\b|цель|норма|таргет|план\b)")

_KCAL = re.compile(
    rf"(?i)(?:kcal|ккал|кк|калори\w*|calories)\s*[:=]?\s*{_NUMBER}|{_NUMBER}\s*(?:kcal|ккал|кк\b|калори\w*|calories)"
)

_MACRO_WORDS: dict[str, str] = {
    "protein_g": r"(?:protein|prot|белок|белка|белки|белков|бел\.?|P|Б)",
    "carbs_g": r"(?:carbs?|carbohydrates?|углеводы|углеводов|углев\.?|угл\.?|C|У)",
    "fat_g": r"(?:fats?|жиры|жиров|жира|жир\.?|F|Ж)",
    "fiber_g": r"(?:fiber|fibre|клетчатка|клетчатки|клетч\.?)",
}


def _macro_patterns(word: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    unit = r"(?:\s*(?:g|г|гр|грамм\w*))?"
    label_first = re.compile(rf"(?<![\w])(?:{word})\s*[:=]?\s*{_NUMBER}{unit}(?![\w])")
    number_first = re.compile(rf"(?<![\w.]){_NUMBER}{unit}\s*(?:{word})(?![\w])")
    return label_first, number_first


_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    key: _macro_patterns(word) for key, word in _MACRO_WORDS.items()
}


@dataclass(frozen=True)
class ClaimedTotals:
    """Numbers a reply states as the day total; ``None`` when not stated."""

    kcal: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None

    @property
    def any(self) -> bool:
        return any(getattr(self, f.name) is not None for f in fields(self))

    def items(self) -> list[tuple[str, float]]:
        return [
            (f.name, getattr(self, f.name))
            for f in fields(self)
            if getattr(self, f.name) is not None
        ]


def _to_float(whole: str, frac: str | None) -> float:
    digits = _THOUSANDS.sub("", whole)
    return float(f"{digits}.{frac}" if frac else digits)


def _first_number(match: re.Match[str]) -> float | None:
    groups = match.groups()
    for i in range(0, len(groups), 2):
        if groups[i] is not None:
            return _to_float(groups[i], groups[i + 1] if i + 1 < len(groups) else None)
    return None


def _kcal_in(line: str) -> float | None:
    match = _KCAL.search(line)
    return _first_number(match) if match else None


def _macro_in(line: str, key: str) -> float | None:
    label_first, number_first = _PATTERNS[key]
    # Case-sensitive single letters (P/C/F, Б/У/Ж) — a lowercase "c" inside a word is not carbs.
    for pattern in (label_first, number_first):
        match = pattern.search(line)
        if match:
            value = _first_number(match)
            if value is not None:
                return value
    return None


def is_total_line(line: str) -> bool:
    """A line that talks about the day total and not about what remains or the target."""
    if REMAINING_MARKERS.search(line) or TARGET_MARKERS.search(line):
        return False
    return bool(TOTAL_MARKERS.search(line))


def extract_numbers(text: str) -> ClaimedTotals:
    """Claimed day totals from the reply; the *last* total line wins for each field."""
    kcal = protein = carbs = fat = fiber = None
    for raw in text.splitlines():
        line = raw.replace("*", "").replace("_", " ").strip()
        if not line or not is_total_line(line):
            continue
        # Many replies put "Total:" on its own line and the numbers on the next; look ahead
        # only through the same line — the model is told to keep the total on one line.
        found_kcal = _kcal_in(line)
        found = {key: _macro_in(line, key) for key in _MACRO_WORDS}
        if found_kcal is None and all(v is None for v in found.values()):
            continue
        if found_kcal is not None:
            kcal = found_kcal
        if found["protein_g"] is not None:
            protein = found["protein_g"]
        if found["carbs_g"] is not None:
            carbs = found["carbs_g"]
        if found["fat_g"] is not None:
            fat = found["fat_g"]
        if found["fiber_g"] is not None:
            fiber = found["fiber_g"]
    return ClaimedTotals(kcal=kcal, protein_g=protein, carbs_g=carbs, fat_g=fat, fiber_g=fiber)
