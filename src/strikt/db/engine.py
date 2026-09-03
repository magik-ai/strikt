"""Async engine and session factory. SQLite (aiosqlite) is used only by tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from strikt.db.models import Base

SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine. SQLite gets a shared static pool and ``foreign_keys=ON``."""
    kwargs: dict[str, Any] = {"echo": echo}
    if is_sqlite(url):
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_async_engine(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_fks(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=10)
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_sqlite_for_tests(engine: AsyncEngine) -> None:
    """``create_all`` for the test database (production uses Alembic migrations)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
