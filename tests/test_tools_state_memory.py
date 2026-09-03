"""Day-state and memory tools: get_day_state, flags, plan, close_day (FakeLLM summary),
render_day_card, get_history, search_history, notes, reminders."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import FakeLLM
from strikt.agent.tools import ToolContext, memory, schemas, state
from strikt.db import repo
from strikt.db.models import MealSlot, NoteKind, SummaryKind
from tests.test_memory_helpers import TODAY, item, seed_meal, seed_turn, seed_workout

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def parsed(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] = json.loads(str(result.content))
    return data


def summary_payload(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "data": {
            "totals": {"kcal": 1, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "fiber_g": 1},
            "adherence": {"kcal": 1, "protein": 1, "fiber": 0, "bedtime": 0, "meals_logged": 1},
            "patterns": ["one meal until evening"],
            "flagged": [],
            "user_said": [],
        },
    }


async def seed_today(session: AsyncSession, user_id: int) -> None:
    await seed_meal(
        session,
        user_id,
        TODAY,
        "13:00",
        [item("chicken plate", 600, 55, 20, 30, fiber=6)],
        slot=MealSlot.lunch,
    )
    await seed_workout(session, user_id, TODAY, "07:00", sport="strength", strain=9.1)


# ----------------------------------------------------------------------------- get_day_state


async def test_get_day_state_returns_text_and_numbers(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_today(session, tool_ctx.user_id)
    result = parsed(await state.get_day_state(tool_ctx, schemas.GetDayStateInput()))
    assert "day 2026-09-03" in result["text"] and "chicken plate" in result["text"]
    numbers = result["numbers"]
    assert numbers["totals"]["kcal"] == 600 and numbers["remaining"]["kcal"] == 1400
    assert numbers["items"][0]["slot"] == "lunch"
    assert numbers["closed"] is False
    other = parsed(
        await state.get_day_state(tool_ctx, schemas.GetDayStateInput(date=date(2026, 8, 1)))
    )
    assert other["numbers"]["totals"]["kcal"] == 0 and "none logged" in other["text"]


# ------------------------------------------------------------------------- flags and plan


async def test_set_day_flag_and_plan(tool_ctx: ToolContext, session: AsyncSession) -> None:
    result = parsed(
        await state.set_day_flag(tool_ctx, schemas.SetDayFlagInput(date=TODAY, flag="salty"))
    )
    assert result["flags"] == ["salty"] and "water" in result["note"]
    result = parsed(
        await state.set_day_flag(
            tool_ctx, schemas.SetDayFlagInput(date=TODAY, flag="salty", on=False)
        )
    )
    assert result["flags"] == []
    plan = parsed(
        await state.set_day_plan(
            tool_ctx,
            schemas.SetDayPlanInput(
                date=TODAY, plan=schemas.DayPlan(lunch="Kinoya ramen 13:00", bedtime="00:30")
            ),
        )
    )
    assert plan["plan"] == {"lunch": "Kinoya ramen 13:00", "bedtime": "00:30"}
    row = await repo.get_day(session, tool_ctx.user_id, TODAY)
    assert row is not None and row.plan == plan["plan"]
    empty = await state.set_day_plan(
        tool_ctx, schemas.SetDayPlanInput(date=TODAY, plan=schemas.DayPlan())
    )
    assert empty.is_error


# --------------------------------------------------------------------------------- close_day


async def test_close_day_writes_summary_via_fake_llm(
    tool_ctx: ToolContext, session: AsyncSession, fake_llm: FakeLLM
) -> None:
    await seed_today(session, tool_ctx.user_id)
    fake_llm.queue(
        FakeLLM.json_result(summary_payload("Day: 600 kcal, one meal.")),
        FakeLLM.json_result(summary_payload("Week: thin data.")),
    )
    result = parsed(
        await state.close_day(
            tool_ctx, schemas.CloseDayInput(date=TODAY, verdict="600 kcal, one meal. Eat lunch.")
        )
    )
    assert result["summary"] == "written"
    assert result["close_line"].startswith("Closed at 600 kcal / 55 P / 20 C / 30 F / 6 fiber")
    assert result["bed_line"] == "Bed by 00:30"
    assert result["verdict"] == "600 kcal, one meal. Eat lunch."
    day_summary = await repo.get_summary(session, tool_ctx.user_id, SummaryKind.day, TODAY)
    assert day_summary is not None and day_summary.text == "Day: 600 kcal, one meal."
    week = await repo.get_summary(session, tool_ctx.user_id, SummaryKind.week, date(2026, 8, 31))
    assert week is not None
    row = await repo.get_day(session, tool_ctx.user_id, TODAY)
    assert row is not None and row.closed_at is not None
    assert [c["purpose"] for c in fake_llm.calls] == ["summary", "summary"]


async def test_close_day_survives_llm_failure(tool_ctx: ToolContext, fake_llm: FakeLLM) -> None:
    # FakeLLM with no scripted responses raises: the close must still land
    result = parsed(await state.close_day(tool_ctx, schemas.CloseDayInput(date=TODAY, verdict="x")))
    assert result["summary"] == "deferred to the nightly job"
    future = await state.close_day(
        tool_ctx, schemas.CloseDayInput(date=TODAY + timedelta(days=1), verdict="x")
    )
    assert future.is_error


async def test_render_day_card_matches_renderer(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_today(session, tool_ctx.user_id)
    result = await state.render_day_card(tool_ctx, schemas.RenderDayCardInput())
    assert not result.is_error
    text = str(result.content)
    assert text.startswith("<b>Сегодня") and "chicken plate" in text and "600" in text


# ------------------------------------------------------------------------- history / search


async def test_get_history_defaults_to_last_week(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_meal(
        session,
        tool_ctx.user_id,
        TODAY - timedelta(days=2),
        "13:20",
        [item("tonkotsu ramen", 780, 38, 85, 30)],
        slot=MealSlot.lunch,
    )
    await seed_meal(
        session, tool_ctx.user_id, TODAY - timedelta(days=20), "13:20", [item("old meal", 500)]
    )
    result = parsed(await memory.get_history(tool_ctx, schemas.GetHistoryInput(kinds=["meals"])))
    assert result["count"] == 1 and "ramen" in result["rows"]
    assert result["from"] == "2026-08-28" and result["to"] == "2026-09-03"
    everything = parsed(
        await memory.get_history(
            tool_ctx,
            schemas.GetHistoryInput(kinds=["meals"], date_from=date(2026, 8, 1), date_to=TODAY),
        )
    )
    assert everything["count"] == 2
    empty = await memory.get_history(tool_ctx, schemas.GetHistoryInput(kinds=[]))
    assert empty.is_error


async def test_search_history_resolves_periods_and_keywords(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    await seed_meal(
        session,
        tool_ctx.user_id,
        date(2026, 9, 1),  # Tuesday
        "13:20",
        [item("tonkotsu ramen", 780, 38, 85, 30)],
        slot=MealSlot.lunch,
    )
    await seed_turn(
        session, tool_ctx.user_id, date(2026, 8, 30), "10:00", "we decided fat stays at 105"
    )
    by_day = parsed(
        await memory.search_history(
            tool_ctx, schemas.SearchHistoryInput(text="what did I eat last Tuesday")
        )
    )
    assert by_day["count"] >= 1 and "ramen" in by_day["rows"]
    by_word = parsed(
        await memory.search_history(tool_ctx, schemas.SearchHistoryInput(text="decided fat"))
    )
    assert "fat stays" in by_word["rows"]
    blank = await memory.search_history(tool_ctx, schemas.SearchHistoryInput(text="   "))
    assert blank.is_error


# ------------------------------------------------------------------------------------- notes


async def test_write_and_retire_note(tool_ctx: ToolContext, session: AsyncSession) -> None:
    created = parsed(
        await memory.write_note(
            tool_ctx, schemas.WriteNoteInput(kind="preference", text="dislikes chia pudding")
        )
    )
    assert created["status"] == "created"
    again = parsed(
        await memory.write_note(
            tool_ctx, schemas.WriteNoteInput(kind="preference", text="Dislikes chia pudding.")
        )
    )
    assert again["note_id"] == created["note_id"] and again["status"].startswith("refreshed")
    replaced = parsed(
        await memory.write_note(
            tool_ctx,
            schemas.WriteNoteInput(
                kind="preference",
                text="eats chia only for fiber",
                supersedes_id=created["note_id"],
                expires_at=datetime(2026, 12, 1, 9, 0),
            ),
        )
    )
    assert replaced["superseded_id"] == created["note_id"] and replaced["expires"] == "2026-12-01"
    active = await repo.list_active_notes(session, tool_ctx.user_id, kinds=[NoteKind.preference])
    assert [n.id for n in active] == [replaced["note_id"]]
    retired = parsed(
        await memory.retire_note(tool_ctx, schemas.RetireNoteInput(id=replaced["note_id"]))
    )
    assert retired["retired_note_id"] == replaced["note_id"]
    twice = await memory.retire_note(tool_ctx, schemas.RetireNoteInput(id=replaced["note_id"]))
    assert twice.is_error
    blank = await memory.write_note(tool_ctx, schemas.WriteNoteInput(kind="rule", text="  "))
    assert blank.is_error


# --------------------------------------------------------------------------------- reminders


async def test_set_and_cancel_reminder_in_user_timezone(
    tool_ctx: ToolContext, session: AsyncSession
) -> None:
    # now is 12:00 in Dubai; a naive 20:00 is local → 16:00 UTC, 8 h ahead
    created = parsed(
        await memory.set_reminder(
            tool_ctx,
            schemas.SetReminderInput(
                when=datetime(2026, 9, 3, 20, 0), text="waist, fasted", kind="measurement"
            ),
        )
    )
    assert created["due_local"] == "2026-09-03 20:00" and created["in_minutes"] == 480
    pending = await repo.pending_reminders(session, tool_ctx.user_id)
    assert len(pending) == 1 and pending[0].kind == "measurement"
    past = await memory.set_reminder(
        tool_ctx, schemas.SetReminderInput(when=datetime(2026, 9, 3, 9, 0), text="x")
    )
    assert past.is_error and "in the past" in str(past.content)
    cancelled = parsed(
        await memory.cancel_reminder(
            tool_ctx, schemas.CancelReminderInput(id=created["reminder_id"])
        )
    )
    assert cancelled["cancelled_reminder_id"] == created["reminder_id"]
    again = await memory.cancel_reminder(
        tool_ctx, schemas.CancelReminderInput(id=created["reminder_id"])
    )
    assert again.is_error and "pending ids: []" in str(again.content)
