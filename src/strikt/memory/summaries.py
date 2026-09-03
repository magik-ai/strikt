"""Rolling summaries (PLAN §6.6, research/07 D4): day on close/nightly, week from the days.

The model writes the text; the code gathers the period (typed rows, notes written that day,
the user's own words) and computes the numbers it must not invent. Output is validated
against ``SUMMARY_SCHEMA`` (structured outputs via ``LLMClient.message(output_schema=...)``)
and upserted into ``summaries``. A refusal or unparsable answer falls back to a deterministic
numbers-only summary so the memory chain never has a hole; a transport error propagates so
the scheduler can retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.agent.client import LLMClient
from strikt.core.clock import Clock, ensure_utc, local_day_bounds, to_local
from strikt.core.types import DayState, Macros
from strikt.db import repo
from strikt.db.models import (
    Profile,
    Summary,
    SummaryKind,
    TurnRole,
    User,
)
from strikt.memory import queries
from strikt.memory.daystate import DayStateBuilder, render_context
from strikt.memory.notes import normalise_text
from strikt.telegram.copy import resolve_lang

log = structlog.get_logger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "agent" / "prompts" / "summarize.md"

MAX_USER_WORDS_CHARS = 6000
MAX_TURN_CHARS = 300
PRIOR_DAYS_FOR_PATTERNS = 3
KCAL_TOLERANCE = 1.05  # within target +5 % counts as a kcal hit
PROTEIN_HIT_RATIO = 0.9
FIBER_HIT_RATIO = 0.8
BEDTIME_SLACK_MIN = 30

_TOTALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kcal": {"type": "number"},
        "protein_g": {"type": "number"},
        "carbs_g": {"type": "number"},
        "fat_g": {"type": "number"},
        "fiber_g": {"type": "number"},
    },
    "required": ["kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"],
    "additionalProperties": False,
}
_ADHERENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kcal": {"type": "number"},
        "protein": {"type": "number"},
        "fiber": {"type": "number"},
        "bedtime": {"type": "number"},
        "meals_logged": {"type": "integer"},
    },
    "required": ["kcal", "protein", "fiber", "bedtime", "meals_logged"],
    "additionalProperties": False,
}
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "data": {
            "type": "object",
            "properties": {
                "totals": _TOTALS_SCHEMA,
                "adherence": _ADHERENCE_SCHEMA,
                "patterns": {"type": "array", "items": {"type": "string"}},
                "flagged": {"type": "array", "items": {"type": "string"}},
                "user_said": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["totals", "adherence", "patterns", "flagged", "user_said"],
            "additionalProperties": False,
        },
    },
    "required": ["text", "data"],
    "additionalProperties": False,
}


class SummaryTotals(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kcal: float = 0
    protein_g: float = 0
    carbs_g: float = 0
    fat_g: float = 0
    fiber_g: float = 0


class SummaryAdherence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kcal: float = 0
    protein: float = 0
    fiber: float = 0
    bedtime: float = 0
    meals_logged: int = 0


class SummaryData(BaseModel):
    model_config = ConfigDict(extra="ignore")
    totals: SummaryTotals = Field(default_factory=SummaryTotals)
    adherence: SummaryAdherence = Field(default_factory=SummaryAdherence)
    patterns: list[str] = Field(default_factory=list)
    flagged: list[str] = Field(default_factory=list)
    user_said: list[str] = Field(default_factory=list)


class SummaryOutput(BaseModel):
    """What the model returns for both kinds (lenient: missing fields default)."""

    model_config = ConfigDict(extra="ignore")
    text: str
    data: SummaryData = Field(default_factory=SummaryData)


@lru_cache(maxsize=1)
def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _n(value: float | None) -> str:
    return "?" if value is None else str(round(value))


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _lang_name(lang: str) -> str:
    return "Russian" if lang == "ru" else "English"


# ------------------------------------------------------------------------------- day digest


async def _user_words(
    session: AsyncSession, user_id: int, start: datetime, end: datetime, tz: str
) -> list[str]:
    turns = await queries.turns_range(
        session, user_id, start=start, end=end, role=TurnRole.user, limit=400
    )
    lines: list[str] = []
    used = 0
    for turn in reversed(turns):  # chronological
        text = " ".join(turn.text.split())
        if not text:
            continue
        line = f"- {to_local(turn.created_at, tz):%H:%M} {_short(text, MAX_TURN_CHARS)}"
        if used + len(line) > MAX_USER_WORDS_CHARS:
            lines.append("- … (more messages omitted)")
            break
        lines.append(line)
        used += len(line)
    return lines


def day_computed(state: DayState, profile: Profile | None, *, tz: str) -> dict[str, Any]:
    """Numbers the code owns for one day (the model must not invent them)."""
    t, g = state.totals.macros, state.targets
    has_data = state.totals.meals > 0
    bedtime_hit: bool | None = None
    if state.sleep and profile and profile.bed_time:
        onset = to_local(state.sleep.started_at, tz)
        target_min = profile.bed_time.hour * 60 + profile.bed_time.minute
        onset_min = onset.hour * 60 + onset.minute
        # bedtimes after midnight (00:30) compare on a 24h ring
        diff = (onset_min - target_min + 720) % 1440 - 720
        bedtime_hit = diff <= BEDTIME_SLACK_MIN
    return {
        "has_data": has_data,
        "totals": t.model_dump(),
        "targets": g.model_dump(),
        "meals_logged": state.totals.meals,
        "items_logged": state.totals.items,
        "kcal_hit": has_data and g.kcal > 0 and t.kcal <= g.kcal * KCAL_TOLERANCE,
        "protein_hit": has_data
        and g.protein_g > 0
        and t.protein_g >= g.protein_g * PROTEIN_HIT_RATIO,
        "fiber_hit": has_data and g.fiber_g > 0 and t.fiber_g >= g.fiber_g * FIBER_HIT_RATIO,
        "workouts": len(state.workouts),
        "strain": sum(w.strain or 0 for w in state.workouts),
        "sleep_min": state.sleep.asleep_min if state.sleep else None,
        "recovery": state.recovery.score if state.recovery else None,
        "bedtime_hit": bedtime_hit,
        "closed": state.closed,
        "flags": list(state.flags),
    }


async def gather_day(
    session: AsyncSession,
    user: User,
    day: date,
    *,
    clock: Clock,
    builder: DayStateBuilder | None = None,
) -> tuple[str, DayState, dict[str, Any]]:
    """The digest text the model summarises, the DayState and the computed facts."""
    tz = user.timezone or "UTC"
    lang = resolve_lang(user.language)
    builder = builder or DayStateBuilder(clock)
    state = await builder.day_state(session, user, day)
    profile = await repo.get_profile(session, user.id)
    start, end = local_day_bounds(day, tz)
    computed = day_computed(state, profile, tz=tz)

    parts: list[str] = [f"<day date={day.isoformat()} tz={tz}>", render_context(state, lang, tz=tz)]

    measurements = await queries.measurements_range(
        session, user.id, start=start, end=end, limit=20
    )
    if measurements:
        parts.append(
            "measurements: "
            + "; ".join(f"{m.type.value} {m.value:g} {m.unit}" for m in reversed(measurements))
        )
    notes = await queries.notes_range(session, user.id, start=start, end=end, limit=20)
    if notes:
        parts.append("notes written today:")
        parts += [f"- [{n.kind.value}] {n.text}" for n in reversed(notes)]
    words = await _user_words(session, user.id, start, end, tz)
    if words:
        parts.append("user said (own words, local time):")
        parts += words
    else:
        parts.append("user said: nothing today")

    prior = await queries.summaries_range(
        session,
        user.id,
        kind=SummaryKind.day,
        date_from=day - timedelta(days=PRIOR_DAYS_FOR_PATTERNS),
        date_to=day - timedelta(days=1),
        limit=PRIOR_DAYS_FOR_PATTERNS,
    )
    if prior:
        parts.append("prior day summaries (for patterns only):")
        for s in reversed(prior):
            patterns = (s.data or {}).get("patterns") if s.data else None
            line = f"- {s.period_start.isoformat()}: {_short(s.text, 400)}"
            if patterns:
                line += " | patterns: " + "; ".join(str(p) for p in patterns)
            parts.append(line)
    parts.append("computed (authoritative): " + _facts_line(computed))
    parts.append("</day>")
    return "\n".join(parts), state, computed


def _facts_line(computed: dict[str, Any]) -> str:
    keys = (
        "meals_logged",
        "kcal_hit",
        "protein_hit",
        "fiber_hit",
        "workouts",
        "sleep_min",
        "recovery",
        "bedtime_hit",
        "closed",
        "flags",
    )
    return ", ".join(f"{k}={computed[k]}" for k in keys if k in computed)


# --------------------------------------------------------------------------------- LLM call


async def _call(
    llm: LLMClient,
    *,
    user_id: int,
    kind: SummaryKind,
    digest: str,
    lang: str,
) -> SummaryOutput | None:
    instruction = (
        f"Write the {kind.value} summary (kind={kind.value}) as JSON matching the schema. "
        f"Language: {_lang_name(lang)}."
    )
    result = await llm.message(
        purpose="summary",
        system=load_prompt(),
        messages=[
            {"role": "user", "content": [{"type": "text", "text": f"{digest}\n\n{instruction}"}]}
        ],
        output_schema=SUMMARY_SCHEMA,
        user_id=user_id,
        cache_tail=False,
    )
    if result.refused:
        log.warning("summary_refused", user_id=user_id, kind=kind.value)
        return None
    try:
        payload = result.json()
        parsed = SummaryOutput.model_validate(payload)
    except (ValueError, TypeError) as exc:
        log.warning("summary_invalid", user_id=user_id, kind=kind.value, error=repr(exc))
        return None
    if not parsed.text.strip():
        log.warning("summary_empty", user_id=user_id, kind=kind.value)
        return None
    return parsed


def _fallback_day_text(state: DayState, lang: str) -> str:
    t, g = state.totals.macros, state.targets
    if state.totals.meals == 0:
        return "нет данных" if lang == "ru" else "no data"
    line = (
        f"{_n(t.kcal)}/{_n(g.kcal)} kcal, P {_n(t.protein_g)}/{_n(g.protein_g)}, "
        f"C {_n(t.carbs_g)}, F {_n(t.fat_g)}, fiber {_n(t.fiber_g)}/{_n(g.fiber_g)}, "
        f"{state.totals.meals} meals"
    )
    if state.workouts:
        line += f", {len(state.workouts)} workout(s)"
    if state.flags:
        line += ", flags: " + ", ".join(state.flags)
    return line


# ------------------------------------------------------------------------------ day summary


async def write_day_summary(
    llm: LLMClient,
    session: AsyncSession,
    user: User,
    day: date,
    *,
    clock: Clock,
    builder: DayStateBuilder | None = None,
) -> Summary:
    """Summarise one local day and upsert ``summaries(kind=day)``. Flushes; caller commits."""
    lang = resolve_lang(user.language)
    digest, state, computed = await gather_day(session, user, day, clock=clock, builder=builder)
    parsed = await _call(llm, user_id=user.id, kind=SummaryKind.day, digest=digest, lang=lang)
    if parsed is None:
        text = _fallback_day_text(state, lang)
        data: dict[str, Any] = {
            "totals": _totals_dict(state.totals.macros),
            "adherence": {
                "kcal": 1.0 if computed["kcal_hit"] else 0.0,
                "protein": 1.0 if computed["protein_hit"] else 0.0,
                "fiber": 1.0 if computed["fiber_hit"] else 0.0,
                "bedtime": 1.0 if computed["bedtime_hit"] else 0.0,
                "meals_logged": computed["meals_logged"],
            },
            "patterns": [],
            "flagged": [],
            "user_said": [],
            "fallback": True,
        }
    else:
        data = parsed.data.model_dump()
        # the code's totals win over the model's (research/07 §10: numbers live in rows)
        data["totals"] = _totals_dict(state.totals.macros)
        text = parsed.text.strip()
    data["computed"] = computed
    row = await repo.upsert_summary(
        session,
        user.id,
        kind=SummaryKind.day,
        period_start=day,
        period_end=day,
        text=text,
        data=data,
        now=ensure_utc(clock.now()),
    )
    log.info("day_summary_written", user_id=user.id, day=day.isoformat(), fallback=parsed is None)
    return row


def _totals_dict(m: Macros) -> dict[str, float]:
    return {
        "kcal": round(m.kcal, 1),
        "protein_g": round(m.protein_g, 1),
        "carbs_g": round(m.carbs_g, 1),
        "fat_g": round(m.fat_g, 1),
        "fiber_g": round(m.fiber_g, 1),
    }


# ----------------------------------------------------------------------------- week summary


def week_computed(
    days: Sequence[Summary], *, targets: Macros, workouts: int, measurements: int
) -> dict[str, Any]:
    """Aggregate the seven day summaries deterministically (averages, hits, repeated patterns)."""
    with_data = [s for s in days if (s.data or {}).get("computed", {}).get("has_data")]
    n = len(with_data)

    def avg(key: str) -> float | None:
        if not with_data:
            return None
        return round(
            sum(float(((s.data or {}).get("totals") or {}).get(key, 0)) for s in with_data) / n, 1
        )

    def hits(key: str) -> int:
        return sum(1 for s in with_data if ((s.data or {}).get("computed") or {}).get(key))

    bedtime_known = [
        s for s in days if ((s.data or {}).get("computed") or {}).get("bedtime_hit") is not None
    ]
    counts: dict[str, int] = {}
    originals: dict[str, str] = {}
    for s in days:
        for pattern in (s.data or {}).get("patterns") or []:
            key = normalise_text(str(pattern))
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
            originals.setdefault(key, str(pattern))
    repeated = [
        originals[k] for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if c >= 2
    ]
    return {
        "days_with_data": n,
        "days_summarised": len(days),
        "avg_kcal": avg("kcal"),
        "avg_protein_g": avg("protein_g"),
        "avg_carbs_g": avg("carbs_g"),
        "avg_fat_g": avg("fat_g"),
        "avg_fiber_g": avg("fiber_g"),
        "kcal_hits": hits("kcal_hit"),
        "protein_hits": hits("protein_hit"),
        "fiber_hits": hits("fiber_hit"),
        "bedtime_hits": hits("bedtime_hit"),
        "bedtime_known": len(bedtime_known),
        "closed_days": hits("closed"),
        "workouts": workouts,
        "measurements": measurements,
        "targets": targets.model_dump(),
        "repeated_patterns": repeated,
    }


def _week_adherence(c: dict[str, Any]) -> dict[str, float | int]:
    n = max(1, int(c["days_with_data"]))
    nights = max(1, int(c["bedtime_known"]))
    return {
        "kcal": round(c["kcal_hits"] / n, 2),
        "protein": round(c["protein_hits"] / n, 2),
        "fiber": round(c["fiber_hits"] / n, 2),
        "bedtime": round(c["bedtime_hits"] / nights, 2),
        "meals_logged": int(c["days_with_data"]),
    }


def _fallback_week_text(c: dict[str, Any], lang: str) -> str:
    if not c["days_with_data"]:
        return "нет данных за неделю" if lang == "ru" else "no data this week"
    return (
        f"avg {_n(c['avg_kcal'])} kcal, P {_n(c['avg_protein_g'])}, fiber {_n(c['avg_fiber_g'])}; "
        f"kcal {c['kcal_hits']}/{c['days_with_data']}, protein {c['protein_hits']}/{c['days_with_data']}, "
        f"fiber {c['fiber_hits']}/{c['days_with_data']}, sessions {c['workouts']}, "
        f"bedtime {c['bedtime_hits']}/{c['bedtime_known']}, measurements {c['measurements']}"
    )


async def update_week_summary(
    llm: LLMClient,
    session: AsyncSession,
    user: User,
    week_start_day: date,
    *,
    clock: Clock,
) -> Summary:
    """Aggregate Monday..Sunday day summaries into ``summaries(kind=week)``. Flushes only."""
    tz = user.timezone or "UTC"
    lang = resolve_lang(user.language)
    start_day = week_start_day - timedelta(days=week_start_day.weekday())
    end_day = start_day + timedelta(days=6)
    days = await queries.summaries_range(
        session, user.id, kind=SummaryKind.day, date_from=start_day, date_to=end_day, limit=7
    )
    days = sorted(days, key=lambda s: s.period_start)
    protocol = await repo.get_active_protocol(session, user.id)
    targets = repo.protocol_targets(protocol)
    start, end = local_day_bounds(start_day, tz)[0], local_day_bounds(end_day, tz)[1]
    workouts = await queries.workouts_range(session, user.id, start=start, end=end, limit=100)
    measurements = await queries.measurements_range(
        session, user.id, start=start, end=end, limit=100
    )
    computed = week_computed(
        days, targets=targets, workouts=len(workouts), measurements=len(measurements)
    )

    parts = [f"<week start={start_day.isoformat()} end={end_day.isoformat()} tz={tz}>"]
    by_date = {s.period_start: s for s in days}
    for offset in range(7):
        d = start_day + timedelta(days=offset)
        s = by_date.get(d)
        if s is None:
            parts.append(f"- {d.isoformat()}: no data")
            continue
        totals = (s.data or {}).get("totals") or {}
        line = f"- {d.isoformat()}: {_short(s.text, 500)}"
        if totals:
            line += f" | totals {_n(totals.get('kcal'))} kcal, P {_n(totals.get('protein_g'))}, fiber {_n(totals.get('fiber_g'))}"
        patterns = (s.data or {}).get("patterns") or []
        if patterns:
            line += " | patterns: " + "; ".join(str(p) for p in patterns)
        said = (s.data or {}).get("user_said") or []
        if said:
            line += " | user said: " + "; ".join(str(p) for p in said[:3])
        parts.append(line)
    parts.append(
        "workouts this week: "
        + (
            "; ".join(
                f"{w.sport} {_n(w.duration_min)} min strain {w.strain if w.strain is not None else '?'}"
                for w in workouts
            )
            or "none"
        )
    )
    parts.append(
        "computed (authoritative): "
        + ", ".join(f"{k}={v}" for k, v in computed.items() if k != "targets")
    )
    parts.append(f"targets: {targets.model_dump()}")
    parts.append("</week>")
    digest = "\n".join(parts)

    parsed = await _call(llm, user_id=user.id, kind=SummaryKind.week, digest=digest, lang=lang)
    if parsed is None:
        text = _fallback_week_text(computed, lang)
        data: dict[str, Any] = {
            "totals": {
                "kcal": computed["avg_kcal"] or 0,
                "protein_g": computed["avg_protein_g"] or 0,
                "carbs_g": computed["avg_carbs_g"] or 0,
                "fat_g": computed["avg_fat_g"] or 0,
                "fiber_g": computed["avg_fiber_g"] or 0,
            },
            "adherence": _week_adherence(computed),
            "patterns": list(computed["repeated_patterns"]),
            "flagged": [],
            "user_said": [],
            "fallback": True,
        }
    else:
        data = parsed.data.model_dump()
        data["adherence"] = _week_adherence(computed)
        merged = list(data.get("patterns") or [])
        seen = {normalise_text(p) for p in merged}
        merged += [p for p in computed["repeated_patterns"] if normalise_text(p) not in seen]
        data["patterns"] = merged
        text = parsed.text.strip()
    data["computed"] = computed
    row = await repo.upsert_summary(
        session,
        user.id,
        kind=SummaryKind.week,
        period_start=start_day,
        period_end=end_day,
        text=text,
        data=data,
        now=ensure_utc(clock.now()),
    )
    log.info(
        "week_summary_written", user_id=user.id, week=start_day.isoformat(), fallback=parsed is None
    )
    return row
