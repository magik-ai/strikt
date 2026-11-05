"""Extra queries the integrations need that ``strikt.db.repo`` does not provide.

Everything here filters by ``user_id`` and flushes; callers commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.db.models import (
    DataSource,
    Integration,
    IntegrationLink,
    IntegrationStatus,
    LinkKind,
    Measurement,
    MeasurementType,
    Provider,
    Recovery,
    Sleep,
    Workout,
)


def _rowcount(result: Any) -> int:
    if isinstance(result, CursorResult):
        return int(result.rowcount)
    return int(getattr(result, "rowcount", 0))


# ------------------------------------------------------------------------------- measurements


async def find_measurement(
    session: AsyncSession,
    user_id: int,
    *,
    source: str,
    type: MeasurementType | str,
    measured_at: datetime,
    note: str | None = None,
) -> Measurement | None:
    """The natural key for imported measurements: (source, type, instant, metric note)."""
    stmt = select(Measurement).where(
        Measurement.user_id == user_id,
        Measurement.source == source,
        Measurement.type == MeasurementType(type),
        Measurement.measured_at == measured_at,
    )
    stmt = stmt.where(Measurement.note.is_(None) if note is None else Measurement.note == note)
    return (await session.scalars(stmt.limit(1))).first()


async def upsert_measurement(
    session: AsyncSession,
    user_id: int,
    *,
    type: MeasurementType | str,
    value: float,
    unit: str,
    measured_at: datetime,
    source: str,
    raw: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> tuple[Measurement, bool]:
    """Insert or update by the natural key. Returns ``(row, created)``."""
    row = await find_measurement(
        session, user_id, source=source, type=type, measured_at=measured_at, note=note
    )
    created = row is None
    if row is None:
        row = Measurement(
            user_id=user_id,
            type=MeasurementType(type),
            measured_at=measured_at,
            source=source,
            note=note,
        )
        session.add(row)
    row.value = value
    row.unit = unit
    row.raw = dict(raw) if raw is not None else None
    await session.flush()
    return row, created


# ------------------------------------------------------------------------------ external ids


async def get_sleep_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> Sleep | None:
    stmt = select(Sleep).where(
        Sleep.user_id == user_id,
        Sleep.source == DataSource(source),
        Sleep.external_id == external_id,
    )
    return (await session.scalars(stmt.limit(1))).first()


async def get_workout_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> Workout | None:
    stmt = select(Workout).where(
        Workout.user_id == user_id,
        Workout.source == DataSource(source),
        Workout.external_id == external_id,
    )
    return (await session.scalars(stmt.limit(1))).first()


async def get_recovery_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> Recovery | None:
    stmt = select(Recovery).where(
        Recovery.user_id == user_id,
        Recovery.source == DataSource(source),
        Recovery.external_id == external_id,
    )
    return (await session.scalars(stmt.limit(1))).first()


async def delete_workout_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> bool:
    result = await session.execute(
        sa.delete(Workout).where(
            Workout.user_id == user_id,
            Workout.source == DataSource(source),
            Workout.external_id == external_id,
        )
    )
    return _rowcount(result) > 0


async def delete_sleep_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> bool:
    result = await session.execute(
        sa.delete(Sleep).where(
            Sleep.user_id == user_id,
            Sleep.source == DataSource(source),
            Sleep.external_id == external_id,
        )
    )
    return _rowcount(result) > 0


async def delete_recovery_by_external(
    session: AsyncSession, user_id: int, source: DataSource | str, external_id: str
) -> bool:
    result = await session.execute(
        sa.delete(Recovery).where(
            Recovery.user_id == user_id,
            Recovery.source == DataSource(source),
            Recovery.external_id == external_id,
        )
    )
    return _rowcount(result) > 0


# ------------------------------------------------------------------------------- integrations


async def list_connected(
    session: AsyncSession, provider: Provider | str | None = None
) -> list[Integration]:
    """Every integration row that can be synced right now."""
    stmt = (
        select(Integration)
        .where(Integration.status == IntegrationStatus.connected)
        .order_by(Integration.id)
    )
    if provider is not None:
        stmt = stmt.where(Integration.provider == Provider(provider))
    return list((await session.scalars(stmt)).all())


# ------------------------------------------------------------------------------------- links


async def list_links(
    session: AsyncSession,
    user_id: int,
    provider: Provider | str,
    kind: LinkKind | str,
    *,
    day: date | None = None,
    key: str | None = None,
) -> list[IntegrationLink]:
    """What Strikt created in the provider's account: one day's entries, or a food by key."""
    stmt = select(IntegrationLink).where(
        IntegrationLink.user_id == user_id,
        IntegrationLink.provider == Provider(provider),
        IntegrationLink.kind == LinkKind(kind),
    )
    if day is not None:
        stmt = stmt.where(IntegrationLink.day == day)
    if key is not None:
        stmt = stmt.where(IntegrationLink.key == key)
    return list((await session.scalars(stmt.order_by(IntegrationLink.id))).all())


async def linked_days(
    session: AsyncSession, user_id: int, provider: Provider | str, *, since: date
) -> set[date]:
    """Days from ``since`` on that hold at least one entry Strikt created."""
    rows = await session.scalars(
        select(IntegrationLink.day)
        .where(
            IntegrationLink.user_id == user_id,
            IntegrationLink.provider == Provider(provider),
            IntegrationLink.kind == LinkKind.entry,
            IntegrationLink.day >= since,
        )
        .distinct()
    )
    return {day for day in rows.all() if day is not None}


async def add_link(
    session: AsyncSession,
    user_id: int,
    provider: Provider | str,
    kind: LinkKind | str,
    *,
    key: str,
    external_id: str,
    now: datetime,
    day: date | None = None,
    data: Mapping[str, Any] | None = None,
) -> IntegrationLink:
    row = IntegrationLink(
        user_id=user_id,
        provider=Provider(provider),
        kind=LinkKind(kind),
        day=day,
        key=key,
        external_id=external_id,
        data=dict(data) if data is not None else None,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def delete_link(session: AsyncSession, user_id: int, link_id: int) -> bool:
    result = await session.execute(
        sa.delete(IntegrationLink).where(
            IntegrationLink.user_id == user_id, IntegrationLink.id == link_id
        )
    )
    return _rowcount(result) > 0


async def list_unannounced(
    session: AsyncSession, status: IntegrationStatus | str
) -> list[Integration]:
    """Rows in ``status`` whose user has not been told yet (``notified_at`` empty)."""
    stmt = (
        select(Integration)
        .where(
            Integration.status == IntegrationStatus(status),
            Integration.notified_at.is_(None),
        )
        .order_by(Integration.id)
    )
    return list((await session.scalars(stmt)).all())
