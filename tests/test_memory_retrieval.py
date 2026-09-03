"""``memory.retrieval``: typed history by range, keyword search, period path, rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.core.types import LabMarker
from strikt.db import repo
from strikt.db.models import DayFlag, MealSlot, NoteKind, SummaryKind, TurnRole, User
from strikt.memory import retrieval
from strikt.memory.retrieval import HistoryRow, get_history, render_rows, search_history
from tests.test_memory_helpers import (
    TODAY,
    TZ,
    at_local,
    item,
    seed_meal,
    seed_measurement,
    seed_recovery,
    seed_sleep,
    seed_turn,
    seed_workout,
)

NOW_LOCAL = datetime(2026, 9, 3, 12, 0, tzinfo=ZoneInfo(TZ))
TUESDAY = date(2026, 9, 1)


async def _seed_history(session: AsyncSession, user: User, clock: FakeClock) -> None:
    await seed_meal(
        session, user.id, TUESDAY, "13:30", [item("ramen", 780, 35, 90, 25)], slot=MealSlot.lunch
    )
    await seed_meal(
        session,
        user.id,
        TUESDAY,
        "20:00",
        [item("cottage cheese", 180, 30, 6, 1), item("greek yogurt", 120, 20, 8, 0)],
        slot=MealSlot.dinner,
    )
    await seed_meal(session, user.id, TODAY, "09:00", [item("eggs", 140, 12, 1, 10)])
    await seed_workout(session, user.id, TUESDAY, "19:00", sport="strength", strain=9.1)
    await seed_workout(session, user.id, TODAY - timedelta(days=10), "07:00", sport="run")
    await seed_sleep(session, user.id, TUESDAY, "00:30", "07:00")
    await seed_recovery(session, user.id, TUESDAY, score=44)
    await seed_measurement(session, user.id, TUESDAY, "07:10", value=104.2)
    await repo.add_labs(
        session,
        user.id,
        taken_at=date(2026, 8, 20),
        markers=[LabMarker(marker="LDL", value=3.9, unit="mmol/L", ref_high=3.0, flag="high")],
    )
    await repo.add_note(
        session,
        user.id,
        kind=NoteKind.preference,
        text="dislikes chia pudding",
        confidence=0.9,
        now=at_local(TUESDAY, "14:00"),
    )
    await repo.upsert_summary(
        session,
        user.id,
        kind=SummaryKind.day,
        period_start=TUESDAY,
        period_end=TUESDAY,
        text="Ramen lunch at Kinoya, strength in the evening, 1220 kcal.",
        data={"patterns": ["late training"]},
        now=clock.now(),
    )
    await repo.set_day_flag(session, user.id, TUESDAY, DayFlag.salty, True, now=clock.now())
    await repo.close_day(session, user.id, TUESDAY, verdict="Closed at 1220.", now=clock.now())
    await seed_turn(session, user.id, TUESDAY, "13:25", "обед: рамен в Kinoya")
    await seed_turn(
        session, user.id, TUESDAY, "13:26", "Logged: ramen 780 kcal", role=TurnRole.assistant
    )
    await seed_turn(session, user.id, TODAY, "09:01", "eggs for breakfast")
    await session.commit()


async def test_get_history_by_range_and_kind(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await _seed_history(session, user, clock)
    rows = await get_history(
        session,
        user,
        kinds=[
            "meals",
            "workouts",
            "sleep",
            "recoveries",
            "measurements",
            "notes",
            "summaries",
            "days",
        ],
        date_from=TUESDAY,
        date_to=TUESDAY,
    )
    kinds = [r.kind for r in rows]
    assert kinds.count("meal") == 2 and "workout" in kinds and "sleep" in kinds
    assert {"recovery", "measurement", "note", "summary", "day"} <= set(kinds)
    assert rows == sorted(rows, key=lambda r: r.at)  # chronological
    assert all(r.at.tzinfo is not None for r in rows)

    lunch = next(r for r in rows if r.kind == "meal" and r.data["slot"] == "lunch")
    assert lunch.title == "lunch: ramen"
    assert lunch.data["totals"]["kcal"] == 780 and lunch.data["items"][0]["name"] == "ramen"
    assert "ramen 780 kcal (35P/90C/25F)" in lunch.detail
    workout = next(r for r in rows if r.kind == "workout")
    assert workout.title == "strength" and "strain 9.1" in workout.detail
    day = next(r for r in rows if r.kind == "day")
    assert day.title == "day 2026-09-01 closed" and "flags salty" in day.detail
    assert day.data["verdict"] == "Closed at 1220."


async def test_get_history_text_filter_limit_and_turns(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await _seed_history(session, user, clock)
    rows = await get_history(
        session, user, kinds=["meals"], date_from=None, date_to=None, text="yogurt"
    )
    assert [r.data["slot"] for r in rows] == ["dinner"]
    rows = await get_history(session, user, kinds=["meals"], date_from=None, date_to=None, limit=1)
    assert len(rows) == 1 and rows[0].data["day_date"] == TODAY.isoformat()  # most recent
    # turns come only with text or a short range
    with_text = await get_history(
        session, user, kinds=["turns"], date_from=None, date_to=None, text="Kinoya"
    )
    assert [r.data["role"] for r in with_text] == ["user"]
    short = await get_history(session, user, kinds=["turns"], date_from=TUESDAY, date_to=TUESDAY)
    assert len(short) == 2
    long = await get_history(
        session, user, kinds=["turns"], date_from=TUESDAY - timedelta(days=60), date_to=TODAY
    )
    assert long == []
    labs = await get_history(
        session, user, kinds=["labs"], date_from=None, date_to=None, text="ldl"
    )
    assert labs[0].title == "LDL 3.9 mmol/L" and "high" in labs[0].detail
    with pytest.raises(ValueError, match="unknown history kinds"):
        await get_history(session, user, kinds=["bogus"], date_from=None, date_to=None)


async def test_search_history_period_path(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await _seed_history(session, user, clock)
    rows = await search_history(session, user, "what did I eat last Tuesday", now_local=NOW_LOCAL)
    meals = [r for r in rows if r.kind == "meal"]
    assert [r.data["slot"] for r in meals] == ["lunch", "dinner"]
    assert all(r.data.get("day_date", TUESDAY.isoformat()) == TUESDAY.isoformat() for r in meals)
    assert "turn" not in {r.kind for r in rows}  # no keywords left → typed rows only

    rows = await search_history(session, user, "ramen last Tuesday", now_local=NOW_LOCAL)
    assert {r.kind for r in rows} >= {"meal", "summary", "turn"}
    assert [r.data["slot"] for r in rows if r.kind == "meal"] == ["lunch"]

    rows = await search_history(session, user, "во вторник", now_local=NOW_LOCAL)
    assert len([r for r in rows if r.kind == "meal"]) == 2

    # a period with nothing in it → falls through to keyword search
    rows = await search_history(session, user, "ramen 10 days ago", now_local=NOW_LOCAL)
    assert any(r.kind == "meal" and r.data["slot"] == "lunch" for r in rows)


async def test_search_history_keyword_path_and_ranking(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await _seed_history(session, user, clock)
    rows = await search_history(
        session, user, "when did I last have ramen at Kinoya", now_local=NOW_LOCAL
    )
    kinds = {r.kind for r in rows}
    assert {"meal", "summary", "turn"} <= kinds
    assert rows == sorted(rows, key=lambda r: r.at)
    assert await search_history(session, user, "и в на", now_local=NOW_LOCAL) == []
    assert await search_history(session, user, "quinoa", now_local=NOW_LOCAL) == []
    limited = await search_history(session, user, "ramen Kinoya", now_local=NOW_LOCAL, limit=1)
    assert len(limited) == 1


def _rows(n: int) -> list[HistoryRow]:
    base = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    return [
        HistoryRow(
            kind="meal",
            at=base + timedelta(minutes=i),
            title=f"lunch {i}",
            detail="x" * 100,
            data={"id": i},
        )
        for i in range(n)
    ]


def test_render_rows_format_and_budget() -> None:
    assert render_rows([], "en") == ""
    one = render_rows(_rows(1), "en", tz=TZ)
    assert one == "2026-09-01 12:00 meal lunch 0 — " + "x" * 100
    dated = HistoryRow(
        kind="recovery", at=datetime(2026, 8, 31, 20, 0, tzinfo=UTC), title="recovery 44%"
    )
    assert render_rows([dated], "en", tz=TZ) == "2026-09-01 recovery recovery 44%"

    text = render_rows(_rows(50), "en", tz=TZ, max_tokens=300)  # 1200 chars ≈ 9 lines
    lines = text.splitlines()
    assert len(text) <= 300 * retrieval.CHARS_PER_TOKEN
    assert lines[-1].startswith("… truncated ") and lines[-1].endswith(" more")
    shown = len(lines) - 1
    assert lines[-1] == f"… truncated {50 - shown} more"
    ru = render_rows(_rows(50), "ru", tz=TZ, max_tokens=300).splitlines()[-1]
    assert ru.startswith("… ещё ")
    full = render_rows(_rows(5), "en", tz=TZ)
    assert len(full.splitlines()) == 5 and "truncated" not in full


def test_render_rows_always_shows_at_least_one() -> None:
    text = render_rows(_rows(3), "en", tz=TZ, max_tokens=1)
    lines = text.splitlines()
    assert lines[0].startswith("2026-09-01 12:00 meal lunch 0")
    assert lines[-1] == "… truncated 2 more"
    assert retrieval.estimate_tokens("abcd" * 10) == 10
