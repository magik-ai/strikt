"""Settings behaviour on managed hosts: plain Postgres URLs and the PORT variable."""

from __future__ import annotations

import pytest

from strikt.config import Settings


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_plain_postgres_url_gets_the_asyncpg_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
    assert _settings().database_url == "postgresql+asyncpg://u:p@host:5432/db"
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    assert _settings().database_url == "postgresql+asyncpg://u:p@host/db"


def test_explicit_driver_and_sqlite_urls_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@host/db")
    assert _settings().database_url == "postgresql+asyncpg://u:p@host/db"
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./dev.db")
    assert _settings().database_url == "sqlite+aiosqlite:///./dev.db"


def test_port_variable_is_honoured_when_web_port_is_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEB_PORT", raising=False)
    monkeypatch.setenv("PORT", "6543")
    assert _settings().web_port == 6543
    monkeypatch.setenv("WEB_PORT", "9000")
    assert _settings().web_port == 9000
    monkeypatch.delenv("WEB_PORT")
    monkeypatch.setenv("PORT", "not-a-port")
    assert _settings().web_port == 8080
