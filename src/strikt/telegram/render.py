"""HTML rendering for Telegram: escaping, numbers, message splitting and the Today card."""

from __future__ import annotations

import html
from datetime import date, datetime
from typing import TYPE_CHECKING

from strikt.core.clock import ensure_utc, to_local
from strikt.telegram.copy import month_name, resolve_lang, t, weekday_name

if TYPE_CHECKING:
    from strikt.core.types import DayState, MealView

THIN_SPACE = " "
TELEGRAM_LIMIT = 4096
BAR_WIDTH = 8
FILLED = "▓"
EMPTY = "░"
MAX_CARD_MEALS = 8
MAX_ITEM_NAME = 20
MAX_ITEMS_PER_MEAL = 3
CARD_MAX_CHARS = 1000


def escape(text: str) -> str:
    """Escape ``<``, ``>`` and ``&`` for Telegram HTML (quotes are fine unescaped)."""
    return html.escape(text, quote=False)


def fmt_num(value: float, decimals: int = 0) -> str:
    """``1240`` → ``1 240`` (thin space thousands separator); negatives keep their sign."""
    rounded = round(value, decimals)
    if decimals == 0:
        rounded = round(rounded)
    text = f"{abs(rounded):,.{decimals}f}".replace(",", THIN_SPACE)
    return f"-{text}" if rounded < 0 else text


def fmt_signed(value: float, decimals: int = 0) -> str:
    text = fmt_num(abs(value), decimals)
    if round(value, decimals) == 0:
        return text
    return f"+{text}" if value > 0 else f"-{text}"


def bar(value: float, target: float, width: int = BAR_WIDTH) -> str:
    """Progress bar ``▓▓▓░░░░░``; full when over target; empty when the target is 0."""
    if target <= 0:
        return EMPTY * width
    ratio = max(0.0, min(1.0, value / target))
    filled = round(ratio * width)
    return FILLED * filled + EMPTY * (width - filled)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so no part exceeds ``limit``; over-long lines are hard-split."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        rest = line
        while len(rest) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(rest[:limit])
            rest = rest[limit:]
        candidate = rest if not current else f"{current}\n{rest}"
        if len(candidate) > limit:
            parts.append(current)
            current = rest
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def fmt_date(day: date, lang: str | None) -> str:
    return f"{weekday_name(lang, day.weekday())} {day.day} {month_name(lang, day.month)}"


def fmt_time(dt: datetime, tz: str) -> str:
    return to_local(ensure_utc(dt), tz).strftime("%H:%M")


def _short(name: str, limit: int = MAX_ITEM_NAME) -> str:
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _macro_line(label: str, value: float, target: float, unit: str = "") -> str:
    left = f"{label:<5}{fmt_num(value):>6} /{fmt_num(target):>6}{unit}"
    return f"<code>{left}  {bar(value, target)}</code>"


def _meal_line(meal: MealView, lang: str, tz: str) -> str:
    shown = [_short(item.name) for item in meal.items[:MAX_ITEMS_PER_MEAL]]
    if len(meal.items) > MAX_ITEMS_PER_MEAL:
        shown.append(f"+{len(meal.items) - MAX_ITEMS_PER_MEAL}")
    names = ", ".join(shown) or "—"
    slot = t(lang, f"card.slot.{meal.slot}")
    when = fmt_time(meal.eaten_at or meal.logged_at, tz)
    return f"• {when} {slot} — {escape(names)} · {fmt_num(meal.macros.kcal)}"


