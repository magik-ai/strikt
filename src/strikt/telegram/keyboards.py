"""The three inline keyboards that survive, and their callback data.

A button is here only when it removes typing that conversation cannot: the language question
(before any language is known), the /forget_me confirmation (destructive, needs an explicit yes)
and undo on a meal just logged. Everything else - the slot of a meal, recalculating, closing the
day, yes or no to the coach - is said in words, because this is a chat and not a control panel.

Callback data formats (≤ 64 bytes): ``undo:<meal_id>``, ``forget:yes`` / ``forget:no``,
``lang:<code>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from strikt.core.types import Button
from strikt.telegram.copy import LANGUAGES, NATIVE_NAMES, t

CallbackKind = Literal["undo", "forget", "lang"]
#: How many language buttons fit a phone row without the text truncating.
LANGUAGES_PER_ROW = 3


@dataclass(frozen=True)
class Callback:
    kind: CallbackKind
    meal_id: int | None = None
    answer: bool | None = None
    language: str | None = None


def parse_callback(data: str | None) -> Callback | None:
    """Parse callback data; returns None for anything malformed (never raises)."""
    if not data:
        return None
    parts = data.split(":")
    try:
        match parts:
            case ["undo", meal_id]:
                return Callback("undo", meal_id=int(meal_id))
            case ["forget", answer] if answer in {"yes", "no"}:
                return Callback("forget", answer=answer == "yes")
            case ["lang", language] if language in LANGUAGES:
                return Callback("lang", language=language)
    except ValueError:
        return None
    return None


def undo_data(meal_id: int) -> str:
    return f"undo:{meal_id}"


def undo_action(meal_id: int, lang: str | None) -> list[list[Button]]:
    """One button under a meal the coach just logged. Saying "remove that" works too, but a
    mis-logged meal is the one thing worth a single tap."""
    return [[Button(text=t(lang, "btn.undo"), callback_data=undo_data(meal_id))]]


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
