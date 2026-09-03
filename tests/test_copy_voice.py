"""The bot's voice: chernyakov.ai ``STYLE.md`` applied to every string the code renders and to
every prompt the model is handed, plus the language question that now opens the chat.

The owner's rules the machine can check: a short dash with spaces and never a long one, lines a
phone can read, no emoji, and a walkthrough that stays a few bullets instead of a numbered wall.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from strikt.telegram.copy import STRINGS, detect_lang, resolve_lang, t

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


def test_rendered_copy_reads_on_a_phone() -> None:
    for lang, table in STRINGS.items():
        for key, text in table.items():
            if key.startswith("bot."):  # the profile texts are read in Telegram's own layout
                continue
            for line in text.split("\n"):
                assert len(line) <= 130, (lang, key, len(line))


def test_no_emoji_in_rendered_copy() -> None:
    for lang, table in STRINGS.items():
        for key, text in table.items():
            assert not any(ord(ch) > 0x2600 for ch in text), (lang, key)


def test_language_question_is_asked_in_both_languages_at_once() -> None:
    """It is the one message sent before the language is known, so it carries both."""
    for lang in ("en", "ru"):
        asked = t(lang, "lang.ask")
        assert "Russian" in asked and "English" in asked
        assert "русски" in asked
        assert len(asked.split("\n")) <= 4


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
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
