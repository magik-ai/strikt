"""The initial migration matches ``Base.metadata`` (applied on SQLite, compared with Alembic)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from strikt.db.models import Base

ROOT = Path(__file__).resolve().parent.parent


def test_initial_migration_matches_models(tmp_path: Path) -> None:
    db_path = tmp_path / "migrated.sqlite"
    url = f"sqlite+aiosqlite:///{db_path}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) - {"alembic_version"} == set(
            Base.metadata.tables
        )
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": False}
        )
        diffs = compare_metadata(context, Base.metadata)
    assert diffs == [], diffs


def test_migration_downgrade_is_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "down.sqlite"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
