"""Memory: day state, coach notes, rolling summaries, temporal parsing and history retrieval.

This is the "infinite memory" contract (PLAN §6.2, research/07). Four tiers:

- ``daystate``   today's typed rows rendered as a compact context block (every turn);
- ``notes``      atomic coach observations with dedupe/supersede (cached profile block);
- ``summaries``  day → week rolling summaries written by the model off the hot path;
- ``retrieval``  typed history rows + keyword search, with ``periods`` resolving "last Tuesday".

Every function takes the ORM ``User`` and filters by ``user.id``. Functions flush; callers
commit.
"""

from __future__ import annotations

from strikt.memory.daystate import (
    DEFAULT_TARGETS,
    DayStateBuilder,
    render_context,
    yesterday_close_line,
)
from strikt.memory.notes import (
    NoteWrite,
    active_notes,
    add_note,
    relevant_notes,
    render_notes_block,
    retire,
)
from strikt.memory.periods import PeriodMatch, find_period, parse_period, strip_period
from strikt.memory.retrieval import (
    ALL_HISTORY_KINDS,
    HistoryRow,
    get_history,
    render_rows,
    search_history,
)
from strikt.memory.summaries import (
    SUMMARY_SCHEMA,
    SummaryOutput,
    update_week_summary,
    write_day_summary,
)

__all__ = [
    "ALL_HISTORY_KINDS",
    "DEFAULT_TARGETS",
    "SUMMARY_SCHEMA",
    "DayStateBuilder",
    "HistoryRow",
    "NoteWrite",
    "PeriodMatch",
    "SummaryOutput",
    "active_notes",
    "add_note",
    "find_period",
    "get_history",
    "parse_period",
    "relevant_notes",
    "render_context",
    "render_notes_block",
    "render_rows",
    "retire",
    "search_history",
    "strip_period",
    "update_week_summary",
    "write_day_summary",
    "yesterday_close_line",
]
