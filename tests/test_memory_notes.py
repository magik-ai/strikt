"""``memory.notes``: dedupe, supersede, retire, ordering, keyword relevance, rendering."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import FakeClock, ensure_utc
from strikt.db import repo
from strikt.db.models import NoteKind, User
from strikt.memory import notes
from strikt.memory.notes import keywords, normalise_text, stem


def test_normalise_and_stem() -> None:
    assert normalise_text("  Не любит  ЧИА-пудинг!  ") == "не любит чиа пудинг"
    assert normalise_text("Ёлка") == "елка"
    assert stem("тренировки") == "тренировк"
    assert stem("skipped") == "skipp"
    assert stem("chia") == "chia"  # too short to strip
    assert keywords("What did I eat with the chia pudding yesterday?") == [
        "chia",
        "pudd",  # crude suffix stripping is fine for overlap matching
        "yesterday",
    ]
    assert keywords("и в на") == []


async def test_add_note_dedupes_and_refreshes(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    first = await notes.add_note(
        session, user, NoteKind.preference, "Dislikes chia pudding.", 0.6, now=clock.now()
    )
    assert first.created and first.note.last_confirmed_at is not None
    clock.advance(timedelta(days=2))
    again = await notes.add_note(
        session,
        user,
        "preference",
        "  dislikes CHIA pudding ",
        0.9,
        now=clock.now(),
        source_turn_id=77,
    )
    assert not again.created and again.note.id == first.note.id
    assert again.note.confidence == 0.9
    assert ensure_utc(again.note.last_confirmed_at) == clock.now()
    assert again.note.source_turn_id == 77
    active = await notes.active_notes(session, user, now=clock.now())
    assert len(active) == 1


async def test_notes_api_defaults_to_wall_clock(session: AsyncSession, user: User) -> None:
    """PLAN §6.4 handlers have no clock: ``now`` may be omitted everywhere."""
    written = await notes.add_note(session, user, NoteKind.commitment, "lunch by 14:00", 0.9)
    assert written.created and written.note.last_confirmed_at is not None
    assert [n.id for n in await notes.active_notes(session, user)] == [written.note.id]
    assert [n.id for n in await notes.relevant_notes(session, user, "lunch today?")] == [
        written.note.id
    ]


async def test_add_note_different_kind_is_not_a_duplicate(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    await notes.add_note(session, user, NoteKind.preference, "no dairy", 0.8, now=clock.now())
    other = await notes.add_note(session, user, NoteKind.rule, "no dairy", 0.8, now=clock.now())
    assert other.created
    assert len(await notes.active_notes(session, user, now=clock.now())) == 2


async def test_supersede_retires_old_and_links(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    old = await notes.add_note(
        session, user, NoteKind.health, "protein target 150 g", 0.8, now=clock.now()
    )
    new = await notes.add_note(
        session,
        user,
        NoteKind.health,
        "protein target 210 g",
        0.9,
        now=clock.now(),
        supersedes_id=old.note.id,
    )
    assert new.created and new.superseded_id == old.note.id
    await session.refresh(old.note)
    old_row = await repo.get_note(session, user.id, old.note.id)
    assert old_row is not None and not old_row.active and old_row.superseded_by == new.note.id
    active = await notes.active_notes(session, user, now=clock.now())
    assert [n.id for n in active] == [new.note.id]


async def test_supersede_into_existing_duplicate(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    keep = await notes.add_note(
        session, user, NoteKind.rule, "no alcohol on weekdays", 0.8, now=clock.now()
    )
    old = await notes.add_note(
        session, user, NoteKind.rule, "no alcohol at all", 0.8, now=clock.now()
    )
    result = await notes.add_note(
        session,
        user,
        NoteKind.rule,
        "No alcohol on weekdays",
        0.8,
        now=clock.now(),
        supersedes_id=old.note.id,
    )
    assert not result.created and result.note.id == keep.note.id
    assert result.superseded_id == old.note.id
    await session.refresh(old.note)
    old_row = await repo.get_note(session, user.id, old.note.id)
    assert old_row is not None and not old_row.active and old_row.superseded_by == keep.note.id


async def test_supersede_missing_falls_back_to_insert(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    result = await notes.add_note(
        session, user, NoteKind.event, "flight Friday", 0.8, now=clock.now(), supersedes_id=999
    )
    assert result.created and result.superseded_id is None


async def test_add_note_rejects_empty_and_clamps(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    with pytest.raises(ValueError, match="empty"):
        await notes.add_note(session, user, NoteKind.answer, "   ", 0.8, now=clock.now())
    result = await notes.add_note(session, user, NoteKind.answer, "x" * 600, 5.0, now=clock.now())
    assert result.note.confidence == 1.0 and len(result.note.text) <= notes.MAX_NOTE_CHARS


async def test_retire_and_expiry(session: AsyncSession, user: User, clock: FakeClock) -> None:
    trip = await notes.add_note(
        session,
        user,
        NoteKind.event,
        "travelling until Sunday",
        0.9,
        now=clock.now(),
        expires_at=clock.now() + timedelta(days=3),
    )
    keep = await notes.add_note(
        session, user, NoteKind.pattern, "skips lunch → 2600 evening", 0.7, now=clock.now()
    )
    assert len(await notes.active_notes(session, user, now=clock.now())) == 2
    clock.advance(timedelta(days=4))
    assert [n.id for n in await notes.active_notes(session, user, now=clock.now())] == [
        keep.note.id
    ]
    assert await notes.retire(session, user, keep.note.id)
    assert not await notes.retire(session, user, keep.note.id)
    assert not await notes.retire(session, user, trip.note.id + 100)
    assert await notes.active_notes(session, user, now=clock.now()) == []


async def test_active_notes_ordering_kinds_and_limit(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    a = await notes.add_note(session, user, NoteKind.rule, "rule old", 0.8, now=clock.now())
    clock.advance(timedelta(hours=1))
    b = await notes.add_note(session, user, NoteKind.rule, "rule new", 0.8, now=clock.now())
    c = await notes.add_note(session, user, NoteKind.pattern, "pattern", 0.8, now=clock.now())
    ordered = await notes.active_notes(session, user, now=clock.now())
    assert [n.id for n in ordered] == [c.note.id, b.note.id, a.note.id]  # kind asc, recency desc
    only_rules = await notes.active_notes(session, user, now=clock.now(), kinds=["rule"], limit=1)
    assert [n.id for n in only_rules] == [b.note.id]


async def test_relevant_notes_keyword_overlap(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    chia = await notes.add_note(
        session,
        user,
        NoteKind.preference,
        "Не любит чиа-пудинг, ест только ради клетчатки",
        0.9,
        now=clock.now(),
    )
    fat = await notes.add_note(
        session, user, NoteKind.pattern, "Голод при жирах ниже 70 г", 0.8, now=clock.now()
    )
    await notes.add_note(
        session,
        user,
        NoteKind.health,
        "Липидный профиль: избегать кокосового масла",
        0.9,
        now=clock.now(),
    )
    hits = await notes.relevant_notes(
        session, user, "Опять голоден. Жиры сегодня 55", now=clock.now()
    )
    assert [n.id for n in hits] == [fat.note.id]
    hits = await notes.relevant_notes(
        session, user, "chia pudding? клетчатка нужна", now=clock.now()
    )
    assert [n.id for n in hits] == [chia.note.id]
    assert await notes.relevant_notes(session, user, "и в на", now=clock.now()) == []
    assert await notes.relevant_notes(session, user, "ramen", now=clock.now()) == []


async def test_render_notes_block_is_deterministic(
    session: AsyncSession, user: User, clock: FakeClock
) -> None:
    assert notes.render_notes_block([]) == ""
    b = await notes.add_note(
        session, user, NoteKind.preference, "likes brussels sprouts", 0.9, now=clock.now()
    )
    a = await notes.add_note(
        session,
        user,
        NoteKind.pattern,
        "one meal until 19:00 → 1100 kcal dinner",
        0.5,
        now=clock.now(),
    )
    trip = await notes.add_note(
        session,
        user,
        NoteKind.event,
        "trip to Riyadh",
        0.9,
        now=clock.now(),
        expires_at=clock.now() + timedelta(days=2),
    )
    rows = await notes.active_notes(session, user, now=clock.now())
    text = notes.render_notes_block(rows)
    assert text == notes.render_notes_block(list(reversed(rows)))
    assert text.splitlines() == [
        f"- [event] #{trip.note.id} trip to Riyadh (until 2026-09-05)",
        f"- [pattern] #{a.note.id} one meal until 19:00 → 1100 kcal dinner (conf 0.5)",
        f"- [preference] #{b.note.id} likes brussels sprouts",
    ]
    assert "2026-09-03T" not in text  # no timestamps
