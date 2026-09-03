"""The Today card renders compactly; helpers behave."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from strikt.core.types import (
    DayState,
    DayTotals,
    Macros,
    MealItemView,
    MealView,
    RecoveryView,
    Remaining,
    SleepView,
    WorkoutView,
)
from strikt.telegram.render import (
    THIN_SPACE,
    bar,
    escape,
    fmt_num,
    fmt_signed,
    render_day_card,
    split_message,
)

TARGETS = Macros(kcal=2000, protein_g=210, carbs_g=75, fat_g=105, fiber_g=30)


def _state(*, meals: int = 3, closed: bool = False) -> DayState:
    items = [
        MealItemView(
            id=i,
            name=f"item number {i} with a fairly long name",
            grams=150,
            macros=Macros(kcal=310, protein_g=30, carbs_g=12, fat_g=15, fiber_g=3),
        )
        for i in range(2)
    ]
    meal_macros = Macros(kcal=620, protein_g=60, carbs_g=24, fat_g=30, fiber_g=6)
    meal_views = [
        MealView(
            id=n,
            slot="lunch" if n else "breakfast",
            logged_at=datetime(2026, 9, 3, 5, 10, tzinfo=UTC) + timedelta(hours=n),
            items=items,
            macros=meal_macros,
        )
        for n in range(meals)
    ]
    totals = Macros.zero()
    for _ in range(meals):
        totals = totals + meal_macros
    return DayState(
        date=date(2026, 9, 3),
        totals=DayTotals(macros=totals, items=2 * meals, meals=meals),
        targets=TARGETS,
        remaining=Remaining.from_targets(TARGETS, totals),
        meals=meal_views,
        workouts=[
            WorkoutView(
                id=1,
                sport="run",
                started_at=datetime(2026, 9, 3, 14, tzinfo=UTC),
                duration_min=45,
                strain=12.1,
                kcal=406,
                avg_hr=130,
                source="whoop",
            )
        ],
        sleep=SleepView(
            started_at=datetime(2026, 9, 2, 21, tzinfo=UTC),
            ended_at=datetime(2026, 9, 3, 4, tzinfo=UTC),
            asleep_min=370,
            performance_pct=78,
        ),
        recovery=RecoveryView(date=date(2026, 9, 3), score=61),
        measurements_due=["waist (16 d)"],
        closed=closed,
        flags=["salty"],
        verdict="Closed at 1 860 / 180 P. Bed by 00:30." if closed else None,
    )


def test_card_is_compact_in_both_languages() -> None:
    for lang in ("ru", "en"):
        text = render_day_card(_state(), lang, tz="Asia/Dubai")
        assert len(text) < 1000, (lang, len(text), text)
        assert "<code>" in text and "▓" in text and "░" in text
        assert "kcal" in text
        assert text.count("\n") < 25


def test_card_with_many_meals_and_closed_stays_under_limit() -> None:
    text = render_day_card(_state(meals=12, closed=True), "en", tz="Asia/Dubai")
    assert len(text) < 1000
    assert "more" in text
    assert "closed" in text and "Verdict" in text


def test_card_shows_remaining_or_over() -> None:
    under = render_day_card(_state(meals=1), "en")
    assert "Left:" in under
    over = render_day_card(_state(meals=4), "en")
    assert "Over by" in over


def test_card_escapes_html_in_names() -> None:
    state = _state(meals=1)
    state.meals[0].items[0].name = "<b>bad</b> & co"
    text = render_day_card(state, "en")
    assert "<b>bad</b>" not in text and "&lt;b&gt;" in text and "&amp;" in text


def test_card_local_times_and_ru_labels() -> None:
    text = render_day_card(_state(meals=1), "ru", tz="Asia/Dubai")
    assert "09:10 завтрак" in text
    assert "Сегодня · чт 3 сен" in text


def test_fmt_num_thin_space_and_sign() -> None:
    assert fmt_num(1240) == f"1{THIN_SPACE}240"
    assert fmt_num(999) == "999"
    assert fmt_num(-1500) == f"-1{THIN_SPACE}500"
    assert fmt_num(12.06, 1) == "12.1"
    assert fmt_signed(12) == "+12" and fmt_signed(-3) == "-3" and fmt_signed(0) == "0"


def test_bar_clamps() -> None:
    assert bar(0, 100) == "░" * 8
    assert bar(50, 100) == "▓▓▓▓░░░░"
    assert bar(500, 100) == "▓" * 8
    assert bar(10, 0) == "░" * 8


def test_split_message_on_line_boundaries() -> None:
    lines = [f"line {i} " + "x" * 90 for i in range(60)]
    text = "\n".join(lines)
    parts = split_message(text, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert "\n".join(parts) == text
    assert split_message("short") == ["short"]
    huge = "y" * 2500
    assert [len(p) for p in split_message(huge, limit=1000)] == [1000, 1000, 500]


def test_escape() -> None:
    assert escape('a < b & c > d "q"') == 'a &lt; b &amp; c &gt; d "q"'
