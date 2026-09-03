"""Read-only SQLAlchemy queries the memory package needs beyond ``strikt.db.repo``.

Every function takes ``user_id`` and filters by it. Date ranges are inclusive local dates;
instant ranges are UTC ``[start, end)``. Text filters use ``to_tsvector`` on Postgres and a
case-insensitive ``LIKE`` elsewhere (the same rule as ``repo._text_match``). Nothing writes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from strikt.db.models import (
    ConversationTurn,
    Lab,
    Meal,
    MealItem,
    Measurement,
    MeasurementType,
    Note,
    NoteKind,
    Recovery,
    Sleep,
    Summary,
    SummaryKind,
    TurnRole,
    Workout,
)


def text_match(session: AsyncSession, column: Any, query: str) -> Any:
    """Full-text predicate: ``to_tsvector('simple')`` on Postgres, case-insensitive LIKE else."""
    needle = " ".join(query.split())
    if str(session.get_bind().dialect.name) == "postgresql":
        return func.to_tsvector("simple", column).op("@@")(func.plainto_tsquery("simple", needle))
    return func.lower(column).like(f"%{needle.lower()}%")


async def _all[T](session: AsyncSession, stmt: Select[tuple[T]]) -> list[T]:
    return list((await session.scalars(stmt)).all())


# ------------------------------------------------------------------------------- measurements


async def latest_measurement_before(
    session: AsyncSession, user_id: int, type: MeasurementType | str, *, before: datetime
) -> Measurement | None:
    stmt = (
        select(Measurement)
        .where(
            Measurement.user_id == user_id,
            Measurement.type == MeasurementType(type),
            Measurement.measured_at < before,
        )
        .order_by(Measurement.measured_at.desc(), Measurement.id.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def measurements_range(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    text: str | None = None,
    limit: int = 50,
) -> list[Measurement]:
    stmt = select(Measurement).where(Measurement.user_id == user_id)
    if start is not None:
        stmt = stmt.where(Measurement.measured_at >= start)
    if end is not None:
        stmt = stmt.where(Measurement.measured_at < end)
    if text:
        stmt = stmt.where(
            sa.or_(
                text_match(session, Measurement.type, text),
                text_match(session, Measurement.note, text),
            )
        )
    stmt = stmt.order_by(Measurement.measured_at.desc(), Measurement.id.desc()).limit(limit)
    return await _all(session, stmt)


# -------------------------------------------------------------------------------------- meals


async def meals_range(
    session: AsyncSession,
    user_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    text: str | None = None,
    limit: int = 50,
) -> list[Meal]:
    """Non-deleted meals (with items), newest first; ``text`` filters on item names."""
    stmt = (
        select(Meal)
        .where(Meal.user_id == user_id, Meal.deleted_at.is_(None))
        .options(selectinload(Meal.items))
    )
    if date_from is not None:
        stmt = stmt.where(Meal.day_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Meal.day_date <= date_to)
    if text:
        matching = select(MealItem.meal_id).where(
            MealItem.user_id == user_id,
            sa.or_(
                text_match(session, MealItem.name, text),
                text_match(session, MealItem.restaurant, text),
                text_match(session, MealItem.brand, text),
            ),
        )
        stmt = stmt.where(Meal.id.in_(matching))
    stmt = stmt.order_by(Meal.day_date.desc(), Meal.logged_at.desc(), Meal.id.desc()).limit(limit)
    return await _all(session, stmt)


# ----------------------------------------------------------------------------------- training


async def workouts_range(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    text: str | None = None,
    limit: int = 50,
) -> list[Workout]:
    stmt = select(Workout).where(Workout.user_id == user_id)
    if start is not None:
        stmt = stmt.where(Workout.started_at >= start)
    if end is not None:
        stmt = stmt.where(Workout.started_at < end)
    if text:
        stmt = stmt.where(
            sa.or_(
                text_match(session, Workout.sport, text), text_match(session, Workout.note, text)
            )
        )
    stmt = stmt.order_by(Workout.started_at.desc(), Workout.id.desc()).limit(limit)
    return await _all(session, stmt)


async def sleep_range(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    limit: int = 50,
) -> list[Sleep]:
    """Sleeps by the instant they *ended* (a night belongs to the morning it ends on)."""
    stmt = select(Sleep).where(Sleep.user_id == user_id)
    if start is not None:
        stmt = stmt.where(Sleep.ended_at >= start)
    if end is not None:
        stmt = stmt.where(Sleep.ended_at < end)
    stmt = stmt.order_by(Sleep.ended_at.desc(), Sleep.id.desc()).limit(limit)
    return await _all(session, stmt)


async def recoveries_range(
    session: AsyncSession,
    user_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    limit: int = 50,
) -> list[Recovery]:
    stmt = select(Recovery).where(Recovery.user_id == user_id)
    if date_from is not None:
        stmt = stmt.where(Recovery.date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Recovery.date <= date_to)
    stmt = stmt.order_by(Recovery.date.desc(), Recovery.id.desc()).limit(limit)
    return await _all(session, stmt)


# --------------------------------------------------------------------------------------- labs


async def labs_range(
    session: AsyncSession,
    user_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    text: str | None = None,
    limit: int = 50,
) -> list[Lab]:
    stmt = select(Lab).where(Lab.user_id == user_id)
    if date_from is not None:
        stmt = stmt.where(Lab.taken_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Lab.taken_at <= date_to)
    if text:
        stmt = stmt.where(text_match(session, Lab.marker, text))
    stmt = stmt.order_by(Lab.taken_at.desc(), Lab.marker).limit(limit)
    return await _all(session, stmt)


# -------------------------------------------------------------------------------------- notes


async def notes_range(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    text: str | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[Note]:
    stmt = select(Note).where(Note.user_id == user_id)
    if active_only:
        stmt = stmt.where(Note.active.is_(True))
    if start is not None:
        stmt = stmt.where(Note.created_at >= start)
    if end is not None:
        stmt = stmt.where(Note.created_at < end)
    if text:
        stmt = stmt.where(text_match(session, Note.text, text))
    stmt = stmt.order_by(Note.created_at.desc(), Note.id.desc()).limit(limit)
    return await _all(session, stmt)


async def active_notes_of_kinds(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime,
    kinds: Iterable[NoteKind | str] | None = None,
) -> list[Note]:
    """Active, unexpired notes (no ordering guarantees; callers sort)."""
    stmt = select(Note).where(
        Note.user_id == user_id,
        Note.active.is_(True),
        sa.or_(Note.expires_at.is_(None), Note.expires_at > now),
    )
    if kinds is not None:
        stmt = stmt.where(Note.kind.in_([NoteKind(k) for k in kinds]))
    return await _all(session, stmt)


# ---------------------------------------------------------------------------------- summaries


async def summaries_range(
    session: AsyncSession,
    user_id: int,
    *,
    kind: SummaryKind | str | None,
    date_from: date | None,
    date_to: date | None,
    text: str | None = None,
    limit: int = 50,
) -> list[Summary]:
    """Summaries whose period overlaps ``[date_from, date_to]``, newest first."""
    stmt = select(Summary).where(Summary.user_id == user_id)
    if kind is not None:
        stmt = stmt.where(Summary.kind == SummaryKind(kind))
    if date_from is not None:
        stmt = stmt.where(Summary.period_end >= date_from)
    if date_to is not None:
        stmt = stmt.where(Summary.period_start <= date_to)
    if text:
        stmt = stmt.where(text_match(session, Summary.text, text))
    stmt = stmt.order_by(Summary.period_start.desc(), Summary.id.desc()).limit(limit)
    return await _all(session, stmt)


# -------------------------------------------------------------------------------------- turns


async def turns_range(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    text: str | None = None,
    role: TurnRole | str | None = None,
    limit: int = 50,
) -> list[ConversationTurn]:
    stmt = select(ConversationTurn).where(ConversationTurn.user_id == user_id)
    if start is not None:
        stmt = stmt.where(ConversationTurn.created_at >= start)
    if end is not None:
        stmt = stmt.where(ConversationTurn.created_at < end)
    if role is not None:
        stmt = stmt.where(ConversationTurn.role == TurnRole(role))
    if text:
        stmt = stmt.where(text_match(session, ConversationTurn.text, text))
    stmt = stmt.order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(
        limit
    )
    return await _all(session, stmt)
