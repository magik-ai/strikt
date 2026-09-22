"""The eval harness itself: seeding, the end-state diff and the checks (no network, no key)."""

from __future__ import annotations

import json

from evals.harness import CASES_PATH, Case, grade, load_cases, run_case
from strikt.agent.client import FakeLLM
from strikt.config import Settings

LOG_MEAL = {
    "items": [
        {"name": "psyllium husk", "kcal": 8, "protein_g": 0, "carbs_g": 9, "fat_g": 0, "fiber_g": 9}
    ],
    "slot": "snack",
}


def _case(**overrides: object) -> Case:
    base = {
        "id": "smoke",
        "tags": ["log"],
        "message": "выпил 2 чайных псиллиума",
        "now": "2026-09-22T11:10:00+04:00",
        "expect": {"must_call": ["log_meal"], "meals_added": 1, "fiber_added_at_least": 5},
        "seed": {},
    }
    base.update(overrides)
    return Case(**base)  # type: ignore[arg-type]


async def test_every_shipped_case_parses_and_is_reviewable() -> None:
    cases = load_cases()
    assert len(cases) >= 15  # below this a single flaky case swings the score
    for case in cases:
        assert case.message.strip(), case.id
        assert case.expect, case.id
        assert case.tags, case.id
        assert case.when is not None  # a bad timestamp fails here, not mid-run
    assert "Asia/Dubai" in CASES_PATH.read_text(encoding="utf-8")


async def test_a_logged_turn_passes_its_checks(fake_llm: FakeLLM, settings: Settings) -> None:
    fake_llm.queue(
        FakeLLM.tool_use("log_meal", LOG_MEAL),
        FakeLLM.text("записал, 8 ккал и 9 клетчатки. до нормы ещё 16."),
    )
    outcome = await run_case(_case(), fake_llm, settings)
    result = grade(outcome)
    assert result.passed, result.failures
    assert outcome.after.meals - outcome.before.meals == 1
    assert any(turn["role"] == "tool_call" for turn in outcome.trace)


async def test_a_turn_that_refuses_to_log_fails(fake_llm: FakeLLM, settings: Settings) -> None:
    fake_llm.queue(FakeLLM.text("Псиллиум записывать не буду, это не еда."))
    outcome = await run_case(_case(), fake_llm, settings)
    result = grade(outcome)
    assert not result.passed
    assert any("log_meal not called" in f for f in result.failures)


async def test_a_turn_that_rewrites_an_older_day_fails(
    fake_llm: FakeLLM, settings: Settings
) -> None:
    """The watermelon guard: every case checks it, no case has to ask for it."""
    seed = {
        "meals": [
            {
                "days_ago": 11,
                "at": "13:00",
                "items": [{"name": "арбуз", "kcal": 90, "protein_g": 2, "carbs_g": 22, "fat_g": 0}],
            }
        ]
    }
    fake_llm.queue(
        FakeLLM.tool_use(
            "update_meal",
            {"item_id": 1, "changes": {"fiber_g": 4}, "expect_name": "арбуз"},
        ),
        FakeLLM.text("поправил"),
    )
    case = _case(seed=seed, expect={"meals_added": 0})
    outcome = await run_case(case, fake_llm, settings)
    result = grade(outcome)
    assert not result.passed
    assert any("older_days_untouched" in f for f in result.failures)


async def test_cases_file_is_valid_json_with_notes() -> None:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert raw["flow"] == "turn"
    real = [c for c in raw["cases"] if c.get("note")]
    assert len(real) >= 5  # the cases that came from the owner's own chat
