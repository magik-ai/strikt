"""``memory.summaries``: day/week gathering, structured output, upsert, fallbacks."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import FakeLLM
from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.models import MealSlot, NoteKind, SummaryKind, User
from strikt.memory import summaries
from strikt.memory.summaries import (
    SUMMARY_SCHEMA,
    SummaryOutput,
    update_week_summary,
    write_day_summary,
)
from tests.test_memory_helpers import (
    TODAY,
    at_local,
    item,
    seed_meal,
    seed_measurement,
    seed_sleep,
    seed_turn,
    seed_workout,
)

MONDAY = TODAY - timedelta(days=3)  # 2026-08-31


def _day_payload(text: str, **data: object) -> dict[str, object]:
    base: dict[str, object] = {
        "totals": {"kcal": 1, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "fiber_g": 1},
        "adherence": {"kcal": 1, "protein": 0, "fiber": 0.5, "bedtime": 1, "meals_logged": 2},
        "patterns": [],
        "flagged": [],
        "user_said": [],
    }
    base.update(data)
    return {"text": text, "data": base}


def test_schema_and_output_model() -> None:
    assert SUMMARY_SCHEMA["additionalProperties"] is False
    assert set(SUMMARY_SCHEMA["properties"]["data"]["required"]) == {
        "totals",
        "adherence",
        "patterns",
        "flagged",
        "user_said",
    }
    parsed = SummaryOutput.model_validate({"text": "x", "data": {"patterns": ["a"], "extra": 1}})
    assert parsed.data.patterns == ["a"] and parsed.data.totals.kcal == 0
    assert "third day in a row" in summaries.load_prompt()


async def test_write_day_summary_gathers_and_upserts(
    session: AsyncSession, user: User, clock: FakeClock, fake_llm: FakeLLM
) -> None:
    day = TODAY - timedelta(days=1)
    await seed_meal(
        session,
        user.id,
        day,
        "13:00",
        [item("chicken plate", 900, 150, 20, 30, fiber=10)],
        slot=MealSlot.lunch,
    )
    await seed_meal(
        session,
        user.id,
        day,
        "20:00",
        [item("cottage cheese", 1010, 48, 60, 20, fiber=20)],
        slot=MealSlot.dinner,
    )
    await seed_workout(session, user.id, day, "18:00", sport="strength", strain=9.1)
    await seed_sleep(session, user.id, day, "00:20", "07:05")
    await seed_measurement(session, user.id, day, "07:30", value=104.2)
    await repo.add_note(
        session,
        user.id,
        kind=NoteKind.answer,
        text="was travelling, no lunch",
        confidence=0.8,
        now=at_local(day, "15:00"),
    )
    await seed_turn(session, user.id, day, "12:50", "Голодный весь день, ем курицу")
    await seed_turn(session, user.id, day, "12:51", "Logged.", role=summaries.TurnRole.assistant)
    await seed_turn(session, user.id, TODAY, "09:00", "eggs")  # not that day
    await repo.upsert_summary(
        session,
        user.id,
        kind=SummaryKind.day,
        period_start=day - timedelta(days=1),
        period_end=day - timedelta(days=1),
        text="prior day",
        data={"patterns": ["one meal until evening"]},
        now=clock.now(),
    )
    await session.commit()

    fake_llm.queue(
        FakeLLM.json_result(
            _day_payload(
                "1910 ккал, белок 198. Один приём до 13:00.",
                patterns=["one meal until evening"],
                user_said=["голодный весь день"],
                totals={"kcal": 9999, "protein_g": 1, "carbs_g": 1, "fat_g": 1, "fiber_g": 1},
            )
        )
    )
    row = await write_day_summary(fake_llm, session, user, day, clock=clock)
    await session.commit()

    call = fake_llm.calls[0]
    assert call["purpose"] == "summary" and call["output_schema"] == SUMMARY_SCHEMA
    assert call["user_id"] == user.id and call["cache_tail"] is False
    assert call["system"] == summaries.load_prompt()
    digest = call["messages"][0]["content"][0]["text"]
    assert f"<day date={day.isoformat()} tz=Asia/Dubai>" in digest
    assert "chicken plate 900 kcal (150P/20C/30F/10fib)" in digest
    assert "training: strength 18:00" in digest
    assert "measurements: weight 104.2 kg" in digest
    assert "- [answer] was travelling, no lunch" in digest
    assert "- 12:50 Голодный весь день, ем курицу" in digest
    assert "Logged." not in digest and "eggs" not in digest  # coach prose and other days excluded
    assert "prior day | patterns: one meal until evening" in digest
    assert "computed (authoritative): meals_logged=2, kcal_hit=True, protein_hit=True" in digest
    assert "Language: Russian" in digest

    assert row.kind == SummaryKind.day and row.period_start == row.period_end == day
    assert row.text == "1910 ккал, белок 198. Один приём до 13:00."
    assert row.data is not None
    assert row.data["totals"]["kcal"] == 1910  # code's totals win over the model's 9999
    assert row.data["patterns"] == ["one meal until evening"]
    assert row.data["user_said"] == ["голодный весь день"]
    computed = row.data["computed"]
    assert computed["kcal_hit"] and computed["protein_hit"] and computed["fiber_hit"]
    assert (
        computed["bedtime_hit"] is True
        and computed["workouts"] == 1
        and computed["sleep_min"] == 370
    )

    # re-running the same day updates in place (idempotent nightly job)
    fake_llm.queue(FakeLLM.json_result(_day_payload("rewritten")))
    again = await write_day_summary(fake_llm, session, user, day, clock=clock)
    assert again.id == row.id and again.text == "rewritten"
    assert len(await repo.list_recent_summaries(session, user.id, SummaryKind.day)) == 2


async def test_write_day_summary_fallbacks(
    session: AsyncSession, user: User, clock: FakeClock, fake_llm: FakeLLM
) -> None:
    await seed_meal(session, user.id, TODAY, "09:00", [item("eggs", 140, 12, 1, 10)])
    await session.commit()
    fake_llm.queue(FakeLLM.refusal())
    row = await write_day_summary(fake_llm, session, user, TODAY, clock=clock)
    assert row.data is not None and row.data["fallback"] is True
    assert row.text.startswith("140/2000 kcal, P 12/210")
    assert row.data["adherence"]["meals_logged"] == 1

    fake_llm.queue(FakeLLM.text("not json at all"))
    row = await write_day_summary(fake_llm, session, user, TODAY, clock=clock)
    assert row.data is not None and row.data["fallback"] is True

    fake_llm.queue(FakeLLM.json_result({"text": "   ", "data": {}}))
    row = await write_day_summary(fake_llm, session, user, TODAY, clock=clock)
    assert row.data is not None and row.data["fallback"] is True

    empty_day = TODAY - timedelta(days=30)
    fake_llm.queue(FakeLLM.refusal())
    row = await write_day_summary(fake_llm, session, user, empty_day, clock=clock)
    assert row.text == "нет данных" and row.data is not None
    assert row.data["computed"]["has_data"] is False


async def test_update_week_summary_aggregates(
    session: AsyncSession, user: User, clock: FakeClock, fake_llm: FakeLLM
) -> None:
    # three day summaries with computed facts, one repeated pattern, two workouts, one weight
    for offset, kcal, protein, hit, patterns in (
        (0, 1900, 200, True, ["skipped lunch → big dinner"]),
        (1, 2600, 120, False, ["skipped lunch → big dinner", "late training"]),
        (2, 1950, 210, True, []),
    ):
        d = MONDAY + timedelta(days=offset)
        await repo.upsert_summary(
            session,
            user.id,
            kind=SummaryKind.day,
            period_start=d,
            period_end=d,
            text=f"day {d.day}",
            data={
                "totals": {
                    "kcal": kcal,
                    "protein_g": protein,
                    "carbs_g": 80,
                    "fat_g": 90,
                    "fiber_g": 25,
                },
                "patterns": patterns,
                "user_said": ["tired"],
                "computed": {
                    "has_data": True,
                    "kcal_hit": hit,
                    "protein_hit": protein >= 189,
                    "fiber_hit": True,
                    "bedtime_hit": offset != 1,
                    "closed": True,
                },
            },
            now=clock.now(),
        )
    await seed_workout(session, user.id, MONDAY, "18:00", sport="strength")
    await seed_workout(session, user.id, MONDAY + timedelta(days=2), "18:00", sport="run")
    await seed_measurement(session, user.id, MONDAY, "07:30")
    await session.commit()

    fake_llm.queue(
        FakeLLM.json_result(
            _day_payload(
                "Week: avg 2150 kcal. Pattern: skipped lunch. Instruction: eat lunch.",
                patterns=["late training"],
            )
        )
    )
    row = await update_week_summary(
        fake_llm, session, user, MONDAY + timedelta(days=3), clock=clock
    )
    await session.commit()

    digest = fake_llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "<week start=2026-08-31 end=2026-09-06" in digest
    assert (
        "- 2026-09-03: no data" in digest
        and "- 2026-08-31: day 31 | totals 1900 kcal, P 200" in digest
    )
    assert "workouts this week: strength 45 min strain 12.3; run 45 min strain 12.3" in digest
    assert "days_with_data=3" in digest and "kcal_hits=2" in digest and "protein_hits=2" in digest
    assert "repeated_patterns=['skipped lunch → big dinner']" in digest

    assert row.kind == SummaryKind.week
    assert row.period_start == MONDAY and row.period_end == MONDAY + timedelta(days=6)
    assert row.data is not None
    c = row.data["computed"]
    assert c["avg_kcal"] == 2150 and c["avg_protein_g"] == 176.7
    assert c["workouts"] == 2 and c["measurements"] == 1 and c["bedtime_hits"] == 2
    assert row.data["adherence"] == {
        "kcal": 0.67,
        "protein": 0.67,
        "fiber": 1.0,
        "bedtime": 0.67,
        "meals_logged": 3,
    }
    assert row.data["patterns"] == ["late training", "skipped lunch → big dinner"]

    # any day of the week resolves to the same Monday; a refusal produces the numbers-only text
    fake_llm.queue(FakeLLM.refusal())
    again = await update_week_summary(
        fake_llm, session, user, MONDAY + timedelta(days=6), clock=clock
    )
    assert again.id == row.id and again.data is not None and again.data["fallback"] is True
    assert again.text.startswith("avg 2150 kcal, P 177, fiber 25; kcal 2/3, protein 2/3")
    assert again.data["patterns"] == ["skipped lunch → big dinner"]


async def test_update_week_summary_without_days(
    session: AsyncSession, user: User, clock: FakeClock, fake_llm: FakeLLM
) -> None:
    fake_llm.queue(FakeLLM.refusal())
    row = await update_week_summary(
        fake_llm, session, user, MONDAY - timedelta(days=14), clock=clock
    )
    assert row.text == "нет данных за неделю"
    assert row.data is not None and row.data["adherence"]["meals_logged"] == 0
