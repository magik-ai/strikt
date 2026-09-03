"""History retrieval for ``get_history`` / ``search_history`` and the ``<context>`` block.

Two paths (research/07 D2): typed rows by kind and date range (SQL, exact), and keyword
search over turns, notes, summaries and food names (FTS on Postgres, LIKE on SQLite).
``search_history`` first resolves a temporal expression in the text ("last Tuesday",
"на прошлой неделе") and only then falls back to keywords. ``render_rows`` fits the result in
a token budget (≈ 4 chars/token) and says how many rows were cut.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.clock import ensure_utc, local_day_bounds, to_local
from strikt.db import repo
from strikt.db.models import (
    ConversationTurn,
    Day,
    Lab,
    Meal,
    Measurement,
    Note,
    Recovery,
    Sleep,
    Summary,
    User,
    Workout,
)
from strikt.memory import queries
from strikt.memory.notes import keywords
from strikt.memory.periods import find_period, strip_period
from strikt.telegram.copy import resolve_lang

log = structlog.get_logger(__name__)

HistoryKind = Literal[
    "meals",
    "workouts",
    "sleep",
    "recoveries",
    "measurements",
    "labs",
    "notes",
    "summaries",
    "days",
    "turns",
]
ALL_HISTORY_KINDS: tuple[HistoryKind, ...] = (
    "meals",
    "workouts",
    "sleep",
    "recoveries",
    "measurements",
    "labs",
    "notes",
    "summaries",
    "days",
    "turns",
)
TEXT_KINDS: tuple[HistoryKind, ...] = ("turns", "notes", "summaries", "meals")
"""Kinds searched by keyword when the question has no date."""

CHARS_PER_TOKEN = 4
RENDER_MAX_TOKENS = 3000
MAX_DETAIL_CHARS = 320
MAX_SEARCH_KEYWORDS = 4
DEFAULT_SEARCH_LIMIT = 30

_TRUNCATED: dict[str, str] = {
    "en": "… truncated {n} more",
    "ru": "… ещё {n} не показано",
}


@dataclass(frozen=True)
class HistoryRow:
    """One row of history in a shape the model reads easily and the renderer can budget."""

    kind: str
    at: datetime
    title: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, Any]:
        return (self.kind, self.data.get("id"))


def _n(value: float | None) -> str:
    return "?" if value is None else str(round(value))


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _day_start(day: date, tz: str) -> datetime:
    return local_day_bounds(day, tz)[0]


# ------------------------------------------------------------------------------ row builders


def meal_row(meal: Meal, tz: str) -> HistoryRow:
    macros = repo.meal_macros(meal)
    names = ", ".join(item.name for item in meal.items) or "—"
    items = [
        {
            "id": i.id,
            "name": i.name,
            "grams": i.grams,
            "kcal": i.kcal,
            "protein_g": i.protein_g,
            "carbs_g": i.carbs_g,
            "fat_g": i.fat_g,
            "fiber_g": i.fiber_g,
            "countable": i.countable,
            "flags": list(i.flags or []),
        }
        for i in meal.items
    ]
    item_bits = [
        f"{i.name} {_n(i.kcal)} kcal ({_n(i.protein_g)}P/{_n(i.carbs_g)}C/{_n(i.fat_g)}F)"
        for i in meal.items
    ]
    detail = "; ".join(item_bits)
    detail += (
        f" = {_n(macros.kcal)} kcal, P {_n(macros.protein_g)}, C {_n(macros.carbs_g)},"
        f" F {_n(macros.fat_g)}, fiber {_n(macros.fiber_g)}"
    )
    if meal.note:
        detail += f" — {meal.note}"
    return HistoryRow(
        kind="meal",
        at=ensure_utc(meal.eaten_at or meal.logged_at),
        title=f"{meal.slot.value}: {_short(names, 80)}",
        detail=detail,
        data={
            "id": meal.id,
            "day_date": meal.day_date.isoformat(),
            "slot": meal.slot.value,
            "source": meal.source.value,
            "items": items,
            "totals": macros.model_dump(),
        },
    )


def workout_row(w: Workout) -> HistoryRow:
    bits: list[str] = []
    if w.duration_min:
        bits.append(f"{_n(w.duration_min)} min")
    if w.strain is not None:
        bits.append(f"strain {w.strain:.1f}")
    if w.kcal:
        bits.append(f"{_n(w.kcal)} kcal")
    if w.avg_hr:
        bits.append(f"avg HR {w.avg_hr}")
    if w.max_hr:
        bits.append(f"max HR {w.max_hr}")
    if w.zones_min:
        bits.append("zones " + " ".join(f"{k}:{_n(v)}" for k, v in sorted(w.zones_min.items())))
    if w.distance_m:
        bits.append(f"{w.distance_m / 1000:.1f} km")
    if w.note:
        bits.append(w.note)
    return HistoryRow(
        kind="workout",
        at=ensure_utc(w.started_at),
        title=w.sport,
        detail=" · ".join(bits),
        data={
            "id": w.id,
            "sport": w.sport,
            "source": w.source.value,
            "duration_min": w.duration_min,
            "strain": w.strain,
            "kcal": w.kcal,
            "avg_hr": w.avg_hr,
            "max_hr": w.max_hr,
            "zones_min": dict(w.zones_min) if w.zones_min else None,
        },
    )


def _hm(minutes: float) -> str:
    hours, mins = divmod(round(minutes), 60)
    return f"{hours}h{mins:02d}"


def sleep_row(s: Sleep, tz: str) -> HistoryRow:
    bits = [f"{to_local(s.started_at, tz):%H:%M}→{to_local(s.ended_at, tz):%H:%M}"]
    if s.asleep_min:
        bits.append(f"asleep {_hm(s.asleep_min)}")
    if s.in_bed_min:
        bits.append(f"in bed {_hm(s.in_bed_min)}")
    if s.performance_pct is not None:
        bits.append(f"{_n(s.performance_pct)}%")
    if s.disturbances:
        bits.append(f"{s.disturbances} disturbances")
    title = "sleep" + (f" {_hm(s.asleep_min)}" if s.asleep_min else "")
    return HistoryRow(
        kind="sleep",
        at=ensure_utc(s.ended_at),
        title=title,
        detail=" · ".join(bits),
        data={
            "id": s.id,
            "started_at": ensure_utc(s.started_at).isoformat(),
            "ended_at": ensure_utc(s.ended_at).isoformat(),
            "asleep_min": s.asleep_min,
            "in_bed_min": s.in_bed_min,
            "performance_pct": s.performance_pct,
        },
    )


def recovery_row(r: Recovery, tz: str) -> HistoryRow:
    bits: list[str] = []
    if r.rhr is not None:
        bits.append(f"rhr {_n(r.rhr)}")
    if r.hrv_ms is not None:
        bits.append(f"hrv {_n(r.hrv_ms)} ms")
    if r.spo2 is not None:
        bits.append(f"spo2 {_n(r.spo2)}%")
    return HistoryRow(
        kind="recovery",
        at=_day_start(r.date, tz),
        title=f"recovery {_n(r.score)}%",
        detail=" · ".join(bits),
        data={
            "id": r.id,
            "date": r.date.isoformat(),
            "score": r.score,
            "rhr": r.rhr,
            "hrv_ms": r.hrv_ms,
        },
    )


def measurement_row(m: Measurement) -> HistoryRow:
    value = f"{m.value:g} {m.unit}"
    return HistoryRow(
        kind="measurement",
        at=ensure_utc(m.measured_at),
        title=f"{m.type.value} {value}",
        detail=" · ".join(b for b in (m.source, m.note or "") if b),
        data={"id": m.id, "type": m.type.value, "value": m.value, "unit": m.unit},
    )


def lab_row(lab: Lab, tz: str) -> HistoryRow:
    bits: list[str] = []
    if lab.ref_low is not None or lab.ref_high is not None:
        bits.append(
            f"ref {lab.ref_low if lab.ref_low is not None else '?'}–{lab.ref_high if lab.ref_high is not None else '?'}"
        )
    if lab.flag:
        bits.append(lab.flag)
    unit = f" {lab.unit}" if lab.unit else ""
    return HistoryRow(
        kind="lab",
        at=_day_start(lab.taken_at, tz),
        title=f"{lab.marker} {lab.value:g}{unit}",
        detail=" · ".join(bits),
        data={
            "id": lab.id,
            "marker": lab.marker,
            "value": lab.value,
            "unit": lab.unit,
            "flag": lab.flag,
        },
    )


def note_row(n: Note) -> HistoryRow:
    status = "active" if n.active else "retired"
    return HistoryRow(
        kind="note",
        at=ensure_utc(n.created_at),
        title=f"[{n.kind.value}] {_short(n.text, 160)}",
        detail=f"{status} · conf {n.confidence:.1f}",
        data={"id": n.id, "kind": n.kind.value, "active": n.active, "confidence": n.confidence},
    )


def summary_row(s: Summary, tz: str) -> HistoryRow:
    period = (
        s.period_start.isoformat()
        if s.period_start == s.period_end
        else f"{s.period_start.isoformat()}…{s.period_end.isoformat()}"
    )
    return HistoryRow(
        kind="summary",
        at=_day_start(s.period_start, tz),
        title=f"{s.kind.value} summary {period}",
        detail=s.text,
        data={
            "id": s.id,
            "kind": s.kind.value,
            "period_start": s.period_start.isoformat(),
            "period_end": s.period_end.isoformat(),
            "data": dict(s.data) if s.data else None,
        },
    )


def day_row(d: Day, tz: str) -> HistoryRow:
    bits: list[str] = []
    if d.flags:
        bits.append("flags " + ", ".join(str(f) for f in d.flags))
    if d.plan:
        bits.append("plan " + "; ".join(f"{k}: {v}" for k, v in sorted(d.plan.items())))
    if d.verdict:
        bits.append(d.verdict)
    return HistoryRow(
        kind="day",
        at=_day_start(d.date, tz),
        title=f"day {d.date.isoformat()} {'closed' if d.closed_at else 'open'}",
        detail=" · ".join(bits),
        data={
            "id": d.id,
            "date": d.date.isoformat(),
            "closed": d.closed_at is not None,
            "flags": [str(f) for f in (d.flags or [])],
            "plan": dict(d.plan) if d.plan else None,
            "verdict": d.verdict,
        },
    )


def turn_row(t: ConversationTurn) -> HistoryRow:
    return HistoryRow(
        kind="turn",
        at=ensure_utc(t.created_at),
        title=f"{t.role.value}: {_short(t.text, 200)}",
        detail="",
        data={"id": t.id, "role": t.role.value},
    )


# ------------------------------------------------------------------------------- get_history


async def get_history(
    session: AsyncSession,
    user: User,
    *,
    kinds: Iterable[HistoryKind | str],
    date_from: date | None,
    date_to: date | None,
    text: str | None = None,
    limit: int = 50,
) -> list[HistoryRow]:
    """Typed rows of the requested kinds in ``[date_from, date_to]`` (local, inclusive).

    Up to ``limit`` most recent rows per kind, returned in chronological order. ``text``
    narrows by name/marker/note text; conversation turns are included only when ``text`` is
    given or when the range is at most seven days (a day's chat is context, a year's is noise).
    """
    tz = user.timezone or "UTC"
    limit = max(1, limit)
    start = _day_start(date_from, tz) if date_from else None
    end = local_day_bounds(date_to, tz)[1] if date_to else None
    text = " ".join(text.split()) if text else None
    wanted = {str(k) for k in kinds}
    unknown = wanted - set(ALL_HISTORY_KINDS)
    if unknown:
        raise ValueError(f"unknown history kinds: {sorted(unknown)}")
    rows: list[HistoryRow] = []

    if "meals" in wanted:
        meals = await queries.meals_range(
            session, user.id, date_from=date_from, date_to=date_to, text=text, limit=limit
        )
        rows += [meal_row(m, tz) for m in meals]
    if "workouts" in wanted:
        workouts = await queries.workouts_range(
            session, user.id, start=start, end=end, text=text, limit=limit
        )
        rows += [workout_row(w) for w in workouts]
    if "sleep" in wanted and not text:
        sleeps = await queries.sleep_range(session, user.id, start=start, end=end, limit=limit)
        rows += [sleep_row(s, tz) for s in sleeps]
    if "recoveries" in wanted and not text:
        recs = await queries.recoveries_range(
            session, user.id, date_from=date_from, date_to=date_to, limit=limit
        )
        rows += [recovery_row(r, tz) for r in recs]
    if "measurements" in wanted:
        ms = await queries.measurements_range(
            session, user.id, start=start, end=end, text=text, limit=limit
        )
        rows += [measurement_row(m) for m in ms]
    if "labs" in wanted:
        labs = await queries.labs_range(
            session, user.id, date_from=date_from, date_to=date_to, text=text, limit=limit
        )
        rows += [lab_row(lab, tz) for lab in labs]
    if "notes" in wanted:
        notes = await queries.notes_range(
            session, user.id, start=start, end=end, text=text, limit=limit
        )
        rows += [note_row(n) for n in notes]
    if "summaries" in wanted:
        summaries = await queries.summaries_range(
            session,
            user.id,
            kind=None,
            date_from=date_from,
            date_to=date_to,
            text=text,
            limit=limit,
        )
        rows += [summary_row(s, tz) for s in summaries]
    if "days" in wanted and not text:
        days = await repo.list_days_range(
            session, user.id, date_from or date.min, date_to or date.max
        )
        rows += [day_row(d, tz) for d in days[-limit:]]
    if "turns" in wanted and (text or _short_range(date_from, date_to)):
        turns = await queries.turns_range(
            session, user.id, start=start, end=end, text=text, limit=limit
        )
        rows += [turn_row(t) for t in turns]

    rows.sort(key=lambda r: (r.at, r.kind, str(r.data.get("id"))))
    return rows


def _short_range(date_from: date | None, date_to: date | None) -> bool:
    return date_from is not None and date_to is not None and (date_to - date_from).days <= 7


# ---------------------------------------------------------------------------- search_history


async def search_history(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    now_local: datetime,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[HistoryRow]:
    """Answer a free-text question about the past.

    1. If the text names a period, read every kind in that range (narrowed by the remaining
       keywords when there are any; if that yields nothing, the period alone).
    2. Otherwise (or when the period path found nothing) search turns, notes, summaries and
       food names by each content keyword, ranked by how many keywords a row matched.
    """
    lang = resolve_lang(user.language)
    match = find_period(text, now_local=now_local, lang=lang)
    rows: list[HistoryRow] = []
    if match is not None:
        rest = strip_period(text, match)
        terms = keywords(rest)[:MAX_SEARCH_KEYWORDS]
        typed_kinds: list[HistoryKind] = [k for k in ALL_HISTORY_KINDS if k != "turns"]
        if terms:
            for term in terms:
                rows += await get_history(
                    session,
                    user,
                    kinds=[*typed_kinds, "turns"],
                    date_from=match.start,
                    date_to=match.end,
                    text=term,
                    limit=limit,
                )
            rows = _dedupe(rows)
        if not rows:
            rows = await get_history(
                session,
                user,
                kinds=typed_kinds,
                date_from=match.start,
                date_to=match.end,
                limit=limit,
            )
        log.debug("history_period", user_id=user.id, label=match.label, rows=len(rows))
    if rows:
        return rows[-limit:]

    terms = keywords(text)[:MAX_SEARCH_KEYWORDS]
    if not terms:
        return []
    hits: dict[tuple[str, Any], tuple[int, HistoryRow]] = {}
    for term in terms:
        for row in await get_history(
            session, user, kinds=TEXT_KINDS, date_from=None, date_to=None, text=term, limit=limit
        ):
            score, _ = hits.get(row.key, (0, row))
            hits[row.key] = (score + 1, row)
    ranked = sorted(hits.values(), key=lambda pair: (-pair[0], pair[1].at))
    top = [row for _, row in ranked[:limit]]
    top.sort(key=lambda r: r.at)
    log.debug("history_search", user_id=user.id, terms=terms, rows=len(top))
    return top


def _dedupe(rows: Sequence[HistoryRow]) -> list[HistoryRow]:
    seen: dict[tuple[str, Any], HistoryRow] = {}
    for row in rows:
        seen.setdefault(row.key, row)
    out = list(seen.values())
    out.sort(key=lambda r: (r.at, r.kind, str(r.data.get("id"))))
    return out


# ------------------------------------------------------------------------------------ render


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def render_row(row: HistoryRow, tz: str) -> str:
    local = to_local(row.at, tz)
    # Rows anchored to a date (recovery, lab, summary, day) carry local midnight; show no time.
    stamp = (
        local.strftime("%Y-%m-%d") if local.time() == time.min else local.strftime("%Y-%m-%d %H:%M")
    )
    line = f"{stamp} {row.kind} {row.title}"
    if row.detail:
        line += f" — {_short(row.detail, MAX_DETAIL_CHARS)}"
    return line


def render_rows(
    rows: Sequence[HistoryRow],
    lang: str | None,
    *,
    tz: str = "UTC",
    max_tokens: int = RENDER_MAX_TOKENS,
) -> str:
    """One line per row within ``max_tokens`` (≈ 4 chars/token); ends with a truncation note."""
    if not rows:
        return ""
    budget = max_tokens * CHARS_PER_TOKEN
    template = _TRUNCATED[resolve_lang(lang)]
    trailer_reserve = len(template.format(n=len(rows))) + 1
    lines: list[str] = []
    used = 0
    shown = 0
    for row in rows:
        line = render_row(row, tz)
        cost = len(line) + 1
        remaining_after = len(rows) - shown - 1
        reserve = trailer_reserve if remaining_after > 0 else 0
        if used + cost + reserve > budget and shown > 0:
            break
        lines.append(line)
        used += cost
        shown += 1
    hidden = len(rows) - shown
    if hidden > 0:
        lines.append(template.format(n=hidden))
    return "\n".join(lines)
