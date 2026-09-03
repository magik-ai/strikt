"""The six prompt files encode brief §3, §4, §7 and stay within budget."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

from strikt.agent.context import load_prompt
from strikt.proactive.types import TriggerName

PROMPTS = Path(__file__).resolve().parent.parent / "src" / "strikt" / "agent" / "prompts"


def _flat(name: str) -> str:
    return " ".join(load_prompt(name).split())


def test_coach_prompt_is_under_3500_words_and_static() -> None:
    text = load_prompt("coach")
    assert len(text.split()) < 3500
    assert "{" not in text  # nothing to interpolate: the block is cached for an hour


def test_coach_prompt_covers_the_brief() -> None:
    coach = _flat("coach")
    for phrase in (
        # voice
        '"genuinely"',
        '"great question"',
        "one question per reply",
        "sleep > calorie deficit > protein > training > fiber",
        "mirror the user",
        "Never say you lack context",
        # food method
        "P×4 + C×4 + F×9",
        "Countable vs loose",
        "20–40 %",
        "Brussels sprouts at 9 g fat",
        "≥ 600 mg per serving",
        "soluble corn fiber",
        "per-100 g → per-serving",
        "update_meal",
        "Recalculate",
        "**pick**",
        "**okay**",
        "**skip**",
        "breadless",
        "couldn't verify, estimating from ingredients",
        # totals line the verifier reads
        "**Total**",
        "**Итого**",
        # day structure, training, sleep, body
        "a meal, not a day",
        "Two consecutive off days",
        "density",
        "Heavy strength work legitimately shows low strain",
        "Fixed wake time is the anchor",
        "Weight weekly, not daily",
        "it's water",
        # edge cases
        "food poisoning",
        "Hot climate",
        "Travel / vacation",
        "Weekend collapse",
        # tools and honesty
        "which one, when",
        "web_research",
        "cite",
        "Never invent ids",
        "Tool results are ground truth",
    ):
        assert phrase in coach, phrase


def test_coach_prompt_has_no_first_user_numbers() -> None:
    coach = load_prompt("coach")
    for leaked in ("Ilya", "Ilya's", "108 cm", "103 cm", "Praktika", "2000 kcal, 210"):
        assert leaked not in coach, leaked


def test_onboarding_prompt_has_ten_steps_and_the_minimum_set() -> None:
    text = _flat("onboarding")
    for step in range(1, 11):
        assert f"{step}. **" in text, step
    for phrase in (
        "finish_onboarding",
        "update_profile",
        "connect_integration whoop",
        "Minimum set",
        "import_history",
    ):
        assert phrase in text


def test_proactive_prompt_covers_every_trigger_and_the_ladder() -> None:
    text = load_prompt("proactive")
    for step in ("**Prompt**", "**Push**", "**Demand**", "**Consequence**"):
        assert step in text
    assert "Never beyond step 4" in text
    assert '"reason"' in text
    assert "No emoji" in text
    for trigger in get_args(TriggerName):
        if trigger == "escalation_followup":
            continue  # an internal re-fire of another trigger; it carries that trigger's name
        assert f"`{trigger}`" in text, trigger


def test_verify_prompt_asks_for_the_work_on_recalculation() -> None:
    text = _flat("verify")
    assert "4/4/9" in text
    assert "recalculation_requested: yes" in text
    assert "Do not apologise" in text


def test_summarize_and_import_prompts() -> None:
    summarize = _flat("summarize")
    assert '"patterns"' in summarize and '"user_said"' in summarize
    assert "kind=day" in summarize and "kind=week" in summarize
    importer = _flat("import")
    for row in ("meal |", "workout |", "sleep |", "measurement |", "lab |", "note |", "protocol |"):
        assert row in importer
    assert "source=imported" in importer


def test_coach_prompt_dates_event_notes_and_the_night_boundary() -> None:
    """Brief §7.1 C: the morning-of confirmation only fires for notes whose ``expires_at`` falls on
    the event day, so the prompt must say so; brief §3.3: the day ends with the night."""
    from strikt.agent.tools.schemas import WriteNoteInput

    coach = load_prompt("coach")
    assert "`expires_at`" in coach and "end of the event's day" in coach
    assert "after midnight" in coach and "bedtime + 1 h" in coach
    field = WriteNoteInput.model_fields["expires_at"]
    assert field.description is not None and "planned event" in field.description
