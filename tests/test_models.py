"""``create_all`` works on SQLite and every enum round-trips through its column type."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from strikt.db.models import (
    ALL_ENUMS,
    USER_OWNED_TABLES,
    Base,
    Note,
    NoteKind,
    User,
    UserStatus,
    enum_col,
)

EXPECTED_TABLES = {
    "users",
    "profiles",
    "protocols",
    "days",
    "meals",
    "meal_items",
    "foods",
    "workouts",
    "sleep",
    "recoveries",
    "measurements",
    "labs",
    "notes",
    "reminders",
    "conversation_turns",
    "summaries",
    "integrations",
    "proactive_sends",
    "token_usage",
    "invites",
    "oauth_states",
    "user_secrets",
}


async def test_create_all_creates_every_table(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
    assert names == EXPECTED_TABLES
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_enum_round_trips_on_both_dialects() -> None:
    for enum_cls in ALL_ENUMS:
        column_type = enum_col(enum_cls, enum_cls.__name__.lower())
        for dialect in (sqlite.dialect(), postgresql.dialect()):
            bind = column_type.bind_processor(dialect)
            result = column_type.result_processor(dialect, None)
            assert bind is not None and result is not None
            for member in enum_cls:
                stored = bind(member)
                assert stored == member.value, (enum_cls, member)
                assert result(stored) is member


def test_enums_are_varchar_not_native() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.Enum):
                assert column.type.native_enum is False, column


async def test_enum_values_persist_and_load(session: AsyncSession) -> None:
    now = datetime(2026, 9, 3, tzinfo=UTC)
    users = [
        User(telegram_id=1000 + i, chat_id=1, status=status, created_at=now)
        for i, status in enumerate(UserStatus)
    ]
    session.add_all(users)
    await session.flush()
    for kind in NoteKind:
        session.add(
            Note(user_id=users[0].id, kind=kind, text=kind.value, confidence=1.0, created_at=now)
        )
    await session.flush()
    session.expunge_all()
    loaded = (await session.scalars(sa.select(User).order_by(User.telegram_id))).all()
    assert [u.status for u in loaded] == list(UserStatus)
    notes = (await session.scalars(sa.select(Note).order_by(Note.id))).all()
    assert [n.kind for n in notes] == list(NoteKind)


def test_user_owned_tables_all_have_user_id() -> None:
    for model in USER_OWNED_TABLES:
        assert "user_id" in model.__table__.columns, model.__tablename__
    owned = {m.__tablename__ for m in USER_OWNED_TABLES}
    assert EXPECTED_TABLES - owned == {"users", "invites", "foods"}


def test_json_columns_have_jsonb_variant() -> None:
    profile = Base.metadata.tables["profiles"]
    likes = profile.c.likes.type
    assert isinstance(likes, sa.JSON)
    assert isinstance(likes.dialect_impl(postgresql.dialect()), postgresql.JSONB)
