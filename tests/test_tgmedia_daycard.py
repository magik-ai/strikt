"""telegram/daycard: one pinned card per day, edited in place, re-posted when gone or on /today."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock
from strikt.core.types import DayState, DayTotals, Macros, Remaining
from strikt.db import repo
from strikt.db.models import User
from strikt.telegram.daycard import DayCard
from strikt.telegram.messenger import FakeMessenger

TARGETS = Macros(kcal=2000, protein_g=210, carbs_g=75, fat_g=105, fiber_g=30)
TODAY = date(2026, 9, 3)  # conftest NOW is 12:00 in Asia/Dubai


def _state(kcal: float = 0, *, day: date = TODAY, closed: bool = False) -> DayState:
    totals = Macros(kcal=kcal, protein_g=kcal / 10, carbs_g=kcal / 40, fat_g=kcal / 30, fiber_g=2)
    return DayState(
        date=day,
        totals=DayTotals(macros=totals, items=1 if kcal else 0, meals=1 if kcal else 0),
        targets=TARGETS,
        remaining=Remaining.from_targets(TARGETS, totals),
        closed=closed,
    )


async def _card_id(session: AsyncSession, user: User, day: date = TODAY) -> int | None:
    row = await repo.get_day(session, user.id, day)
    return row.card_message_id if row else None


async def test_first_refresh_sends_pins_and_stores(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    message_id = await card.refresh(session, user, _state())
    assert message_id is not None
    assert [m.message_id for m in messenger.sent] == [message_id]
    assert messenger.sent[0].silent is True
    assert messenger.sent[0].keyboard is None  # the pinned card is a readout, not a panel
    assert messenger.pins == [(user.chat_id, message_id)]
    assert await _card_id(session, user) == message_id
    assert "Сегодня" in messenger.sent[0].text


async def test_second_refresh_edits_same_message(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    first = await card.refresh(session, user, _state())
    second = await card.refresh(session, user, _state(620))
    assert second == first
    assert len(messenger.sent) == 1
    assert len(messenger.pins) == 1
    assert [e[1] for e in messenger.edits] == [first]
    assert "620" in messenger.edits[0][2]


async def test_unchanged_state_skips_the_edit(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    first = await card.refresh(session, user, _state(300))
    again = await card.refresh(session, user, _state(300))
    assert again == first
    assert messenger.edits == []
    assert len(messenger.sent) == 1


async def test_gone_message_is_reposted(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    first = await card.refresh(session, user, _state())
    assert first is not None
    await messenger.delete(user.chat_id, first)  # the user removed the pinned card
    second = await card.refresh(session, user, _state(500))
    assert second is not None
    assert second != first
    assert [m.message_id for m in messenger.sent] == [first, second]
    assert messenger.pins[-1] == (user.chat_id, second)
    assert (user.chat_id, first) in messenger.unpins
    assert await _card_id(session, user) == second


async def test_unknown_history_after_restart_does_not_repost_on_first_false(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    before = DayCard(messenger, clock)
    first = await before.refresh(session, user, _state(100))
    assert first is not None
    after_restart = DayCard(messenger, clock)  # empty local cache
    same = await after_restart.refresh(session, user, _state(100))  # Telegram: not modified
    assert same == first
    assert len(messenger.sent) == 1
    changed = await after_restart.refresh(session, user, _state(900))
    assert changed == first
    assert [e[1] for e in messenger.edits] == [first]


async def test_day_change_sends_a_new_card_and_unpins_yesterday(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    old = await card.refresh(session, user, _state(1500))
    clock.advance(timedelta(days=1))
    tomorrow = TODAY + timedelta(days=1)
    new = await card.refresh(session, user, _state(day=tomorrow))
    assert new is not None
    assert old is not None
    assert new != old
    assert len(messenger.sent) == 2
    assert messenger.pins[-1] == (user.chat_id, new)
    assert (user.chat_id, old) in messenger.unpins
    assert await _card_id(session, user, tomorrow) == new
    assert await _card_id(session, user, TODAY) == old  # yesterday keeps its own card


async def test_past_day_without_card_is_not_posted_but_existing_is_edited(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    yesterday = TODAY - timedelta(days=1)
    assert await card.refresh(session, user, _state(day=yesterday)) is None
    assert messenger.sent == []

    old = await card.refresh(session, user, _state(200))
    clock.advance(timedelta(days=1))
    edited = await card.refresh(session, user, _state(700))  # correcting yesterday's log
    assert edited == old
    assert len(messenger.sent) == 1
    assert [e[1] for e in messenger.edits] == [old]


async def test_repost_sends_new_pins_and_unpins_old(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    first = await card.refresh(session, user, _state(400))
    second = await card.repost(session, user, _state(400))
    assert first is not None
    assert second != first
    assert [m.message_id for m in messenger.sent] == [first, second]
    assert (user.chat_id, first) in messenger.unpins
    assert messenger.pins[-1] == (user.chat_id, second)
    assert await _card_id(session, user) == second
    # later refreshes edit the re-posted card
    third = await card.refresh(session, user, _state(800))
    assert third == second
    assert messenger.edits[-1][1] == second


async def test_repost_without_existing_card(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    message_id = await card.repost(session, user, _state())
    assert messenger.unpins == []
    assert messenger.pins == [(user.chat_id, message_id)]


async def test_close_appends_verdict_and_drops_buttons(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    first = await card.refresh(session, user, _state(1900))
    closed = await card.close(session, user, _state(1900), "Best structure this month.")
    assert closed == first
    text = messenger.edits[-1][2]
    assert "закрыт" in text
    assert "Вердикт" in text
    assert "Best structure this month." in text


async def test_without_actions_no_keyboard(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    card = DayCard(messenger, clock)
    await card.refresh(session, user, _state())
    assert messenger.sent[0].keyboard is None


async def test_a_late_bedtime_still_gets_its_card_after_midnight(
    session: AsyncSession, user: User, messenger: FakeMessenger, clock: FakeClock
) -> None:
    """With a 03:30 bedtime the day rolls over at 04:30, so 04:05 is still the evening before.
    Asking without the profile calls it a new day and the card for the real one is never posted.
    """
    await repo.upsert_profile(
        session, user.id, {"bed_time": time(3, 30), "wake_time": time(11, 0)}, now=clock.now()
    )
    await session.commit()
    clock.set(datetime(2026, 9, 4, 0, 5, tzinfo=UTC))  # 04:05 on the 4th in Dubai

    message_id = await DayCard(messenger, clock).refresh(session, user, _state(day=TODAY))

    assert message_id is not None, "the evening's card, not a skipped past day"
    assert await _card_id(session, user, TODAY) == message_id
