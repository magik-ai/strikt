"""``agent/proactive_decide.py``: structured decision, brief §7.4 enforced in code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import FakeLLM, FakeLLMFactory
from strikt.agent.proactive_decide import (
    DECISION_SCHEMA,
    MAX_CHARS,
    LLMDecider,
    sanitize_proactive_text,
    strip_emoji,
)
from strikt.config import Settings
from strikt.core.clock import FakeClock, to_local
from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import User
from strikt.memory.daystate import DayStateBuilder
from strikt.proactive.types import LadderState, TriggerFire
from tests.conftest import NOW

TODAY = datetime(2026, 9, 3, tzinfo=UTC).date()


def fire(name: str = "no_lunch", **facts: object) -> TriggerFire:
    return TriggerFire(
        name=name,  # type: ignore[arg-type]
        klass="time",
        window_key=f"{name}:2026-09-03",
        local_now=to_local(NOW, "Asia/Dubai"),
        day=TODAY,
        facts=dict(facts) or {"protein_g": 39, "hours_since_wake": 4},
    )


def ladder(step: int = 2) -> LadderState:
    return LadderState(step=step, sends_today=1, cap_today=5, intensity="pushy", response_rate=0.5)


@pytest.fixture
def decider(fake_llm: FakeLLM, settings: Settings, clock: FakeClock) -> LLMDecider:
    return LLMDecider(FakeLLMFactory(fake_llm), settings, clock=clock)


async def test_decision_strips_emoji_caps_lines_and_keeps_step(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    decider: LLMDecider,
    clock: FakeClock,
    settings: Settings,
) -> None:
    fake_llm.queue(
        FakeLLM.json_result(
            {
                "send": True,
                "text": "15:10 😊 Ничего не записано.\n\nБелка 39 г.\nОбед в течение часа. 💪\nЧто ешь?\nпятая строка",
                "reason": "silence past lunch window",
            }
        )
    )
    state = await DayStateBuilder(clock, settings).day_state(session, user, TODAY)
    decision = await decider.decide(session, user, fire(), ladder(2), state)
    assert decision.send is True
    assert "😊" not in decision.text and "💪" not in decision.text
    assert decision.text.split("\n") == [
        "15:10 Ничего не записано.",
        "Белка 39 г.",
        "Обед в течение часа.",
        "Что ешь?",
    ]
    assert decision.step == 2
    assert decision.reason == "silence past lunch window"
    call = fake_llm.calls[0]
    assert call["purpose"] == "proactive"
    assert call["output_schema"] == DECISION_SCHEMA
    assert call["cache_tail"] is False
    assert "Proactive decision" in call["system"][0]["text"]
    prompt = call["messages"][0]["content"][0]["text"]
    assert "trigger: no_lunch" in prompt
    assert "step: 2 of 4" in prompt
    assert '"protein_g": 39' in prompt
    assert "response rate for this trigger: 50%" in prompt
    assert "<profile>" in prompt and "<day>" in prompt and "targets: 2000 kcal" in prompt
    assert "Language: Russian" in prompt


async def test_long_text_is_capped_at_350_chars(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider
) -> None:
    long = " ".join(["Слово"] * 120)
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": long, "reason": ""}))
    decision = await decider.decide(session, user, fire(), ladder(1), None)
    assert decision.send is True
    assert len(decision.text) <= MAX_CHARS
    assert not decision.text.endswith(" ")


async def test_silent_decision_has_no_text(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider
) -> None:
    fake_llm.queue(
        FakeLLM.json_result({"send": False, "text": "ignored", "reason": "day flagged sick"})
    )
    decision = await decider.decide(session, user, fire(), ladder(3), None)
    assert decision.send is False and decision.text == "" and decision.reason == "day flagged sick"


async def test_refusal_and_bad_json_are_silent(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider
) -> None:
    fake_llm.queue(FakeLLM.refusal(), FakeLLM.text("not json"))
    first = await decider.decide(session, user, fire(), ladder(1), None)
    second = await decider.decide(session, user, fire(), ladder(1), None)
    assert first.send is False and first.reason == "refusal"
    assert second.send is False and second.reason == "invalid_output"


async def test_step_never_below_one_and_send_with_empty_text_is_silent(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider
) -> None:
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": "🎉🎉", "reason": ""}))
    decision = await decider.decide(session, user, fire(), ladder(0), None)
    assert decision.send is False and decision.step == 1 and decision.reason == "empty_text"


async def test_prompt_includes_summaries_notes_and_sends_today(
    session: AsyncSession,
    user: User,
    fake_llm: FakeLLM,
    decider: LLMDecider,
    clock: FakeClock,
    settings: Settings,
) -> None:
    await repo.upsert_summary(
        session,
        user.id,
        kind="day",
        period_start=TODAY - timedelta(days=1),
        period_end=TODAY - timedelta(days=1),
        text="one meal until 19:00 → 2,400 kcal",
        data=None,
        now=NOW,
    )
    await repo.add_note(
        session,
        user.id,
        kind="pattern",
        text="skipped lunch ends in evening overeating",
        confidence=0.8,
        now=NOW,
    )
    await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_first_meal",
        window_key="no_first_meal:2026-09-03",
        step=1,
        sent_at=NOW - timedelta(hours=1),
        text="Nothing logged. Breakfast?",
    )
    await repo.add_meal_with_items(
        session,
        user.id,
        day_date=TODAY,
        items=[FoodItemIn(name="eggs", macros=Macros(kcal=300, protein_g=20, carbs_g=2, fat_g=22))],
        logged_at=NOW,
    )
    await session.commit()
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": "ok", "reason": ""}))
    state = await DayStateBuilder(clock, settings).day_state(session, user, TODAY)
    await decider.decide(
        session, user, fire("protein_check", protein_g=20, lunch="skipped"), ladder(2), state
    )
    prompt = fake_llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "<recent_days>" in prompt and "one meal until 19:00" in prompt
    assert "<notes>" in prompt and "skipped lunch" in prompt
    assert (
        "<sent_today>" in prompt
        and "Nothing logged. Breakfast?" in prompt
        and "unanswered" in prompt
    )
    assert "eggs" in prompt


def test_sanitize_helpers() -> None:
    assert strip_emoji("ok 👍🏽 done ✅") == "ok  done "
    assert sanitize_proactive_text("  a  \n\n\n b\nc\nd\ne") == "a\nb\nc\nd"
    assert sanitize_proactive_text("") == ""
    text = sanitize_proactive_text("x" * 400)
    assert len(text) <= MAX_CHARS


async def test_weekly_review_keeps_five_lines_while_no_lunch_stays_capped(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider
) -> None:
    """Brief §7.1/§7.5: the Sunday review is "the week in five lines" plus a pattern and an
    instruction; the generic 4-line cap must not eat the instruction."""
    review = (
        "Неделя: 1 980 ккал в среднем, белок 192 г.\n"
        "Клетчатка 24 г — ниже цели 30.\n"
        "Тренировки: 3 из 3. Отбой в срок 4 ночи из 7.\n"
        "Паттерн: обе субботы — одна еда до вечера, потом 2 600.\n"
        "Задача недели: обед до 14:00 в субботу, замер талии в четверг."
    )
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": review, "reason": "sunday"}))
    decision = await decider.decide(session, user, fire("weekly_review"), ladder(1), None)
    assert decision.text == review  # all five lines survive
    fake_llm.queue(FakeLLM.json_result({"send": True, "text": review, "reason": "x"}))
    capped = await decider.decide(session, user, fire("no_lunch"), ladder(1), None)
    assert len(capped.text.split("\n")) == 4
    assert sanitize_proactive_text("x" * 500, "weekly_review") == "x" * 500
    assert len(sanitize_proactive_text("x" * 800, "weekly_review")) <= 700


async def test_truncated_decision_is_reported_as_truncated(
    session: AsyncSession, user: User, fake_llm: FakeLLM, decider: LLMDecider, settings: Settings
) -> None:
    from strikt.agent.client import STOP_MAX_TOKENS, LLMResult

    fake_llm.queue(
        LLMResult(
            content=[
                {"type": "thinking", "thinking": "…"},
                {"type": "text", "text": '{"send": true, "text": "14:10. Ничего не за'},
            ],
            stop_reason=STOP_MAX_TOKENS,
        )
    )
    decision = await decider.decide(session, user, fire(), ladder(2), None)
    assert decision.send is False and decision.reason == "truncated"
    assert settings.max_tokens_proactive >= 4096  # the cap only bounds thinking
