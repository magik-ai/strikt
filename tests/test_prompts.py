"""PROMPTS.md is generated from the prompt files and the prompts encode the brief's rules."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "src" / "strikt" / "agent" / "prompts"


def _load_builder() -> object:
    spec = importlib.util.spec_from_file_location(
        "build_prompts_md", ROOT / "scripts" / "build_prompts_md.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_prompts_md"] = module
    spec.loader.exec_module(module)
    return module


def test_prompts_md_is_in_sync() -> None:
    module = _load_builder()
    expected = module.build()  # type: ignore[attr-defined]
    assert (ROOT / "PROMPTS.md").read_text(encoding="utf-8") == expected


def test_all_six_prompts_exist_and_are_substantial() -> None:
    for name in ("coach", "onboarding", "proactive", "verify", "summarize", "import"):
        text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
        assert len(text) > 400, name


def test_coach_prompt_encodes_the_brief() -> None:
    coach = " ".join((PROMPTS / "coach.md").read_text(encoding="utf-8").split())
    for phrase in (
        "sleep > calorie deficit > protein > training > fiber",
        "P×4 + C×4 + F×9",
        "Countable vs loose",
        "pick",
        "skip",
        "couldn't verify, estimating from ingredients",
        "Recalculate",
        "a meal, not a day",
        "mirror the user",
        "Never say you lack context",
    ):
        assert phrase in coach, phrase
    assert "Strikt" in coach


def test_coach_prompt_has_no_user_specific_numbers() -> None:
    coach = (PROMPTS / "coach.md").read_text(encoding="utf-8")
    for leaked in ("Ilya", "108 cm", "Praktika"):
        assert leaked not in coach


def test_proactive_prompt_has_the_ladder() -> None:
    text = (PROMPTS / "proactive.md").read_text(encoding="utf-8")
    for step in ("Prompt", "Push", "Demand", "Consequence"):
        assert step in text
    assert "Never beyond step 4" in text