def render_day_card(state: DayState, lang: str | None, tz: str = "UTC") -> str:
    """The pinned Today card: targets vs totals as bars, meals, training, sleep, dues, flags.

    Compact by design (phone, three seconds): under ~1000 characters for a normal day.
    """
    lang = resolve_lang(lang)
    title = t(lang, "card.title", date=fmt_date(state.date, lang))
    if state.closed:
        title += f" · {t(lang, 'card.closed')}"
    lines: list[str] = [f"<b>{escape(title)}</b>"]

    totals = state.totals.macros
    targets = state.targets
    if targets.kcal <= 0:
        lines.append(escape(t(lang, "card.no_protocol")))
    lines.append(_macro_line("kcal", totals.kcal, targets.kcal))
    lines.append(_macro_line("P", totals.protein_g, targets.protein_g, "g"))
    lines.append(_macro_line("C", totals.carbs_g, targets.carbs_g, "g"))
    lines.append(_macro_line("F", totals.fat_g, targets.fat_g, "g"))
    lines.append(_macro_line("fiber", totals.fiber_g, targets.fiber_g, "g"))

    rem = state.remaining
    if targets.kcal > 0:
        key = "card.remaining" if rem.kcal >= 0 else "card.over"
        lines.append(
            escape(
                t(
                    lang,
                    key,
                    kcal=fmt_num(abs(rem.kcal)),
                    p=fmt_signed(rem.protein_g) if rem.kcal < 0 else fmt_num(rem.protein_g),
                    c=fmt_signed(rem.carbs_g) if rem.kcal < 0 else fmt_num(rem.carbs_g),
                    f=fmt_signed(rem.fat_g) if rem.kcal < 0 else fmt_num(rem.fat_g),
                )
            )
        )

    head = lines
    meal_lines = [_meal_line(meal, lang, tz) for meal in state.meals]
    tail: list[str] = []
    lines = tail

    if state.workouts:
        bits = []
        for w in state.workouts:
            parts = [escape(w.sport)]
            if w.duration_min:
                parts.append(f"{fmt_num(w.duration_min)} min")
            if w.strain is not None:
                parts.append(f"strain {fmt_num(w.strain, 1)}")
            if w.kcal:
                parts.append(f"{fmt_num(w.kcal)} kcal")
            if w.avg_hr:
                parts.append(f"avg HR {w.avg_hr}")
            bits.append(" · ".join(parts))
        lines.append(f"<b>{escape(t(lang, 'card.training'))}</b>: " + "; ".join(bits))

    if state.sleep or state.recovery:
        parts = []
        if state.sleep and state.sleep.asleep_min:
            hours, minutes = divmod(int(state.sleep.asleep_min), 60)
            parts.append(f"{hours}h{minutes:02d}")
        if state.sleep and state.sleep.performance_pct is not None:
            parts.append(f"{fmt_num(state.sleep.performance_pct)}%")
        if state.recovery and state.recovery.score is not None:
            parts.append(f"{t(lang, 'card.recovery')} {fmt_num(state.recovery.score)}%")
        if parts:
            lines.append(f"<b>{escape(t(lang, 'card.sleep'))}</b>: " + " · ".join(parts))

    if state.measurements_due:
        lines.append(
            f"<b>{escape(t(lang, 'card.due'))}</b>: " + escape(", ".join(state.measurements_due))
        )
    if state.flags:
        lines.append(f"<b>{escape(t(lang, 'card.flags'))}</b>: " + escape(", ".join(state.flags)))
    if state.closed and state.verdict:
        lines.append(f"<b>{escape(t(lang, 'card.verdict'))}</b>: {escape(state.verdict)}")

    meals_header = [f"<b>{escape(t(lang, 'card.meals'))}</b>"]
    fixed = len("\n".join([*head, "", *meals_header, *tail])) + 1
    body = _fit_meals(meal_lines, lang, budget=CARD_MAX_CHARS - fixed)
    return "\n".join([*head, "", *meals_header, *body, *tail])


def _fit_meals(meal_lines: list[str], lang: str, *, budget: int) -> list[str]:
    """Show at most ``MAX_CARD_MEALS`` meals and drop from the end until the card fits."""
    if not meal_lines:
        return [escape(t(lang, "card.no_meals"))]
    shown = list(meal_lines[:MAX_CARD_MEALS])
    while True:
        hidden = len(meal_lines) - len(shown)
        block = shown + ([escape(t(lang, "card.more_meals", n=hidden))] if hidden else [])
        if len("\n".join(block)) <= budget or len(shown) <= 1:
            return block
        shown.pop()
