"""Memory tools: get_history, search_history, write_note, retire_note, set_reminder,
cancel_reminder (PLAN §6.2, §6.4).

History comes through ``memory.retrieval`` (typed rows, rendered inside a token budget); notes
through ``memory.notes`` (dedupe / supersede); reminders through ``repo.reminders`` with naive
times read in the user's timezone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.tools.common import fail, hhmm, ok, to_utc
from strikt.core.clock import ensure_utc, local_now, to_local
from strikt.db import repo
from strikt.memory import notes as notes_mod, retrieval

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)

DEFAULT_RANGE_DAYS = 7
HISTORY_MAX_TOKENS = 1500
SEARCH_MAX_TOKENS = 1200
MAX_LIMIT = 200
MAX_REMINDER_DAYS = 400


async def get_history(ctx: ToolContext, args: schemas.GetHistoryInput) -> ToolResult:
    if not args.kinds:
        return fail("get_history: give at least one kind")
    date_from, date_to = args.date_from, args.date_to
    if date_from is None and date_to is None and not args.text:
        date_to = ctx.local_date
        date_from = date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from, date_to = date_to, date_from
    limit = max(1, min(args.limit, MAX_LIMIT))
    rows = await retrieval.get_history(
        ctx.session,
        ctx.user,
        kinds=list(args.kinds),
        date_from=date_from,
        date_to=date_to,
        text=args.text,
        limit=limit,
    )
    text = retrieval.render_rows(rows, ctx.lang, tz=ctx.tz, max_tokens=HISTORY_MAX_TOKENS)
    return ok(
        {
            "kinds": list(args.kinds),
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
            "count": len(rows),
            "rows": text or "no rows",
        }
    )


async def search_history(ctx: ToolContext, args: schemas.SearchHistoryInput) -> ToolResult:
    query = " ".join(args.text.split())
    if not query:
        return fail("search_history: empty query")
    limit = max(1, min(args.limit, MAX_LIMIT))
    rows = await retrieval.search_history(
        ctx.session, ctx.user, query, now_local=local_now(ctx.clock, ctx.tz), limit=limit
    )
    text = retrieval.render_rows(rows, ctx.lang, tz=ctx.tz, max_tokens=SEARCH_MAX_TOKENS)
    return ok({"query": query, "count": len(rows), "rows": text or "no matches"})


async def write_note(ctx: ToolContext, args: schemas.WriteNoteInput) -> ToolResult:
    text = " ".join(args.text.split())
    if not text:
        return fail("write_note: empty text")
    expires = to_utc(args.expires_at, ctx.tz) if args.expires_at is not None else None
    write = await notes_mod.add_note(
        ctx.session,
        ctx.user,
        args.kind,
        text,
        args.confidence,
        now=ctx.clock.now(),
        expires_at=expires,
        supersedes_id=args.supersedes_id,
    )
    result: dict[str, Any] = {
        "note_id": write.note.id,
        "kind": write.note.kind.value,
        "text": write.note.text,
        "status": "created" if write.created else "refreshed (identical note existed)",
    }
    if write.superseded_id is not None:
        result["superseded_id"] = write.superseded_id
    if expires is not None:
        result["expires"] = to_local(expires, ctx.tz).date().isoformat()
    return ok(result)


async def retire_note(ctx: ToolContext, args: schemas.RetireNoteInput) -> ToolResult:
    done = await notes_mod.retire(ctx.session, ctx.user, args.id)
    if not done:
        return fail(f"note {args.id} not found or already retired")
    return ok({"retired_note_id": args.id})


async def set_reminder(ctx: ToolContext, args: schemas.SetReminderInput) -> ToolResult:
    text = " ".join(args.text.split())
    if not text:
        return fail("set_reminder: empty text")
    now = ctx.clock.now()
    due = to_utc(args.when, ctx.tz)
    if due <= now:
        return fail(
            f"set_reminder: {hhmm(due, ctx.tz)} on {to_local(due, ctx.tz).date().isoformat()} "
            f"is in the past (now {hhmm(now, ctx.tz)} local)"
        )
    if due - now > timedelta(days=MAX_REMINDER_DAYS):
        return fail("set_reminder: more than a year ahead")
    row = await repo.add_reminder(
        ctx.session, ctx.user_id, due_at=due, text=text, now=now, kind=args.kind or "custom"
    )
    local = to_local(due, ctx.tz)
    log.info(
        "reminder_set", user_id=ctx.user_id, reminder_id=row.id, due=ensure_utc(due).isoformat()
    )
    return ok(
        {
            "reminder_id": row.id,
            "due_local": local.strftime("%Y-%m-%d %H:%M"),
            "in_minutes": int((due - now).total_seconds() // 60),
            "text": text,
            "kind": row.kind,
        }
    )


async def cancel_reminder(ctx: ToolContext, args: schemas.CancelReminderInput) -> ToolResult:
    done = await repo.cancel_reminder(ctx.session, ctx.user_id, args.id)
    if not done:
        pending = await repo.pending_reminders(ctx.session, ctx.user_id)
        ids = [r.id for r in pending]
        return fail(f"reminder {args.id} is not pending; pending ids: {ids}")
    return ok({"cancelled_reminder_id": args.id})
