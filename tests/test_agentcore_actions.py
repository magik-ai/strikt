"""agent/actions.py: one line per state-changing call, nothing for reads."""

from __future__ import annotations

import json

from strikt.agent.actions import action_line, actions_block, strip_actions


def test_log_meal_line_names_meal_items_and_day() -> None:
    payload = {
        "meal_id": 112,
        "slot": "breakfast",
        "date": "2026-09-29",
        "items": [
            {"id": 276, "name": "Slice Avocado", "kcal": 96, "P": 1, "C": 5, "F": 9, "fiber": 0},
            {
                "id": 278,
                "name": "Buffalo Chicken Wrap",
                "grams": 300,
                "kcal": 207,
                "P": 38,
                "C": 7,
                "F": 3,
                "fiber": 0,
                "flags": ["kcal_mismatch"],
            },
        ],
        "meal_total": {"kcal": 303, "P": 39, "C": 12, "F": 12, "fiber": 0},
        "day": {"totals": {"kcal": 303, "P": 39, "C": 12, "F": 12, "fiber": 0}},
    }
    line = action_line("log_meal", json.dumps(payload), False)
    assert line is not None
    assert line.startswith("log_meal: meal #112 2026-09-29 breakfast:")
    assert "Slice Avocado (item 276)" in line
    assert "Buffalo Chicken Wrap (item 278) 300 g 207 kcal" in line
    assert "[kcal_mismatch]" in line
    assert "day now 303 kcal" in line


def test_update_delete_and_errors() -> None:
    update = {
        "item": {"id": 5, "name": "wrap", "kcal": 386, "P": 55, "C": 10, "F": 14, "fiber": 0},
        "before": {"kcal": 300, "P": 55, "C": 10, "F": 4, "fiber": 0},
        "meal_id": 9,
    }
    line = action_line("update_meal", json.dumps(update), False)
    assert line is not None and "wrap (item 5)" in line and "(was 300 kcal" in line
    deleted = {"deleted_meal_id": 110, "removed": ["chicken"], "removed_total": {"kcal": 551}}
    line = action_line("delete_meal", json.dumps(deleted), False)
    assert line is not None and "meal #110 removed (chicken)" in line
    assert action_line("log_meal", "item 3 not found", True) == "log_meal FAILED: item 3 not found"


def test_reads_leave_no_line_and_no_block() -> None:
    assert action_line("get_day_state", "{}", False) is None
    assert action_line("search_food", "{}", False) is None
    assert actions_block([]) is None
    block = actions_block(["log_meal: meal #1"])
    assert block is not None and block["internal"] is True


def test_strip_actions() -> None:
    assert strip_actions("ok\n<actions>\nx\n</actions>") == "ok"
    assert strip_actions("ok <actions> half written") == "ok"
    assert strip_actions("plain") == "plain"
