"""The ``<body>`` block, the recent lines' recovery and weight, and WHOOP day strain in ``<day>``."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.db import repo
from strikt.db.models import DataSource, User
from strikt.memory.body import body_block
from strikt.memory.daystate import DayStateBuilder, render_context
from strikt.memory.recent import recent_block

TODAY = date(2026, 9, 3)
DUBAI = "Asia/Dubai"


def _at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


async def _m(
    session: AsyncSession,
    user: User,
    type_: str,
    value: float,
    unit: str,
    at: datetime,
    *,
    note: str | None = None,
    source: str = "withings",
) -> None:
    await repo.add_measurement(
        session,
        user.id,
        type=type_,
        value=value,
        unit=unit,
        measured_at=at,
        source=source,
        note=note,
    )


async def test_body_block_reads_every_number_with_its_date(
    session: AsyncSession, user: User
) -> None:
    assert await body_block(session, user.id, today=TODAY, tz=DUBAI) == ""
    await _m(session, user, "weight", 106.0, "kg", _at(TODAY - timedelta(days=30), 4))
    for offset, value in ((5, 104.8), (3, 104.6), (1, 104.2)):
        await _m(session, user, "weight", value, "kg", _at(TODAY - timedelta(days=offset), 4))
    await _m(session, user, "bodyfat", 24.1, "%", _at(TODAY - timedelta(days=1), 4))
    await _m(
        session, user, "waist", 101.0, "cm", _at(TODAY - timedelta(days=9), 4), source="manual"
    )
    await _m(
        session, user, "other", 9.0, "index", _at(TODAY - timedelta(days=1), 4), note="visceral_fat"
    )
    bp_at = _at(TODAY - timedelta(days=2), 3)
    await _m(session, user, "bp_sys", 128.0, "mmHg", bp_at)
    await _m(session, user, "bp_dia", 84.0, "mmHg", bp_at)
    await _m(session, user, "other", 66.0, "bpm", bp_at, note="heart_rate")
    yesterday_noon = _at(TODAY - timedelta(days=1), 8)  # 12:00 in Dubai
    await _m(session, user, "steps", 11234.0, "steps", yesterday_noon)
    await _m(session, user, "other", 512.3, "kcal", yesterday_noon, note="active_kcal")
    await session.commit()

    text = await body_block(session, user.id, today=TODAY, tz=DUBAI)
    assert text == (
        "weight 104.2 kg (09-02, withings) · 7d avg 104.5 (3 readings) · 30d -1.8"
        " | body fat 24.1% (09-02) | waist 101 cm (08-25) | visceral fat 9 (09-02)"
        " | bp 128/84, pulse 66 (09-01) | steps yesterday 11234, active 512 kcal"
    )


async def test_recent_lines_carry_recovery_and_the_last_weigh_in(
    session: AsyncSession, user: User
) -> None:
    day = TODAY - timedelta(days=1)
    await repo.upsert_recovery_by_external(
        session, user.id, source=DataSource.whoop, external_id="c1", day=day, score=62.0
    )
    await _m(session, user, "weight", 104.6, "kg", _at(day, 3))
    await _m(session, user, "weight", 104.4, "kg", _at(day, 15))
    await session.commit()
    text = await recent_block(session, user.id, today=TODAY, tz=DUBAI, lang="en")
    line = next(line for line in text.splitlines() if line.startswith("09-02"))
    assert line.endswith("| recovery 62% | 104.4 kg")
    assert "no food logged" in line


async def test_day_block_shows_whoop_day_strain_and_burn(
    session: AsyncSession, user: User, clock: FakeClock, settings: object
) -> None:
    await repo.upsert_recovery_by_external(
        session,
        user.id,
        source=DataSource.whoop,
        external_id="c2",
        day=TODAY,
        score=71.0,
        hrv_ms=48.0,
        raw={"cycle": {"strain": 9.4321, "kcal": 2310.4}},
    )
    await session.commit()
    state = await DayStateBuilder(clock, settings).day_state(session, user, TODAY)  # type: ignore[arg-type]
    assert state.recovery is not None and state.recovery.day_strain == 9.4321
    text = render_context(state, "en", tz=DUBAI)
    assert "recovery: 71% · hrv 48 ms · day strain 9.4 · 2310 kcal burned" in text


async def test_steps_add_up_within_a_source_and_two_sources_do_not_double(
    session: AsyncSession, user: User
) -> None:
    day = TODAY - timedelta(days=1)
    for hour, steps in ((5, 3000.0), (9, 4000.0), (13, 2500.0)):  # hourly points, Dubai time +4
        await _m(session, user, "steps", steps, "count", _at(day, hour), source="apple_health")
    await _m(session, user, "steps", 9100.0, "steps", _at(day, 8), source="withings")
    await session.commit()
    text = await body_block(session, user.id, today=TODAY, tz=DUBAI)
    assert text == "steps yesterday 9500"
