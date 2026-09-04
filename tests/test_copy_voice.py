"""The bot's voice: chernyakov.ai ``STYLE.md`` applied to every string the code renders and to
every prompt the model is handed, plus the language question that now opens the chat.

The owner's rules the machine can check: a short dash with spaces and never a long one, lines a
phone can read, no emoji, and a walkthrough that stays a few bullets instead of a numbered wall.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from strikt.telegram.copy import (
    LANGUAGES,
    NATIVE_NAMES,
    STRINGS,
    detect_lang,
    resolve_lang,
    t,
)
from strikt.telegram.keyboards import language_picker

LONG_DASH = "—"
PROMPTS = Path(__file__).resolve().parents[1] / "src" / "strikt" / "agent" / "prompts"


def test_rendered_copy_has_no_long_dash() -> None:
    for lang, table in STRINGS.items():
        for key, text in table.items():
            assert LONG_DASH not in text, (lang, key)


def test_prompts_have_no_long_dash() -> None:
    """The prompt is the model's style sheet as much as its instructions: a long dash in it is a
    long dash in the replies."""
    files = sorted(PROMPTS.glob("*.md"))
    assert files, "no prompts found"
    for path in files:
        assert LONG_DASH not in path.read_text(encoding="utf-8"), path.name


def test_the_onboarding_checklist_has_no_long_dash() -> None:
    """It goes into the system prompt, where a long dash teaches the model to write one."""
    from strikt.onboarding import checklist

    source = Path(checklist.__file__).read_text(encoding="utf-8")
    assert LONG_DASH not in source
    for lang in ("en", "ru"):
        assert LONG_DASH not in checklist.render_state(None, lang)


def test_rendered_copy_reads_on_a_phone() -> None:
    for lang, table in STRINGS.items():
        for key, text in table.items():
            if key.startswith("bot."):  # the profile texts are read in Telegram's own layout
                continue
            for line in text.split("\n"):
                assert len(line) <= 130, (lang, key, len(line))


#: Emoji blocks only. Chinese, Japanese and Korean letters live far above 0x2600 and are words.
EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u26ff\u2700-\u27bf\ufe0f\u2b00-\u2bff]")


def test_no_emoji_in_rendered_copy() -> None:
    for lang, table in STRINGS.items():
        for key, text in table.items():
            assert not EMOJI.search(text), (lang, key)


def test_every_locale_carries_the_same_keys_as_english() -> None:
    """``t`` falls back to English for a missing key, which would put an English sentence in the
    middle of a Thai chat. The locale files stay in step instead."""
    english = set(STRINGS["en"])
    for code, table in STRINGS.items():
        assert set(table) == english, (code, sorted(english ^ set(table)))


def test_every_locale_keeps_the_placeholders_english_uses() -> None:
    placeholders = {key: set(re.findall(r"{(\w+)}", text)) for key, text in STRINGS["en"].items()}
    for code, table in STRINGS.items():
        for key, text in table.items():
            assert set(re.findall(r"{(\w+)}", text)) == placeholders[key], (code, key)


def test_language_picker_offers_every_locale_by_its_own_name() -> None:
    assert set(LANGUAGES) == set(STRINGS), "LANGUAGE_ORDER and the locale files disagree"
    assert LANGUAGES[0] == "en"
    buttons = [b for row in language_picker("ru") for b in row]
    assert buttons[0].callback_data == "lang:ru", "the guess comes first"
    assert {b.callback_data for b in buttons} == {f"lang:{code}" for code in LANGUAGES}
    assert {b.text for b in buttons} == {NATIVE_NAMES[code] for code in LANGUAGES}
    assert all(len(row) <= 3 for row in language_picker(None))


def test_the_language_question_is_short_in_every_locale() -> None:
    """It is the first message a user ever gets, in a language picked from Telegram's guess."""
    for code in STRINGS:
        asked = t(code, "lang.ask")
        assert asked.strip()
        assert len(asked.split("\n")) <= 4, code
        assert len(asked) <= 160, (code, len(asked))


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # a two-letter code is an ordinary word elsewhere: these all used to pick the wrong
        # language out of a sentence that names one plainly
        ("let's do it in English", "en"),
        ("quiero hablar en español", "es"),
        ("Hi", "en"),
        ("ja, bitte", "en"),
        ("per favore", "en"),
        ("русский", "ru"),
        ("рус", "ru"),
        ("по-русски", "ru"),
        ("Russian", "ru"),
        ("ru", "ru"),
        ("english", "en"),
        ("EN", "en"),
        # a named language beats the alphabet it is written in
        ("английский", "en"),
        ("давай на английском", "en"),
        # nothing named: the alphabet decides
        ("привет", "ru"),
        ("hey there", "en"),
        # nothing to read at all
        ("", None),
        ("12345", None),
        ("!!!", None),
        (None, None),
    ],
)
def test_detect_lang(answer: str | None, expected: str | None) -> None:
    assert detect_lang(answer) == expected


def test_detect_lang_and_resolve_lang_agree() -> None:
    for code in ("ru", "en"):
        detected = detect_lang(code)
        assert detected is not None and resolve_lang(detected) == code


def test_profile_texts_fit_telegram_limits_in_every_locale() -> None:
    """``setMyShortDescription`` caps at 120 characters and ``setMyDescription`` at 512, per
    language. Telegram rejects the call rather than truncating."""
    for code, table in STRINGS.items():
        assert len(table["bot.short"]) <= 120, (code, len(table["bot.short"]))
        assert len(table["bot.description"]) <= 512, (code, len(table["bot.description"]))
        for name in ("cmd.start", "cmd.today", "cmd.forget_me"):
            assert 1 <= len(table[name]) <= 256, (code, name)


def test_every_locale_names_itself_and_answers_to_its_own_name() -> None:
    for code in STRINGS:
        native = NATIVE_NAMES[code]
        assert native.strip()
        assert detect_lang(native) == code, (code, native)
