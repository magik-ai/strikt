"""Inline keyboards only where they remove typing (PLAN §7) and their callback data.

Callback data formats (≤ 64 bytes): ``s:<meal_id>:<slot>``, ``undo:<meal_id>``, ``recalc``,
``close``, ``forget:yes`` / ``forget:no``, ``yn:<action>:yes|no``, ``lang:ru`` / ``lang:en``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from strikt.core.types import Button
from strikt.telegram.copy import LANGUAGES, NATIVE_NAMES, t

CallbackKind = Literal["slot", "undo", "recalc", "close", "forget", "yesno", "lang"]
#: How many language buttons fit a phone row without the text truncating.
LANGUAGES_PER_ROW = 3
SLOTS: tuple[str, ...] = ("breakfast", "lunch", "dinner", "snack")


@dataclass(frozen=True)
class Callback:
    kind: CallbackKind
    meal_id: int | None = None
    slot: str | None = None
    answer: bool | None = None
    action: str | None = None
    language: str | None = None


def parse_callback(data: str | None) -> Callback | None:
    """Parse callback data; returns None for anything malformed (never raises)."""
    if not data:
        return None
    parts = data.split(":")
    try:
        match parts:
            case ["s", meal_id, slot] if slot in SLOTS:
                return Callback("slot", meal_id=int(meal_id), slot=slot)
            case ["undo", meal_id]:
                return Callback("undo", meal_id=int(meal_id))
            case ["recalc"]:
                return Callback("recalc")
            case ["close"]:
                return Callback("close")
            case ["forget", answer] if answer in {"yes", "no"}:
                return Callback("forget", answer=answer == "yes")
            case ["yn", action, answer] if answer in {"yes", "no"} and action:
                return Callback("yesno", action=action, answer=answer == "yes")
            case ["lang", language] if language in LANGUAGES:
                return Callback("lang", language=language)
    except ValueError:
        return None
    return None


def slot_data(meal_id: int, slot: str) -> str:
    return f"s:{meal_id}:{slot}"


def undo_data(meal_id: int) -> str:
    return f"undo:{meal_id}"


def slot_picker(meal_id: int, lang: str | None) -> list[list[Button]]:
    return [
        [
            Button(text=t(lang, "btn.breakfast"), callback_data=slot_data(meal_id, "breakfast")),
            Button(text=t(lang, "btn.lunch"), callback_data=slot_data(meal_id, "lunch")),
        ],
        [
            Button(text=t(lang, "btn.dinner"), callback_data=slot_data(meal_id, "dinner")),
            Button(text=t(lang, "btn.snack"), callback_data=slot_data(meal_id, "snack")),
        ],
    ]


def meal_actions(meal_id: int, lang: str | None, *, ask_slot: bool) -> list[list[Button]]:
    """After a food log: slot picker when the slot is unknown, plus Undo / Recalculate."""
    rows = slot_picker(meal_id, lang) if ask_slot else []
    rows.append(
        [
            Button(text=t(lang, "btn.undo"), callback_data=undo_data(meal_id)),
            Button(text=t(lang, "btn.recalc"), callback_data="recalc"),
        ]
    )
    return rows


def day_actions(lang: str | None) -> list[list[Button]]:
    return [
        [
            Button(text=t(lang, "btn.recalc"), callback_data="recalc"),
            Button(text=t(lang, "btn.close"), callback_data="close"),
        ]
    ]


def forget_confirm(lang: str | None) -> list[list[Button]]:
    return [
        [Button(text=t(lang, "btn.forget_confirm"), callback_data="forget:yes")],
        [Button(text=t(lang, "btn.cancel"), callback_data="forget:no")],
    ]


def language_picker(guess: str | None = None) -> list[list[Button]]:
    """The one question asked before anything else, in every language the bot carries.

    Each button says the language in its own words. Typing the answer works just as well
    (``copy.detect_lang``); the buttons are there so it takes one tap. The language Telegram
    reported comes first, because it is usually the right one.
    """
    codes = list(LANGUAGES)
    if guess in codes:
        codes.remove(guess)
        codes.insert(0, guess)
    buttons = [Button(text=NATIVE_NAMES[code], callback_data=f"lang:{code}") for code in codes]
    return [
        buttons[start : start + LANGUAGES_PER_ROW]
        for start in range(0, len(buttons), LANGUAGES_PER_ROW)
    ]


def yes_no(action: str, lang: str | None) -> list[list[Button]]:
    if ":" in action or not action:
        raise ValueError("action must be a non-empty token without ':'")
    return [
        [
            Button(text=t(lang, "btn.yes"), callback_data=f"yn:{action}:yes"),
            Button(text=t(lang, "btn.no"), callback_data=f"yn:{action}:no"),
        ]
    ]
