"""One smoke test per repository group."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock, ensure_utc
from strikt.core.types import FoodItemIn, LabMarker, Macros
from strikt.db import repo
from strikt.db.crypto import TokenCipher
from strikt.db.models import (
    DataSource,
    DayFlag,
    ItemSource,
    MealSlot,
    MealSource,
    MeasurementType,
    NoteKind,
    Provider,
    SummaryKind,
    TurnRole,
    UsagePurpose,
    User,
    UserStatus,
)


def _item(name: str, kcal: float, p: float, c: float, f: float, **kw: object) -> FoodItemIn:
    return FoodItemIn(
        name=name, macros=Macros(kcal=kcal, protein_g=p, carbs_g=c, fat_g=f, fiber_g=2), **kw
    )  # type: ignore[arg-type]


async def test_users_get_or_create(session: AsyncSession, clock: FakeClock) -> None:
    user, created = await repo.get_or_create_user(
        session, telegram_id=42, chat_id=42, now=clock.now(), language="ru"
    )
    assert created and user.id and user.status == UserStatus.onboarding
    again, created2 = await repo.get_or_create_user(
        session, telegram_id=42, chat_id=43, now=clock.now()
    )
    assert not created2 and again.id == user.id and again.chat_id == 43
    await repo.set_user_status(session, user.id, UserStatus.active)
    await repo.set_user_locale(session, user.id, timezone="Asia/Dubai")
    session.expire_all()
    reloaded = await repo.get_user_by_telegram_id(session, 42)
    assert reloaded and reloaded.status == UserStatus.active and reloaded.timezone == "Asia/Dubai"
    assert [u.id for u in await repo.list_users(session, statuses=[UserStatus.active])] == [user.id]


async def test_profile_upsert_and_unknown_field(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    profile = await repo.upsert_profile(session, user.id, {"city": "Abu Dhabi"}, now=clock.now())
    assert profile.city == "Abu Dhabi" and profile.name == "Test"
    with pytest.raises(ValueError, match="unknown profile fields"):
        await repo.upsert_profile(session, user.id, {"nope": 1}, now=clock.now())


async def test_protocol_versions_single_active(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    second = await repo.set_active_protocol(
        session,
        user.id,
        kcal=2100,
        protein_g=200,
        fat_g=100,
        carbs_g=100,
        fiber_g=30,
        rationale="more carbs",
        now=clock.now(),
    )
    assert second.version == 2
    active = await repo.get_active_protocol(session, user.id)
    assert active is not None and active.id == second.id
    protocols = await repo.list_protocols(session, user.id)
    assert [p.active for p in protocols] == [False, True]
    assert repo.protocol_targets(active).kcal == 2100


async def test_days_open_close_flag_plan_card(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    today = date(2026, 9, 3)
    day = await repo.get_or_open_day(session, user.id, today, now=clock.now())
    assert day.flags == [] and day.closed_at is None
    await repo.set_day_flag(session, user.id, today, DayFlag.salty, True, now=clock.now())
    await repo.set_day_flag(session, user.id, today, "alcohol", True, now=clock.now())
    await repo.set_day_flag(session, user.id, today, DayFlag.salty, False, now=clock.now())
    await repo.set_day_plan(session, user.id, today, {"lunch": "ramen 13:00"}, now=clock.now())
    await repo.set_card_message(session, user.id, today, 555, now=clock.now())
    closed = await repo.close_day(
        session, user.id, today, verdict="Closed at 1910", now=clock.now()
    )
    assert closed.flags == ["alcohol"]
    assert closed.plan == {"lunch": "ramen 13:00"}
    assert closed.card_message_id == 555 and closed.closed_at is not None
    assert len(await repo.list_days_range(session, user.id, today, today)) == 1


async def test_meals_add_update_delete_list(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    today = date(2026, 9, 3)
    meal = await repo.add_meal_with_items(
        session,
        user.id,
        day_date=today,
        items=[_item("eggs", 140, 12, 1, 10), _item("toast", 160, 5, 30, 2, countable=True)],
        slot=MealSlot.breakfast,
        source=MealSource.photo,
        logged_at=clock.now(),
        item_flags={0: ["kcal_mismatch"]},
    )
    assert meal.id and [i.position for i in meal.items] == [0, 1]
    assert meal.items[0].flags == ["kcal_mismatch"]
    assert repo.meal_macros(meal).kcal == 300

    item = await repo.update_meal_item(
        session,
        user.id,
        meal.items[1].id,
        {"kcal": 80, "carbs_g": 15},
        user_correction={"portion": "half"},
    )
    assert item and item.kcal == 80 and item.user_correction == {"portion": "half"}
    with pytest.raises(ValueError, match="unknown meal item fields"):
        await repo.update_meal_item(session, user.id, item.id, {"bogus": 1})

    await repo.update_meal(session, user.id, meal.id, slot="lunch", note="late")
    listed = await repo.list_meals_for_date(session, user.id, today)
    assert len(listed) == 1 and listed[0].slot == MealSlot.lunch and listed[0].note == "late"
    assert (await repo.last_meal(session, user.id)) is not None
    assert [m.name for m in await repo.search_meal_items(session, user.id, "TOAST")] == ["toast"]

    assert await repo.soft_delete_meal(session, user.id, meal.id, now=clock.now())
    assert await repo.list_meals_for_date(session, user.id, today) == []
    assert await repo.last_meal(session, user.id) is None
    assert await repo.restore_meal(session, user.id, meal.id)
    assert len(await repo.list_meals_range(session, user.id, today, today)) == 1


async def test_foods_key_and_upsert(session: AsyncSession, clock: FakeClock) -> None:
    key = repo.make_food_key("  Greek Yogurt, 0% ", "Fage", None)
    assert key == "greek yogurt 0|fage|"
    food = await repo.upsert_food(
        session,
        name="Greek Yogurt, 0%",
        brand="Fage",
        per_100g=Macros(kcal=57, protein_g=10, carbs_g=4, fat_g=0),
        source=ItemSource.label,
        fetched_at=clock.now(),
        barcode="123",
    )
    again = await repo.upsert_food(
        session,
        name="Greek Yogurt, 0%",
        brand="Fage",
        per_100g=Macros(kcal=59, protein_g=10, carbs_g=4, fat_g=0),
        source="off",
        fetched_at=clock.now(),
        barcode="123",
    )
    assert again.id == food.id and again.per_100g["kcal"] == 59 and again.source == ItemSource.off
    assert (await repo.get_food_by_key(session, key)) is not None
    assert (await repo.get_food_by_barcode(session, "123")) is not None


async def test_workouts_upsert_last_same_sport_avg(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    first, created = await repo.upsert_workout_by_external(
        session,
        user.id,
        source=DataSource.whoop,
        external_id="w1",
        sport="run",
        started_at=now - timedelta(days=3),
        now=now,
        duration_min=45,
        strain=12.0,
        kcal=406,
        avg_hr=130,
    )
    assert created
    _, created_again = await repo.upsert_workout_by_external(
        session,
        user.id,
        source="whoop",
        external_id="w1",
        sport="run",
        started_at=now - timedelta(days=3),
        now=now,
        duration_min=46,
    )
    assert not created_again
    second, _ = await repo.upsert_workout_by_external(
        session,
        user.id,
        source=DataSource.screenshot,
        external_id=None,
        sport="Run",
        started_at=now - timedelta(hours=2),
        now=now,
        duration_min=94,
        strain=8.0,
        kcal=361,
        avg_hr=104,
    )
    prev = await repo.last_same_sport(
        session, user.id, "run", before=second.started_at, exclude_id=second.id
    )
    assert prev is not None and prev.id == first.id
    avg = await repo.avg_30d(session, user.id, now=now, sport="run")
    assert avg.count == 2 and avg.duration_min == pytest.approx(70)
    listed = await repo.list_workouts_range(session, user.id, now - timedelta(days=7), now)
    assert [w.id for w in listed] == [first.id, second.id]


async def test_sleep_and_recoveries(session: AsyncSession, user: User, clock: FakeClock) -> None:
    now = clock.now()
    _sleep, created = await repo.upsert_sleep_by_external(
        session,
        user.id,
        source=DataSource.whoop,
        external_id="s1",
        started_at=now - timedelta(hours=9),
        ended_at=now - timedelta(hours=1),
        now=now,
        asleep_min=420,
        performance_pct=78,
    )
    assert created and (await repo.last_sleep(session, user.id)) is not None
    assert len(await repo.list_sleep_range(session, user.id, now - timedelta(days=1), now)) == 1
    rec, created = await repo.upsert_recovery_by_external(
        session,
        user.id,
        source="whoop",
        external_id="r1",
        day=date(2026, 9, 3),
        score=61,
        hrv_ms=44,
    )
    assert created
    rec2, created2 = await repo.upsert_recovery_by_external(
        session, user.id, source="whoop", external_id="r1", day=date(2026, 9, 3), score=65
    )
    assert not created2 and rec2.id == rec.id and rec2.score == 65
    assert (await repo.recovery_for_date(session, user.id, date(2026, 9, 3))) is not None
    assert (
        len(await repo.list_recoveries_range(session, user.id, date(2026, 9, 1), date(2026, 9, 3)))
        == 1
    )


async def test_measurements_latest_and_days_since(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    await repo.add_measurement(
        session,
        user.id,
        type=MeasurementType.waist,
        value=105,
        unit="cm",
        measured_at=now - timedelta(days=30),
    )
    await repo.add_measurement(
        session, user.id, type="waist", value=103, unit="cm", measured_at=now - timedelta(days=16)
    )
    latest = await repo.latest_by_type(session, user.id, "waist")
    assert latest is not None and latest.value == 103
    assert await repo.days_since_last(session, user.id, "waist", now=now) == 16
    assert await repo.days_since_last(session, user.id, "weight", now=now) is None
    assert (
        await repo.average_by_type(session, user.id, "waist", since=now - timedelta(days=40)) == 104
    )
    rows = await repo.list_measurements_range(
        session, user.id, now - timedelta(days=20), now, type="waist"
    )
    assert len(rows) == 1


async def test_labs(session: AsyncSession, user: User) -> None:
    rows = await repo.add_labs(
        session,
        user.id,
        taken_at=date(2026, 6, 2),
        markers=[LabMarker(marker="LDL", value=3.9, unit="mmol/L", ref_high=3.0, flag="high")],
        source="photo",
    )
    assert len(rows) == 1
    assert [r.marker for r in await repo.list_labs(session, user.id, marker="ldl")] == ["LDL"]


async def test_notes_supersede_retire_search(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    note = await repo.add_note(
        session, user.id, kind=NoteKind.preference, text="dislikes chia", confidence=0.9, now=now
    )
    temp = await repo.add_note(
        session,
        user.id,
        kind="event",
        text="trip to Riga",
        confidence=1,
        now=now,
        expires_at=now + timedelta(days=1),
    )
    assert len(await repo.list_active_notes(session, user.id, now=now)) == 2
    assert len(await repo.list_active_notes(session, user.id, now=now + timedelta(days=2))) == 1
    new = await repo.supersede_note(
        session, user.id, note.id, text="dislikes chia pudding", confidence=0.95, now=now
    )
    assert new is not None and new.id != note.id
    old = await repo.get_note(session, user.id, note.id)
    assert old is not None and old.active is False and old.superseded_by == new.id
    assert await repo.retire_note(session, user.id, temp.id)
    assert not await repo.retire_note(session, user.id, temp.id)
    assert [n.id for n in await repo.list_active_notes(session, user.id)] == [new.id]
    assert next(n.id for n in await repo.search_notes(session, user.id, "chia")) == new.id
    assert await repo.confirm_note(session, user.id, new.id, now=now)


async def test_reminders(session: AsyncSession, user: User, clock: FakeClock) -> None:
    now = clock.now()
    r1 = await repo.add_reminder(
        session, user.id, due_at=now + timedelta(hours=1), text="waist", now=now, kind="measurement"
    )
    r2 = await repo.add_reminder(
        session, user.id, due_at=now + timedelta(hours=5), text="bed", now=now
    )
    assert [r.id for r in await repo.pending_reminders(session, user.id)] == [r1.id, r2.id]
    assert [
        r.id for r in await repo.pending_reminders(session, due_before=now + timedelta(hours=2))
    ] == [r1.id]
    assert await repo.cancel_reminder(session, user.id, r2.id)
    assert await repo.mark_reminder_sent(session, user.id, r1.id)
    assert await repo.pending_reminders(session, user.id) == []


async def test_turns_stub_images_and_search(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}},
        {"type": "text", "text": "Ramen at Kinoya"},
    ]
    t1 = await repo.add_turn(
        session, user.id, role=TurnRole.user, content=content, now=now, telegram_message_id=7
    )
    assert t1.text == "Ramen at Kinoya"
    t2 = await repo.add_turn(
        session,
        user.id,
        role="assistant",
        content=[{"type": "text", "text": "780 kcal"}],
        now=now + timedelta(seconds=5),
        output_tokens=12,
    )
    turns = await repo.last_n_turns(session, user.id, 5)
    assert [t.id for t in turns] == [t1.id, t2.id]
    stubbed = await repo.mark_stub_images(session, user.id, t1.id)
    assert stubbed is not None and stubbed.content[0]["type"] == "text"
    assert stubbed.content[0]["text"].startswith("[image: ")
    hits = await repo.search_turns(session, user.id, "kinoya")
    assert [h.id for h in hits] == [t1.id]


async def test_summaries_upsert_and_recent(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    for offset in range(3):
        day = date(2026, 9, 1) + timedelta(days=offset)
        await repo.upsert_summary(
            session,
            user.id,
            kind=SummaryKind.day,
            period_start=day,
            period_end=day,
            text=f"day {day}",
            data={"kcal": 1900 + offset},
            now=now,
        )
    updated = await repo.upsert_summary(
        session,
        user.id,
        kind="day",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 1),
        text="rewritten",
        data=None,
        now=now,
    )
    assert updated.text == "rewritten"
    recent = await repo.list_recent_summaries(session, user.id, SummaryKind.day, limit=2)
    assert [s.period_start for s in recent] == [date(2026, 9, 3), date(2026, 9, 2)]
    assert (await repo.get_summary(session, user.id, "day", date(2026, 9, 1))) is not None
    assert len(await repo.search_summaries(session, user.id, "rewritten")) == 1


async def test_integrations_tokens_encrypted(
    session: AsyncSession, user: User, clock: FakeClock, cipher: TokenCipher
) -> None:
    now = clock.now()
    token = repo.generate_webhook_token()
    await repo.upsert_integration(session, user.id, Provider.whoop, now=now, webhook_token=token)
    row = await repo.set_integration_tokens(
        session,
        cipher,
        user.id,
        "whoop",
        access_token="acc",
        refresh_token="ref",
        expires_at=now + timedelta(hours=1),
        now=now,
        external_user_id="u-9",
    )
    assert row.access_token_enc and "acc" not in row.access_token_enc
    tokens = repo.integration_tokens(cipher, row)
    assert (tokens.access_token, tokens.refresh_token) == ("acc", "ref")
    assert (await repo.integration_by_webhook_token(session, token)) is not None
    assert (await repo.integration_by_external_user(session, "whoop", "u-9")) is not None
    assert await repo.set_integration_status(session, user.id, "whoop", "expired", last_sync_at=now)
    assert len(await repo.list_integrations(session, provider="whoop")) == 1
    assert await repo.clear_integration(session, user.id, Provider.whoop)
    assert (await repo.get_integration(session, user.id, Provider.whoop)) is None


async def test_proactive_sends_ladder_and_response_rate(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_first_meal",
        window_key="2026-09-03:no_first_meal",
        step=1,
        sent_at=now - timedelta(hours=2),
        text="Nothing logged.",
    )
    last = await repo.add_proactive_send(
        session,
        user.id,
        trigger="no_first_meal",
        window_key="2026-09-03:no_first_meal",
        step=2,
        sent_at=now - timedelta(hours=1),
        text="Two hours past.",
    )
    found = await repo.last_send_for_window(session, user.id, "2026-09-03:no_first_meal")
    assert found is not None and found.id == last.id and found.step == 2
    assert await repo.count_sends_today(session, user.id, since=now - timedelta(hours=3)) == 2
    assert await repo.response_rate(session, user.id) == 0.0
    assert await repo.mark_responded(session, user.id, at=now, turn_id=1) == 2
    assert await repo.response_rate(session, user.id, trigger="no_first_meal") == 1.0
    assert await repo.response_rate(session, user.id, trigger="nope") is None
    assert len(await repo.list_sends_since(session, user.id, since=now - timedelta(days=1))) == 2


async def test_token_usage_aggregates(session: AsyncSession, user: User) -> None:
    day = date(2026, 9, 3)
    for _ in range(2):
        await repo.add_usage(
            session,
            user.id,
            day=day,
            model="claude-sonnet-5",
            purpose=UsagePurpose.turn,
            input_tokens=100,
            cache_read_tokens=1000,
            cache_write_tokens=0,
            output_tokens=50,
            cost_usd=0.001,
        )
    await repo.add_usage(
        session,
        user.id,
        day=day,
        model="claude-sonnet-5",
        purpose="verify",
        input_tokens=10,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=5,
        cost_usd=0.0001,
    )
    rows = await repo.usage_for_date(session, user.id, day)
    assert len(rows) == 2
    turn = next(r for r in rows if r.purpose == UsagePurpose.turn)
    assert turn.calls == 2 and turn.input_tokens == 200 and turn.cost_usd == pytest.approx(0.002)
    totals = await repo.usage_totals(session, user.id, day, day)
    assert totals.calls == 3 and totals.output_tokens == 105


async def test_invites(session: AsyncSession, user: User, clock: FakeClock) -> None:
    now = clock.now()
    invite = await repo.create_invite(session, now=now, created_by=user.id)
    assert len(invite.code) >= 8
    assert (await repo.consume_invite(session, invite.code, used_by=user.id, now=now)) is not None
    assert (await repo.consume_invite(session, invite.code, used_by=user.id, now=now)) is None
    assert (await repo.consume_invite(session, "missing", used_by=user.id, now=now)) is None


async def test_oauth_states_single_use_and_expiry(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    now = clock.now()
    fresh = await repo.create_oauth_state(session, user.id, Provider.whoop, now=now)
    consumed = await repo.consume_oauth_state(session, fresh.state, now=now + timedelta(minutes=5))
    assert consumed is not None and consumed.provider == Provider.whoop
    assert (await repo.consume_oauth_state(session, fresh.state, now=now)) is None
    stale = await repo.create_oauth_state(session, user.id, "withings", now=now)
    assert (
        await repo.consume_oauth_state(session, stale.state, now=now + timedelta(minutes=11))
    ) is None
    old = await repo.create_oauth_state(session, user.id, "withings", now=now - timedelta(hours=1))
    assert await repo.purge_oauth_states(session, now=now) == 1
    assert (await session.get(type(old), old.state)) is None


def test_ensure_utc_handles_naive() -> None:
    naive = datetime(2026, 9, 3, 8, 0)
    assert ensure_utc(naive).tzinfo is UTC
