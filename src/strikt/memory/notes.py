"""Coach notes: write with dedupe/supersede, read active, rank by keyword overlap, render.

research/07 D5: contradictions supersede, duplicates refresh ``last_confirmed_at`` instead of
inserting again. Ranking is a plain keyword overlap with light suffix stripping for Russian
and English (no embeddings at launch, D2). ``render_notes_block`` is deterministic (sorted,
no timestamps) so the cached profile block stays byte-stable between writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc
from strikt.db import repo
from strikt.db.models import Note, NoteKind, User
from strikt.memory import queries

log = structlog.get_logger(__name__)

MAX_NOTE_CHARS = 400
DEFAULT_ACTIVE_LIMIT = 40
DEFAULT_RELEVANT_LIMIT = 8
MIN_TOKEN_LEN = 3

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")

STOPWORDS: frozenset[str] = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "has",
        "had",
        "was",
        "were",
        "are",
        "you",
        "your",
        "our",
        "not",
        "but",
        "all",
        "any",
        "can",
        "did",
        "does",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "how",
        "did",
        "will",
        "would",
        "could",
        "should",
        "about",
        "into",
        "over",
        "than",
        "then",
        "them",
        "they",
        "there",
        "their",
        "been",
        "being",
        "also",
        "just",
        "very",
        "more",
        "most",
        "some",
        "such",
        "only",
        "own",
        "same",
        "too",
        "its",
        "it's",
        "his",
        "her",
        "him",
        "she",
        "he",
        "we",
        "me",
        "my",
        "i",
        "a",
        "an",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "is",
        "be",
        "as",
        "or",
        "if",
        "do",
        "so",
        "no",
        "yes",
        "it",
        "up",
        "out",
        "off",
        # domain question words: every food question contains them, they select nothing
        "eat",
        "ate",
        "eaten",
        "eating",
        "had",
        "have",
        "ел",
        "ела",
        "ели",
        "съел",
        "съела",
        "поел",
        "поела",
        "и",
        "в",
        "во",
        "на",
        "не",
        "что",
        "это",
        "как",
        "так",
        "с",
        "со",
        "по",
        "за",
        "от",
        "до",
        "из",
        "для",
        "при",
        "но",
        "или",
        "же",
        "уже",
        "ещё",
        "еще",
        "бы",
        "ли",
        "да",
        "нет",
        "он",
        "она",
        "оно",
        "они",
        "мы",
        "вы",
        "я",
        "ты",
        "мне",
        "мой",
        "моя",
        "мое",
        "мои",
        "меня",
        "тебя",
        "его",
        "её",
        "ее",
        "их",
        "нас",
        "вас",
        "был",
        "была",
        "было",
        "были",
        "быть",
        "есть",
        "был",
        "у",
        "о",
        "об",
        "про",
        "к",
        "ко",
        "без",
        "над",
        "под",
        "между",
        "через",
        "то",
        "те",
        "та",
        "тот",
        "эта",
        "эти",
        "этот",
        "этого",
        "этой",
        "этом",
        "тут",
        "там",
        "где",
        "когда",
        "куда",
        "почему",
        "зачем",
        "чем",
    ]
)

# Suffixes stripped for matching (longest first). Deliberately crude: a note lookup is a
# hint, not a linguistic analysis.
_RU_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ой",
    "ей",
    "ый",
    "ий",
    "ая",
    "яя",
    "ую",
    "юю",
    "ые",
    "ие",
    "ах",
    "ях",
    "ов",
    "ев",
    "ам",
    "ям",
    "ом",
    "ем",
    "ии",
    "ия",
    "ие",
    "а",
    "я",
    "ы",
    "и",
    "у",
    "ю",
    "о",
    "е",
    "ь",
)
_EN_SUFFIXES = ("ings", "ing", "ies", "ers", "ed", "es", "s")


def normalise_text(text: str) -> str:
    """Casefold, ё→е, strip punctuation, collapse whitespace (the dedupe key)."""
    lowered = text.casefold().replace("ё", "е")
    return _WS.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def stem(word: str) -> str:
    for suffixes in (_RU_SUFFIXES, _EN_SUFFIXES):
        for suffix in suffixes:
            if word.endswith(suffix) and len(word) - len(suffix) >= MIN_TOKEN_LEN:
                return word[: -len(suffix)]
    return word


def keywords(text: str) -> list[str]:
    """Ordered, de-duplicated content stems of ``text`` (stopwords and short tokens dropped)."""
    seen: dict[str, None] = {}
    for token in normalise_text(text).split():
        if len(token) < MIN_TOKEN_LEN or token in STOPWORDS:
            continue
        seen.setdefault(stem(token), None)
    return list(seen)


def overlap(a: Iterable[str], b: Iterable[str]) -> int:
    return len(set(a) & set(b))


def _now(now: datetime | None) -> datetime:
    """Callers with a ``Clock`` pass ``now``; tool handlers without one get wall-clock UTC."""
    return ensure_utc(now) if now is not None else datetime.now(UTC)


# ------------------------------------------------------------------------------------- write


@dataclass(frozen=True)
class NoteWrite:
    """What ``add_note`` did. ``created=False`` means an identical active note was refreshed."""

    note: Note
    created: bool
    superseded_id: int | None = None


async def add_note(
    session: AsyncSession,
    user: User,
    kind: NoteKind | str,
    text: str,
    confidence: float,
    *,
    now: datetime | None = None,
    source_turn_id: int | None = None,
    expires_at: datetime | None = None,
    supersedes_id: int | None = None,
) -> NoteWrite:
    """Insert a note unless an active one with the same kind and normalised text exists.

    On a duplicate: bump ``last_confirmed_at``, raise confidence to the max of both, take a
    new ``expires_at`` when given. ``supersedes_id`` retires that note and links it (repo
    ``supersede_note``); when the new text duplicates another active note the old one is still
    retired and linked to the survivor.
    """
    now = _now(now)
    kind_enum = NoteKind(kind)
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("note text is empty")
    if len(cleaned) > MAX_NOTE_CHARS:
        cleaned = cleaned[: MAX_NOTE_CHARS - 1].rstrip() + "…"
    confidence = max(0.0, min(1.0, confidence))
    key = normalise_text(cleaned)

    existing = await queries.active_notes_of_kinds(session, user.id, now=now, kinds=[kind_enum])
    duplicate = next(
        (n for n in existing if normalise_text(n.text) == key and n.id != supersedes_id), None
    )
    if duplicate is not None:
        duplicate.last_confirmed_at = now
        duplicate.confidence = max(duplicate.confidence, confidence)
        if expires_at is not None:
            duplicate.expires_at = expires_at
        if source_turn_id is not None and duplicate.source_turn_id is None:
            duplicate.source_turn_id = source_turn_id
        superseded: int | None = None
        if supersedes_id is not None and supersedes_id != duplicate.id:
            old = await repo.get_note(session, user.id, supersedes_id)
            if old is not None and old.active:
                old.active = False
                old.superseded_by = duplicate.id
                superseded = old.id
        await session.flush()
        log.info("note_refreshed", user_id=user.id, note_id=duplicate.id, kind=kind_enum.value)
        return NoteWrite(note=duplicate, created=False, superseded_id=superseded)

    if supersedes_id is not None:
        replaced = await repo.supersede_note(
            session,
            user.id,
            supersedes_id,
            text=cleaned,
            confidence=confidence,
            now=now,
            kind=kind_enum,
            expires_at=expires_at,
            source_turn_id=source_turn_id,
        )
        if replaced is not None:
            replaced.last_confirmed_at = now
            await session.flush()
            log.info("note_superseded", user_id=user.id, old_id=supersedes_id, new_id=replaced.id)
            return NoteWrite(note=replaced, created=True, superseded_id=supersedes_id)
        log.warning("note_supersede_missing", user_id=user.id, old_id=supersedes_id)

    note = await repo.add_note(
        session,
        user.id,
        kind=kind_enum,
        text=cleaned,
        confidence=confidence,
        now=now,
        source_turn_id=source_turn_id,
        expires_at=expires_at,
    )
    note.last_confirmed_at = now
    await session.flush()
    log.info("note_added", user_id=user.id, note_id=note.id, kind=kind_enum.value)
    return NoteWrite(note=note, created=True)


async def retire(session: AsyncSession, user: User, note_id: int) -> bool:
    """Deactivate one note. False when it does not exist or is already inactive."""
    done = await repo.retire_note(session, user.id, note_id)
    if done:
        log.info("note_retired", user_id=user.id, note_id=note_id)
    return done


# -------------------------------------------------------------------------------------- read


def _recency(note: Note) -> datetime:
    return ensure_utc(note.last_confirmed_at or note.created_at)


async def active_notes(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    kinds: Iterable[NoteKind | str] | None = None,
    limit: int = DEFAULT_ACTIVE_LIMIT,
) -> list[Note]:
    """Active, unexpired notes ordered by kind then most recently confirmed first."""
    rows = await queries.active_notes_of_kinds(session, user.id, now=_now(now), kinds=kinds)
    rows.sort(key=lambda n: (n.kind.value, -_recency(n).timestamp(), -n.id))
    return rows[:limit]


async def relevant_notes(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_RELEVANT_LIMIT,
) -> list[Note]:
    """Active notes sharing content words with ``text``, best overlap first, then recency."""
    query = keywords(text)
    if not query:
        return []
    rows = await queries.active_notes_of_kinds(session, user.id, now=_now(now))
    scored = [(overlap(query, keywords(n.text)), n) for n in rows]
    hits = [(score, n) for score, n in scored if score > 0]
    hits.sort(key=lambda pair: (-pair[0], -_recency(pair[1]).timestamp(), -pair[1].id))
    return [n for _, n in hits[:limit]]


def render_notes_block(notes: Sequence[Note]) -> str:
    """Deterministic text for the cached profile block: sorted by kind, text, id; no timestamps.

    Ids are included so the model can ``retire_note``/``supersede`` precisely; ``until`` shows
    the expiry date of temporary facts (static per note, so cache-safe).
    """
    if not notes:
        return ""
    ordered = sorted(notes, key=lambda n: (n.kind.value, normalise_text(n.text), n.id))
    lines: list[str] = []
    for note in ordered:
        line = f"- [{note.kind.value}] #{note.id} {' '.join(note.text.split())}"
        if note.confidence < 0.7:
            line += f" (conf {note.confidence:.1f})"
        if note.expires_at is not None:
            line += f" (until {ensure_utc(note.expires_at).date().isoformat()})"
        lines.append(line)
    return "\n".join(lines)
