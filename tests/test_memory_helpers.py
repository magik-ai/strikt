"""Seeders shared by the memory tests (not a test module: no ``test_`` functions)."""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import local_datetime
from strikt.core.types import FoodItemIn, Macros
from strikt.db import repo
from strikt.db.models import DataSource, Meal, MealSlot, MeasurementType, TurnRole

TZ = "Asia/Dubai"
TODAY = date(2026, 9, 3)  # Thursday


def at_local(day: date, hhmm: str, tz: str = TZ) -> datetime:
    hour, minute = (int(p) for p in hhmm.split(":"))
    return local_datetime(day, time(hour, minute), tz)


def item(
    name: str,
    kcal: float,
    p: float = 0,
    c: float = 0,
    f: float = 0,
    fiber: float = 0,
    *,
    countable: bool = True,
) -> FoodItemIn:
    return FoodItemIn(
        name=name,
        macros=Macros(kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=fiber),
        countable=countable,
    )


async def seed_meal(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    items: list[FoodItemIn],
    *,
    slot: MealSlot | str = MealSlot.unknown,
    note: str | None = None,
    item_flags: dict[int, list[str]] | None = None,
) -> Meal:
    when = at_local(day, hhmm)
    return await repo.add_meal_with_items(
        session,
        user_id,
        day_date=day,
        items=items,
        slot=slot,
        logged_at=when,
        eaten_at=when,
        note=note,
        item_flags=item_flags,
    )


async def seed_workout(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    *,
    sport: str = "run",
    duration_min: float = 45,
    strain: float | None = 12.3,
    kcal: float | None = 406,
    avg_hr: int | None = 130,
    external_id: str | None = None,
) -> None:
    start = at_local(day, hhmm)
    await repo.upsert_workout_by_external(
        session,
        user_id,
        source=DataSource.whoop,
        external_id=external_id or f"w-{day.isoformat()}-{hhmm}",
        sport=sport,
        started_at=start,
        now=start,
        duration_min=duration_min,
        strain=strain,
        kcal=kcal,
        avg_hr=avg_hr,
        zones_min={"z0": 10, "z1": 20, "z2": 15},
    )


async def seed_sleep(
    session: AsyncSession,
    user_id: int,
    end_day: date,
    start_hhmm: str,
    end_hhmm: str,
    *,
    asleep_min: float = 370,
    performance_pct: float = 78,
) -> None:
    from datetime import timedelta

    start_day = end_day - timedelta(days=1) if start_hhmm > end_hhmm else end_day
    start = at_local(start_day, start_hhmm)
    end = at_local(end_day, end_hhmm)
    await repo.upsert_sleep_by_external(
        session,
        user_id,
        source=DataSource.whoop,
        external_id=f"s-{end_day.isoformat()}",
        started_at=start,
        ended_at=end,
        now=end,
        in_bed_min=asleep_min + 20,
        asleep_min=asleep_min,
        performance_pct=performance_pct,
    )


async def seed_recovery(
    session: AsyncSession, user_id: int, day: date, *, score: float = 61, rhr: float = 52
) -> None:
    await repo.upsert_recovery_by_external(
        session,
        user_id,
        source=DataSource.whoop,
        external_id=f"r-{day.isoformat()}",
        day=day,
        score=score,
        rhr=rhr,
        hrv_ms=48,
    )


async def seed_measurement(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    *,
    type: MeasurementType | str = MeasurementType.weight,
    value: float = 104.2,
    unit: str = "kg",
) -> None:
    await repo.add_measurement(
        session, user_id, type=type, value=value, unit=unit, measured_at=at_local(day, hhmm)
    )


async def seed_turn(
    session: AsyncSession,
    user_id: int,
    day: date,
    hhmm: str,
    text: str,
    *,
    role: TurnRole = TurnRole.user,
) -> int:
    turn = await repo.add_turn(
        session,
        user_id,
        role=role,
        content=[{"type": "text", "text": text}],
        now=at_local(day, hhmm),
    )
    return turn.id
