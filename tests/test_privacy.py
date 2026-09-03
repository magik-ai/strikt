"""``delete_everything`` removes every row of one user and nothing of another."""

from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.core.types import FoodItemIn, LabMarker, Macros
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import USER_OWNED_TABLES, Invite, User, UserStatus
from strikt.privacy import delete_everything


async def _seed(session: AsyncSession, user_id: int, clock: FakeClock, cipher: TokenCipher) -> None:
    now = clock.now()
    today = date(2026, 9, 3)
    await repo.get_or_open_day(session, user_id, today, now=now)
    await repo.add_meal_with_items(
        session,
        user_id,
        day_date=today,
        items=[FoodItemIn(name="eggs", macros=Macros(kcal=140, protein_g=12, carbs_g=1, fat_g=10))],
        logged_at=now,
    )
    await repo.upsert_workout_by_external(
        session, user_id, source="manual", external_id=None, sport="run", started_at=now, now=now
    )
    await repo.upsert_sleep_by_external(
        session,
        user_id,
        source="manual",
        external_id=None,
        started_at=now - timedelta(hours=8),
        ended_at=now,
        now=now,
    )
    await repo.upsert_recovery_by_external(
        session, user_id, source="whoop", external_id=f"r{user_id}", day=today, score=50
    )
    await repo.add_measurement(
        session, user_id, type="weight", value=100, unit="kg", measured_at=now
    )
    await repo.add_labs(
        session, user_id, taken_at=today, markers=[LabMarker(marker="LDL", value=3.0)]
    )
    n1 = await repo.add_note(session, user_id, kind="preference", text="a", confidence=1, now=now)
    await repo.supersede_note(session, user_id, n1.id, text="b", confidence=1, now=now)
    await repo.add_reminder(session, user_id, due_at=now, text="x", now=now)
    await repo.add_turn(
        session, user_id, role="user", content=[{"type": "text", "text": "hi"}], now=now
    )
    await repo.upsert_summary(
        session,
        user_id,
        kind="day",
        period_start=today,
        period_end=today,
        text="s",
        data=None,
        now=now,
    )
    await repo.set_integration_tokens(
        session,
        cipher,
        user_id,
        "whoop",
        access_token="a",
        refresh_token="b",
        expires_at=None,
        now=now,
    )
    await repo.add_proactive_send(
        session, user_id, trigger="t", window_key="w", step=1, sent_at=now, text="p"
    )
    await repo.add_usage(
        session,
        user_id,
        day=today,
        model="m",
        purpose="turn",
        input_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=1,
        cost_usd=0,
    )
    await repo.create_oauth_state(session, user_id, "whoop", now=now)
    await repo.set_user_secret(session, user_id, "openai", "sk-proj-" + "x" * 30, cipher, now=now)
    await repo.create_invite(session, now=now, created_by=user_id, code=f"inv{user_id}")


async def _count_rows(session: AsyncSession, user_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in USER_OWNED_TABLES:
        value = await session.scalar(
            sa.select(sa.func.count()).select_from(model).where(model.user_id == user_id)
        )
        counts[model.__tablename__] = int(value or 0)
    return counts


async def test_delete_everything_removes_all_rows_of_one_user_only(
    session: AsyncSession, user: User, clock: FakeClock, cipher: TokenCipher
) -> None:
    other, _ = await repo.get_or_create_user(
        session, telegram_id=999, chat_id=999, now=clock.now(), status=UserStatus.active
    )
    await repo.upsert_profile(session, other.id, {"name": "Other"}, now=clock.now())
    await _seed(session, user.id, clock, cipher)
    await _seed(session, other.id, clock, cipher)
    await session.commit()

    before = await _count_rows(session, user.id)
    assert all(v > 0 for v in before.values()), before
    other_before = await _count_rows(session, other.id)

    counts = await delete_everything(session, user.id)
    await session.commit()

    assert counts["users"] == 1
    assert counts["notes"] == 2 and counts["meal_items"] == 1 and counts["integrations"] == 1
    for table, n in before.items():
        assert counts[table] == n, table
    assert all(v == 0 for v in (await _count_rows(session, user.id)).values())
    assert await session.get(User, user.id) is None
    assert (await _count_rows(session, other.id)) == other_before
    assert await session.get(User, other.id) is not None
    invite = await session.get(Invite, f"inv{user.id}")
    assert invite is not None and invite.created_by is None
    assert sum(counts.values()) == sum(before.values()) + 1 + counts["invites.created_by"]


async def test_delete_everything_unknown_user_is_noop(session: AsyncSession) -> None:
    counts = await delete_everything(session, 123456)
    assert sum(counts.values()) == 0
