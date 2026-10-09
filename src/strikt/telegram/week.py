"""``/week``: the last seven coaching days against the daily targets. A pure read, no model call."""

from __future__ import annotations

from datetime import date, time, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from strikt.core.types import Macros
from strikt.db import repo
from strikt.db.models import User
from strikt.proactive.stats import WeekScorecard, week_scorecard
from strikt.telegram.copy import resolve_lang, t
from strikt.telegram.render import fmt_num

WEEK_DAYS = 7


def _row(label: str, avg: float | None, target: float, unit: str = "") -> str:
    shown = fmt_num(avg or 0)
    tail = f" / {fmt_num(target)}" if target > 0 else ""
    return f"<code>{label:<5}{shown:>6}{tail}{unit}</code>"


def render_week(card: WeekScorecard, targets: Macros, lang: str | None) -> str:
    """The reply text; ``card`` must cover the window (the caller asks for ``WEEK_DAYS`` days)."""
    if card.days_logged == 0:
        return t(lang, "week.empty")
    lines = [
        t(lang, "week.head", logged=card.days_logged, days=WEEK_DAYS),
        t(lang, "week.avg"),
        _row("kcal", card.avg_kcal, targets.kcal),
        _row("P", card.avg_protein_g, targets.protein_g, "g"),
        _row("C", card.avg_carbs_g, targets.carbs_g, "g"),
        _row("F", card.avg_fat_g, targets.fat_g, "g"),
    ]
    if targets.kcal <= 0:
        lines.append(t(lang, "week.no_targets"))
    return "\n".join(lines)


async def week_text(
    session: AsyncSession, user: User, *, today: date, bed_time: time | None
) -> str:
    """The seven coaching days before ``today`` (the caller's ``coaching_today``), plus ``today``
    once it is closed. An open day is a partial day and would drag every average down."""
    day = await repo.get_day(session, user.id, today)
    closed = day is not None and day.closed_at is not None
    targets = repo.protocol_targets(await repo.get_active_protocol(session, user.id))
    card = await week_scorecard(
        session,
        user.id,
        week_start=today - timedelta(days=WEEK_DAYS - (1 if closed else 0)),
        tz=user.timezone or "UTC",
        targets=targets,
        bed_time=bed_time,
    )
    return render_week(card, targets, resolve_lang(user.language))
