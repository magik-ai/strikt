"""``/week``: the last seven coaching days against the daily targets. A pure read, no model call."""

from __future__ import annotations

from strikt.core.types import Macros
from strikt.proactive.stats import SevenDays
from strikt.telegram.copy import resolve_lang, t
from strikt.telegram.render import fmt_num


def _row(label: str, avg: float | None, target: float, unit: str = "") -> str:
    tail = f" / {fmt_num(target)}" if target > 0 else ""
    return f"<code>{label:<5}{fmt_num(avg or 0):>6}{tail}{unit}</code>"


def render_week(week: SevenDays, targets: Macros, lang: str | None) -> str:
    lang = resolve_lang(lang)
    if week.days_logged == 0:
        return t(lang, "week.empty")
    return "\n".join(
        [
            t(lang, "week.head", logged=week.days_logged, days=week.days),
            t(lang, "week.avg"),
            _row("kcal", week.avg_kcal, targets.kcal),
            _row("P", week.avg_protein_g, targets.protein_g, "g"),
            _row("C", week.avg_carbs_g, targets.carbs_g, "g"),
            _row("F", week.avg_fat_g, targets.fat_g, "g"),
        ]
    )
