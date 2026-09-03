"""``agent/context.py``: deterministic bytes, cache markers, the ``<context>`` block, history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.context import (
    ONBOARDING_STEPS,
    build_context,
    looks_like_past_question,
    render_onboarding_checklist,
    render_profile_block,
    user_blocks,
)
from strikt.agent.tools import Registry
from strikt.config import Settings
from strikt.core.clock import FakeClock
from strikt.core.types import Attachment, Incoming
from strikt.db import repo
from strikt.db.models import Profile, Protocol, TurnRole, User, UserStatus
from strikt.memory.daystate import DayStateBuilder
from tests.conftest import CHAT_ID, NOW


def incoming(
    user: User, text: str | None, *, attachments: list[Attachment] | None = None
) -> Incoming:
    return Incoming(
        user_id=user.id,
        chat_id=CHAT_ID,
        message_id=42,
        text=text,
        attachments=attachments or [],
        received_at=NOW,
    )


async def build(
    session: AsyncSession,
    user: User,
    clock: FakeClock,
    settings: Settings,
    registry: Registry,
    inc: Incoming,
) -> tuple[
    list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], dict[str, int]
]:
    bundle = await build_context(
        session,
        user,
        inc,
        clock=clock,
        settings=settings,
        state_provider=DayStateBuilder(clock, settings),
        registry=registry,
    )
    return bundle.system, bundle.messages, bundle.tools, bundle.budget


async def test_identical_inputs_render_identical_bytes(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    inc = incoming(user, "200 г творога 0.5%")
    first = await build(session, user, clock, settings, registry, inc)
    second = await build(session, user, clock, settings, registry, inc)
    assert json.dumps(first[:3], ensure_ascii=False) == json.dumps(second[:3], ensure_ascii=False)


async def test_system_blocks_are_static_and_cache_marked(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    system, _messages, tools, budget = await build(
        session, user, clock, settings, registry, incoming(user, "hi")
    )
    assert len(system) == 2
    assert system[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "Strikt" in str(system[0]["text"])
    joined = str(system[0]["text"]) + str(system[1]["text"])
    for volatile in ("2026-09-03", "12:00", "08:00 UTC", "now:"):
        assert volatile not in joined, volatile
    assert "<profile>" in str(system[1]["text"])
    assert "kcal 2000 | P 210 g" in str(system[1]["text"])
    assert "onboarding_done: yes" in str(system[1]["text"])
    assert "onboarding_checklist" not in str(system[1]["text"])
    assert [t["name"] for t in tools] == sorted(t["name"] for t in tools)
    assert budget["total"] > 0
    assert budget["system_static"] > 1000  # cacheable on Sonnet 5 (≥ 1024 tokens)


async def test_profile_block_has_sorted_keys_and_no_timestamps(
    user: User, profile: Profile, protocol: Protocol
) -> None:
    text = render_profile_block(user, profile, protocol, "- [preference] #1 dislikes chia")
    lines = [line for line in text.split("\n") if ": " in line and not line.startswith("<")]
    keys = [
        line.split(":")[0]
        for line in lines
        if line.split(":")[0] not in {"language", "timezone", "rationale"}
    ]
    assert keys == sorted(keys)
    assert "updated_at" not in text
    assert "onboarding_done_at" not in text
    assert "wake_time: 08:00" in text
    assert "dislikes chia" in text
    assert "<protocol version=1>" in text


async def test_current_message_starts_with_context_block_then_media_then_text(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    photo = Attachment(kind="image", mime="image/jpeg", bytes_b64="YWJj", sha256="deadbeef")
    photo2 = Attachment(kind="image", mime="image/png", bytes_b64="ZGVm", sha256="cafebabe")
    voice = Attachment(kind="voice", text="творог и ягоды")
    inc = incoming(user, "ужин", attachments=[photo, photo2, voice])
    _system, messages, _, _ = await build(session, user, clock, settings, registry, inc)
    last = messages[-1]
    assert last["role"] == "user"
    content = last["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    ctx_text = content[0]["text"]
    assert ctx_text.startswith("<context>")
    assert "now: 2026-09-03 12:00" in ctx_text
    assert "Asia/Dubai" in ctx_text
    assert "<day>" in ctx_text and "targets: 2000 kcal" in ctx_text
    types = [b["type"] for b in content[1:]]
    assert types == ["text", "image", "text", "image", "text"]
    assert content[1]["text"] == "Image 1:"
    assert content[2]["source"]["media_type"] == "image/jpeg"
    assert "[voice transcript] творог и ягоды" in content[-1]["text"]
    assert content[-1]["text"].endswith("ужин")


def test_user_blocks_without_anything_is_not_empty() -> None:
    inc = Incoming(user_id=1, chat_id=1, message_id=1, text=None, received_at=NOW)
    assert user_blocks(inc) == [{"type": "text", "text": "[empty message]"}]
    fwd = Incoming(
        user_id=1, chat_id=1, message_id=1, text="меню", forwarded_from="Kinoya", received_at=NOW
    )
    assert user_blocks(fwd)[0]["text"] == "[forwarded from Kinoya]\nменю"


async def test_onboarding_user_gets_the_interview_and_checklist(
    session: AsyncSession, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    user, _ = await repo.get_or_create_user(
        session, telegram_id=999, chat_id=999, now=clock.now(), language="en", timezone="UTC"
    )
    assert user.status == UserStatus.onboarding
    await session.commit()
    system, _, _, _ = await build(session, user, clock, settings, registry, incoming(user, "hi"))
    text = str(system[1]["text"])
    assert "Onboarding interview" in text
    assert "<onboarding_checklist>" in text
    assert "1. [todo] identity" in text
    assert "next step: 1" in text
    assert "no profile yet" in text


def test_checklist_marks_done_steps(profile: Profile) -> None:
    profile.onboarding_step = 4
    text = render_onboarding_checklist(profile)
    assert "4. [done]" in text
    assert "5. [todo]" in text
    assert "next step: 5" in text
    assert len(ONBOARDING_STEPS) == 10


async def test_history_is_limited_alternating_and_cache_marked(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    base = NOW - timedelta(hours=5)
    for i in range(46):  # ends on an assistant turn: the normal case
        role = TurnRole.user if i % 2 == 0 else TurnRole.assistant
        await repo.add_turn(
            session,
            user.id,
            role=role,
            content=[{"type": "text", "text": f"turn {i}"}],
            now=base + timedelta(minutes=i),
        )
    await session.commit()
    settings.context_max_turns = 30
    _, messages, _, budget = await build(
        session, user, clock, settings, registry, incoming(user, "?")
    )
    history = messages[:-1]
    texts = [m["content"][0]["text"] for m in history]
    assert texts == [f"turn {i}" for i in range(16, 46)]  # 30 rows at a 16-row hysteresis step
    assert history[0]["role"] == "user"
    roles = [m["role"] for m in messages]
    assert all(a != b for a, b in pairwise(roles))
    last_block = history[-1]["content"][-1]
    assert last_block["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in messages[-1]["content"][0]
    assert budget["history"] > 0


async def test_dangling_user_turn_is_merged_into_the_current_message(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    """A user turn without a reply (a crashed run) must not break user/assistant alternation."""
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=[{"type": "text", "text": "first"}],
        now=NOW - timedelta(minutes=10),
    )
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.assistant,
        content=[{"type": "text", "text": "reply"}],
        now=NOW - timedelta(minutes=9),
    )
    await repo.add_turn(
        session,
        user.id,
        role=TurnRole.user,
        content=[{"type": "text", "text": "unanswered"}],
        now=NOW - timedelta(minutes=5),
    )
    await session.commit()
    _, messages, _, _ = await build(session, user, clock, settings, registry, incoming(user, "?"))
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    current = messages[-1]["content"]
    assert current[0]["text"] == "unanswered"
    assert current[0]["cache_control"] == {"type": "ephemeral"}  # the stable part stays marked
    assert current[1]["text"].startswith("<context>")


async def test_history_token_budget_trims_oldest(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    base = NOW - timedelta(hours=2)
    for i in range(6):
        await repo.add_turn(
            session,
            user.id,
            role=TurnRole.user if i % 2 == 0 else TurnRole.assistant,
            content=[{"type": "text", "text": "x" * 400}],
            now=base + timedelta(minutes=i),
        )
    await session.commit()
    settings.context_max_tokens = 250  # ≈ two 400-char turns
    _, messages, _, _ = await build(session, user, clock, settings, registry, incoming(user, "?"))
    assert len(messages) <= 3  # ≤ 2 history messages + the current one


async def test_past_question_adds_history_block(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    assert looks_like_past_question("что я ел вчера", now_local=clock.now(), lang="ru")
    assert looks_like_past_question("what did I eat last Tuesday", now_local=clock.now(), lang="en")
    assert not looks_like_past_question("200 г творога", now_local=clock.now(), lang="ru")
    _, messages, _, _ = await build(
        session, user, clock, settings, registry, incoming(user, "что я ел вчера?")
    )
    ctx = messages[-1]["content"][0]["text"]
    assert "<history>" in ctx
    _, messages, _, _ = await build(
        session, user, clock, settings, registry, incoming(user, "творог")
    )
    assert "<history>" not in messages[-1]["content"][0]["text"]


async def test_reminders_and_answered_proactive_send_appear(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    await repo.add_reminder(
        session,
        user.id,
        due_at=NOW + timedelta(hours=20),
        text="талия натощак",
        now=NOW,
        kind="measurement",
    )
    await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_first_meal",
        window_key="no_first_meal:2026-09-03",
        step=2,
        sent_at=NOW - timedelta(minutes=30),
        text="11:30. Ничего не записано.",
    )
    await session.commit()
    bundle = await build_context(
        session,
        user,
        incoming(user, "проспал"),
        clock=clock,
        settings=settings,
        state_provider=DayStateBuilder(clock, settings),
        registry=registry,
    )
    ctx = bundle.messages[-1]["content"][0]["text"]
    assert "<reminders>" in ctx and "талия натощак" in ctx
    assert "<proactive>" in ctx and "ladder step 2 of 4" in ctx
    assert bundle.answered_send is not None
    assert bundle.answered_send.trigger == "no_first_meal"


async def test_context_shows_yesterday_and_summaries(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    yesterday = datetime(2026, 9, 2, tzinfo=UTC).date()
    await repo.close_day(session, user.id, yesterday, verdict="1910 / 198 P / 30 fiber", now=NOW)
    await repo.upsert_summary(
        session,
        user.id,
        kind="week",
        period_start=datetime(2026, 8, 31, tzinfo=UTC).date(),
        period_end=datetime(2026, 9, 6, tzinfo=UTC).date(),
        text="avg 1950 kcal, protein 4/5",
        data=None,
        now=NOW,
    )
    await session.commit()
    _, messages, _, _ = await build(session, user, clock, settings, registry, incoming(user, "?"))
    ctx = messages[-1]["content"][0]["text"]
    assert "<yesterday>" in ctx and "1910 / 198 P" in ctx
    assert "<summaries>" in ctx and "avg 1950 kcal" in ctx


async def test_history_prefix_is_stable_across_turns_hysteresis(
    session: AsyncSession, user: User, clock: FakeClock, settings: Settings, registry: Registry
) -> None:
    """Past ``context_max_turns`` a plain sliding window would move ``messages[0]`` every turn and
    the history cache entry would be written but never read; the window start must only move
    every ``HISTORY_SLACK`` rows."""
    from strikt.agent.context import HISTORY_SLACK, history_messages, history_window

    assert history_window(20, 30) == 20
    assert history_window(30, 30) == 30
    assert history_window(31, 30) == 31  # under the slack: keep everything
    assert history_window(45, 30) == 45
    assert history_window(46, 30) == 30  # one step: drop the oldest 16
    assert history_window(61, 30) == 45
    assert history_window(62, 30) == 30
    base = NOW - timedelta(hours=5)
    for i in range(50):
        role = TurnRole.user if i % 2 == 0 else TurnRole.assistant
        await repo.add_turn(
            session,
            user.id,
            role=role,
            content=[{"type": "text", "text": f"turn {i}"}],
            now=base + timedelta(minutes=i),
        )
    await session.commit()
    settings.context_max_turns = 30
    first, _ = await history_messages(session, user, settings)
    for i in (50, 51):
        await repo.add_turn(
            session,
            user.id,
            role=TurnRole.user if i % 2 == 0 else TurnRole.assistant,
            content=[{"type": "text", "text": f"turn {i}"}],
            now=base + timedelta(minutes=i),
        )
    await session.commit()
    second, _ = await history_messages(session, user, settings)
    assert len(first) == 34 and len(second) == 36
    assert second[: len(first)] == first  # byte-stable prefix: the cache entry is read
    assert first[0]["content"][0]["text"] == "turn 16"
    assert HISTORY_SLACK == 16


def test_estimate_tokens_counts_cyrillic_denser() -> None:
    from strikt.agent.context import estimate_tokens

    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("я" * 400) == 160  # 2.5 chars/token, not 4
    assert estimate_tokens({"text": "яя"}) >= 3
