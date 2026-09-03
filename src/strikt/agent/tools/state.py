"""Day-state tools: get_day_state, set_day_flag, set_day_plan, close_day, render_day_card.

``close_day`` is the end-of-day ritual (brief §3.3, §7.4): verdict on the ``days`` row, the day
summary written by the model through ``memory.summaries`` (a failing LLM never blocks the close;
the nightly job retries), the week summary refreshed, and the close line with the bed target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from strikt.agent.tools.common import build_state, fail, macros_dict, ok, rnd, state_numbers
from strikt.core.clock import week_start
from strikt.db import repo
from strikt.memory import summaries
from strikt.memory.daystate import render_context
from strikt.telegram.render import render_day_card as render_card

if TYPE_CHECKING:
    from strikt.agent.tools import schemas
    from strikt.agent.tools.registry import ToolContext, ToolResult

log = structlog.get_logger(__name__)


async def get_day_state(ctx: ToolContext, args: schemas.GetDayStateInput) -> ToolResult:
    day = args.date or ctx.local_date
    state = await build_state(ctx, day)
    numbers = state_numbers(state)
    numbers["items"] = [
        {"meal_id": m.id, "slot": m.slot, "items": [{"id": i.id, "name": i.name} for i in m.items]}
        for m in state.meals
    ]
    numbers["measurements_due"] = state.measurements_due
    numbers["flags"] = state.flags
    return ok({"text": render_context(state, ctx.lang, tz=ctx.tz), "numbers": numbers})


async def set_day_flag(ctx: ToolContext, args: schemas.SetDayFlagInput) -> ToolResult:
    row = await repo.set_day_flag(
        ctx.session, ctx.user_id, args.date, args.flag, args.on, now=ctx.clock.now()
    )
    flags = [str(f) for f in (row.flags or [])]
    result: dict[str, Any] = {"date": args.date.isoformat(), "flags": flags}
    if args.on and args.flag in {"salty", "alcohol"}:
        result["note"] = "tomorrow's weight is water; skip the scale or ignore it"
    if args.on and args.flag == "sick":
        result["note"] = "targets paused: hydration, electrolytes, no training, reintroduce slowly"
    log.info(
        "day_flag_set", user_id=ctx.user_id, date=args.date.isoformat(), flag=args.flag, on=args.on
    )
    return ok(result)


async def set_day_plan(ctx: ToolContext, args: schemas.SetDayPlanInput) -> ToolResult:
    plan = args.plan.model_dump(exclude_none=True)
    if not plan:
        return fail("set_day_plan: the plan is empty")
    row = await repo.set_day_plan(ctx.session, ctx.user_id, args.date, plan, now=ctx.clock.now())
    return ok({"date": args.date.isoformat(), "plan": row.plan})


async def close_day(ctx: ToolContext, args: schemas.CloseDayInput) -> ToolResult:
    day = args.date
    if day > ctx.local_date:
        return fail("close_day: cannot close a future day")
    verdict = " ".join(args.verdict.split())
    if not verdict:
        return fail("close_day: verdict is empty")
    now = ctx.clock.now()
    row = await repo.close_day(ctx.session, ctx.user_id, day, verdict=verdict, now=now)
    llm = ctx.services.get("llm")
    summary_state = "skipped (no llm wired)"
    if llm is not None:
        try:
            await summaries.write_day_summary(llm, ctx.session, ctx.user, day, clock=ctx.clock)
            await summaries.update_week_summary(
                llm, ctx.session, ctx.user, week_start(day), clock=ctx.clock
            )
            summary_state = "written"
        except Exception as exc:
            log.warning("close_day_summary_failed", user_id=ctx.user_id, error=repr(exc))
            summary_state = "deferred to the nightly job"
    state = await build_state(ctx, day)
    t = state.totals.macros
    close_line = (
        f"Closed at {rnd(t.kcal, 0)} kcal / {rnd(t.protein_g, 0)} P / {rnd(t.carbs_g, 0)} C /"
        f" {rnd(t.fat_g, 0)} F / {rnd(t.fiber_g, 0)} fiber"
    )
    result: dict[str, Any] = {
        "date": day.isoformat(),
        "closed_at": row.closed_at,
        "verdict": verdict,
        "close_line": close_line,
        "totals": macros_dict(t),
        "targets": macros_dict(state.targets),
        "remaining": state_numbers(state)["remaining"],
        "meals": state.totals.meals,
        "workouts": len(state.workouts),
        "flags": state.flags,
        "summary": summary_state,
    }
    if ctx.profile is not None and ctx.profile.bed_time is not None:
        result["bed_line"] = f"Bed by {ctx.profile.bed_time:%H:%M}"
    log.info("day_closed", user_id=ctx.user_id, date=day.isoformat(), summary=summary_state)
    return ok(result)


async def render_day_card(ctx: ToolContext, args: schemas.RenderDayCardInput) -> ToolResult:
    from strikt.agent.tools.registry import ToolResult

    state = await build_state(ctx)
    return ToolResult(content=render_card(state, ctx.lang, tz=ctx.tz))
