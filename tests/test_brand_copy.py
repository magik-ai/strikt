"""BRAND.md section 9 and ``telegram/copy.py`` are one surface: this test keeps them equal.

The document is what a human reads before typing into BotFather; ``copy.py`` is what the bot
registers at startup. Two homes for the same sentence is how they drift, so the strings are quoted
in the document and asserted here, character counts included.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from strikt.telegram.commands import (
    COMMAND_NAMES,
    MAX_COMMAND_DESCRIPTION,
    MAX_DESCRIPTION,
    MAX_SHORT_DESCRIPTION,
)
from strikt.telegram.copy import t

BRAND_MD = Path(__file__).resolve().parents[1] / "BRAND.md"


def _doc() -> str:
    return BRAND_MD.read_text(encoding="utf-8")


def _quoted(kind: str, lang: str) -> tuple[str, int]:
    """The blockquote under ``About, en (76 chars…):`` plus the character count it claims."""
    match = re.search(rf"^{kind}, {lang} \((\d+) chars[^)]*\):\n\n> (.+)$", _doc(), re.MULTILINE)
    assert match, f"{kind}, {lang} not found in BRAND.md section 9"
    return match.group(2).strip(), int(match.group(1))


def _commands(lang: str) -> dict[str, str]:
    blocks = re.findall(r"```\n(start - .+?)```", _doc(), re.DOTALL)
    assert len(blocks) == 2, "BRAND.md section 9 must carry an en and a ru command block"
    block = blocks[0 if lang == "en" else 1]
    return dict(line.split(" - ", 1) for line in block.strip().split("\n"))


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_about_matches_code(lang: str) -> None:
    text, claimed = _quoted("About", lang)
    assert t(lang, "bot.short") == text
    assert len(text) == claimed
    assert len(text) <= MAX_SHORT_DESCRIPTION


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_description_matches_code(lang: str) -> None:
    text, claimed = _quoted("Description", lang)
    assert t(lang, "bot.description") == text
    assert len(text) == claimed
    assert len(text) <= MAX_DESCRIPTION


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_commands_match_code(lang: str) -> None:
    documented = _commands(lang)
    assert tuple(documented) == COMMAND_NAMES
    for name, description in documented.items():
        assert t(lang, f"cmd.{name}") == description
        assert len(description) <= MAX_COMMAND_DESCRIPTION
