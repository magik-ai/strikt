"""Code-rendered strings (card, buttons, errors, /start, /forget_me), one JSON file per language.

The coach's replies are model-written and come out in whatever language the user writes; only what
the *code* renders lives here, in ``telegram/locales/<code>.json``. A locale file carries the
language's native name, the words people use to ask for it, the short weekday and month names, and
the strings themselves. English is the fallback for a key a translation has not caught up with.

Voice (chernyakov.ai ``STYLE.md``): a person talking to a person. Real sentences, never staccato
fragments. A short dash with spaces, never a long one. No emoji, no praise, no marketing words.
Messages stay short: a blank line between blocks reads better in Telegram than one dense block,
and a list is at most four one-line bullets.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

Lang = str

LOCALES_DIR = Path(__file__).with_name("locales")
DEFAULT_LANG: Lang = "en"

#: The order the language picker shows, most widely spoken first. Every code here must have a
#: locale file and every locale file must appear here (``tests/test_copy_voice.py``).
LANGUAGE_ORDER: tuple[str, ...] = (
    "en",
    "zh",
    "hi",
    "es",
    "ar",
    "fr",
    "bn",
    "pt",
    "ru",
    "ur",
    "id",
    "de",
    "ja",
    "tr",
    "ko",
    "vi",
    "it",
    "fa",
    "th",
    "pl",
)

#: Telegram sends the client's language, which is often one we do not carry. A close relative
#: beats English; anything else falls back to English.
_NEIGHBOURS: dict[str, Lang] = {
    "be": "ru",
    "kk": "ru",
    "ky": "ru",
    "uz": "ru",
    "tg": "ru",
    "ms": "id",
    "jv": "id",
    "su": "id",
    "gl": "es",
    "ca": "es",
    "ro": "it",
    "nl": "de",
    "af": "de",
    "ps": "fa",
    "sd": "ur",
    "pa": "hi",
    "mr": "hi",
    "ne": "hi",
    "as": "bn",
    "lo": "th",
    "yue": "zh",
    "wuu": "zh",
}


def _load_locales() -> dict[Lang, dict[str, Any]]:
    locales: dict[Lang, dict[str, Any]] = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        locales[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    if DEFAULT_LANG not in locales:
        raise RuntimeError(f"no {DEFAULT_LANG}.json in {LOCALES_DIR}")
    return locales


LOCALES: dict[Lang, dict[str, Any]] = _load_locales()
LANGUAGES: tuple[Lang, ...] = tuple(code for code in LANGUAGE_ORDER if code in LOCALES)
STRINGS: dict[Lang, dict[str, str]] = {code: loc["strings"] for code, loc in LOCALES.items()}
NATIVE_NAMES: dict[Lang, str] = {code: str(loc["name"]) for code, loc in LOCALES.items()}
WEEKDAYS: dict[Lang, tuple[str, ...]] = {
    code: tuple(loc["weekdays"]) for code, loc in LOCALES.items()
}
MONTHS: dict[Lang, tuple[str, ...]] = {code: tuple(loc["months"]) for code, loc in LOCALES.items()}


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, Lang]:
    """Every word that names a language, mapped to its code. Built from the locale files, so a new
    language brings its own names with it."""
    table: dict[str, Lang] = {}
    for code, loc in LOCALES.items():
        words = [code, str(loc["name"]), *(str(alias) for alias in loc.get("aliases", []))]
        for word in words:
            table.setdefault(word.strip().lower(), code)
    return table


#: Scripts that name one language on sight. Persian and Urdu come before Arabic: all three use the
#: Arabic script and only the extra letters tell them apart.
_SCRIPTS: tuple[tuple[re.Pattern[str], Lang], ...] = (
    (re.compile(r"[Ѐ-ӿ]"), "ru"),
    (re.compile(r"[ऀ-ॿ]"), "hi"),
    (re.compile(r"[ঀ-৿]"), "bn"),
    (re.compile(r"[฀-๿]"), "th"),
    (re.compile(r"[가-힯ᄀ-ᇿ]"), "ko"),
    (re.compile(r"[぀-ヿ]"), "ja"),
    (re.compile(r"[پچژگ]"), "fa"),
    (re.compile(r"[ٹڈڑےں]"), "ur"),
    (re.compile(r"[؀-ۿ]"), "ar"),
    (re.compile(r"[一-鿿]"), "zh"),
)
_LATIN = re.compile(r"[a-z]")
_WORDS = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)?", re.UNICODE)


def resolve_lang(code: str | None) -> Lang:
    """A Telegram ``language_code`` or a stored language to one we carry; English when in doubt."""
    if not code:
        return DEFAULT_LANG
    lowered = code.lower().replace("_", "-")
    if lowered in LOCALES:
        return lowered
    base = lowered.split("-")[0]
    if base in LOCALES:
        return base
    return _NEIGHBOURS.get(base, DEFAULT_LANG)


def detect_lang(text: str | None) -> Lang | None:
    """The language the user answered ``lang.ask`` with, or None when the message says nothing.

    A named language wins over the script it is written in, because people name languages in their
    own alphabet: ``английский`` is Cyrillic and asks for English.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    aliases = _alias_table()
    named = aliases.get(lowered)
    if named is not None:
        return named
    # names of more than one word ("bahasa indonesia", "tiếng việt") never survive a word split
    for alias, code in aliases.items():
        if " " in alias and alias in lowered:
            return code
    for word in _WORDS.findall(lowered):
        named = aliases.get(word)
        if named is not None:
            return named
    for pattern, code in _SCRIPTS:
        if code in LOCALES and pattern.search(lowered):
            return code
    if _LATIN.search(lowered):
        return DEFAULT_LANG
    return None


def t(lang: str | None, key: str, **kwargs: Any) -> str:
    """Translate ``key`` for ``lang`` with ``str.format`` args; falls back to en, then the key."""
    table = STRINGS.get(resolve_lang(lang), STRINGS[DEFAULT_LANG])
    template = table.get(key) or STRINGS[DEFAULT_LANG].get(key) or key
    return template.format(**kwargs) if kwargs else template


def language_name(lang: str | None) -> str:
    """The language's own name, the way its speakers write it."""
    return NATIVE_NAMES.get(resolve_lang(lang), NATIVE_NAMES[DEFAULT_LANG])


def weekday_name(lang: str | None, weekday: int) -> str:
    return WEEKDAYS[resolve_lang(lang)][weekday % 7]


def month_name(lang: str | None, month: int) -> str:
    return MONTHS[resolve_lang(lang)][(month - 1) % 12]
