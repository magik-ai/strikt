"""onboarding.importer: parsing tolerance, writes with source=imported, idempotency."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import to_local
from strikt.db import repo
from strikt.db.models import DataSource, MealSlot, MealSource, NoteKind, User, UserStatus
from strikt.onboarding import importer

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)

SAMPLE = """
meal | 2026-08-14 | 13:20 | lunch | Kinoya tonkotsu ramen | kcal=780 p=38 c=85 f=30 fiber=4 | loose
meal | 2026-08-14 | 20:10 | dinner | cottage cheese 0.5% 200 g; Greek yogurt 0% 160 g; raspberries 100 g | kcal=420 p=52 c=28 f=6 fiber=7
meal | 2026-08-15 | breakfast | eggs (kcal=140 p=12 c=1 f=10); toast (kcal=80 p=3 c=15 f=1)
workout | 2026-08-14 | 18:30 | strength | duration=62 strain=9.4 kcal=410 avg_hr=118 max_hr=156
sleep | 2026-08-14 | 00:40 | 08:05 | asleep=390 performance=71
sleep | 2026-08-15 | 23:30 | 07:00 | asleep=400
measurement | 2026-08-18 | waist | 103 | cm
measurement | 2026-08-18 | weight | 104.2 | kg
lab | 2026-06-02 | LDL | 3.9 | mmol/L | ref=0-3.0 | high
note | preference | dislikes chia pudding; eats it only for fiber
note | pattern | days with one meal until evening ended in 2,400+ kcal
note | health | lipid panel present; avoid coconut oil as fat source
protocol | 2026-07-01 | kcal=2200 p=180 f=120 c=150 fiber=25 | earlier scheme
protocol | 2026-08-01 | kcal=2000 p=210 f=105 c=75 fiber=30 | chosen after discussion
"""


def test_parse_rows_is_tolerant() -> None:
    rows, skipped = importer.parse_rows(
        "meal | 2026-08-14 | x\n\n# comment\nbogus | 1\nNOTE: | rule | be strict\n"
    )
    assert [r.kind for r in rows] == ["meal", "note"]
    assert skipped == ["line 4: unknown row kind 'bogus'"]
    assert importer.parse_kv("kcal=780 p=38 c=85 f=30,5") == {
        "kcal": 780,
        "p": 38,
        "c": 85,
        "f": 30.5,
    }
    macros = importer.macros_from_kv({"p": 10, "c": 20, "f": 5})
    assert macros is not None and macros.kcal == 165  # 4/4/9 when kcal is absent
    assert importer.macros_from_kv({"strain": 9}) is None
    assert importer.parse_time("25:00") is None and importer.parse_date("2026-13-01") is None


def test_parse_meal_shapes() -> None:
    rows, _ = importer.parse_rows(SAMPLE)
    meals = [importer.parse_meal(r) for r in rows if r.kind == "meal"]
    assert meals[0].slot == "lunch" and meals[0].at is not None and meals[0].at.hour == 13
    assert len(meals[0].items) == 1 and meals[0].items[0].countable is False
    assert meals[0].items[0].macros.kcal == 780
    # several items with one macros group → one item with the joined name
    assert len(meals[1].items) == 1 and "Greek yogurt" in meals[1].items[0].name
    # per-item numbers → separate items, no time
    assert meals[2].at is None and [i.name for i in meals[2].items] == ["eggs", "toast"]
    assert meals[2].items[1].macros.carbs_g == 15


async def _onboarding_user(session: AsyncSession) -> User:
    user, _ = await repo.get_or_create_user(
        session,
        telegram_id=777,
        chat_id=777,
        now=NOW,
        timezone="Asia/Dubai",
        status=UserStatus.onboarding,
    )
    return user


async def test_import_history_writes_everything_with_source_imported(
    session: AsyncSession,
) -> None:
    user = await _onboarding_user(session)
    result = await importer.import_history(session, user, SAMPLE, now=NOW)
    assert result.counts == {
        "meals": 3,
        "workouts": 1,
        "sleep": 2,
        "measurements": 2,
        "labs": 1,
        "notes": 3,
        "protocol": 1,
    }
    assert result.skipped == [] and result.duplicates == 0

    meals = await repo.list_meals_range(session, user.id, date(2026, 8, 14), date(2026, 8, 15))
    assert len(meals) == 3 and all(m.source is MealSource.imported for m in meals)
    ramen = meals[0]
    assert ramen.slot is MealSlot.lunch and ramen.eaten_at is not None
    assert to_local(ramen.eaten_at, "Asia/Dubai").strftime("%H:%M") == "13:20"
    assert ramen.items[0].countable is False and ramen.items[0].source.value == "user"

    workouts = await repo.list_workouts_range(
        session, user.id, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
    )
    assert len(workouts) == 1 and workouts[0].source is DataSource.other
    assert workouts[0].duration_min == 62 and workouts[0].avg_hr == 118
    assert workouts[0].external_id is not None and workouts[0].external_id.startswith("import:")

    sleeps = await repo.list_sleep_range(
        session, user.id, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
    )
    assert len(sleeps) == 2
    late = next(s for s in sleeps if s.asleep_min == 400)
    assert to_local(late.started_at, "Asia/Dubai").date() == date(
        2026, 8, 14
    )  # 23:30 the night before

    waist = await repo.latest_by_type(session, user.id, "waist")
    assert waist is not None and waist.value == 103 and waist.source == "imported"
    labs = await repo.list_labs(session, user.id)
    assert len(labs) == 1 and labs[0].ref_high == 3.0 and labs[0].flag == "high"
    notes = await repo.list_active_notes(session, user.id)
    assert {n.kind for n in notes} == {NoteKind.preference, NoteKind.pattern, NoteKind.health}

    protocol = await repo.get_active_protocol(session, user.id)
    assert protocol is not None and protocol.kcal == 2000 and protocol.protein_g == 210
    assert protocol.rationale is not None and protocol.rationale.startswith("imported 2026-08-01")


async def test_import_history_is_idempotent_and_keeps_existing_protocol(
    session: AsyncSession,
) -> None:
    user = await _onboarding_user(session)
    await repo.set_active_protocol(
        session,
        user.id,
        kcal=1800,
        protein_g=150,
        fat_g=80,
        carbs_g=120,
        fiber_g=25,
        rationale="mine",
        now=NOW,
    )
    first = await importer.import_history(session, user, SAMPLE, now=NOW)
    assert first.counts["protocol"] == 0
    assert any("kept the existing active protocol" in s for s in first.skipped)
    second = await importer.import_history(session, user, SAMPLE, now=NOW)
    assert second.total == 0
    assert second.duplicates == 3 + 1 + 2 + 2 + 1 + 3
    protocol = await repo.get_active_protocol(session, user.id)
    assert protocol is not None and protocol.kcal == 1800


async def test_import_history_reports_bad_rows(session: AsyncSession) -> None:
    user = await _onboarding_user(session)
    text = "\n".join(
        [
            "meal | 2026-08-14 | lunch | ramen",  # no numbers
            "meal | lunch | ramen | kcal=700",  # no date
            "sleep | 2026-08-14 | 00:40 | asleep=390",  # one time only
            "measurement | 2026-08-18 | 103 | cm",  # no type
            "lab | 2026-06-02 | LDL",  # no value
            "protocol | 2026-08-01 | rationale only",
            "note | just a preference without a kind",
        ]
    )
    result = await importer.import_history(session, user, text, now=NOW)
    assert result.counts["notes"] == 1 and result.total == 1
    assert len(result.skipped) == 6
    assert any("without numbers" in s for s in result.skipped)
    assert any("no ISO date" in s for s in result.skipped)
    assert any("start and end" in s for s in result.skipped)
    result_dict = result.as_dict()
    assert result_dict["total"] == 1 and result_dict["skipped_total"] == 6
