"""``memory.periods``: ru/en temporal expressions → inclusive local date ranges."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from strikt.memory.periods import find_period, parse_period, strip_period

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=ZoneInfo("Asia/Dubai"))  # Thursday
TODAY = date(2026, 9, 3)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what did I eat today", (TODAY, TODAY)),
        ("что я ел сегодня", (TODAY, TODAY)),
        ("yesterday's total?", (date(2026, 9, 2), date(2026, 9, 2))),
        ("сколько было вчера", (date(2026, 9, 2), date(2026, 9, 2))),
        ("the day before yesterday", (date(2026, 9, 1), date(2026, 9, 1))),
        ("позавчера обед", (date(2026, 9, 1), date(2026, 9, 1))),
        ("this week protein", (date(2026, 8, 31), TODAY)),
        ("на этой неделе", (date(2026, 8, 31), TODAY)),
        ("last week strain", (date(2026, 8, 24), date(2026, 8, 30))),
        ("на прошлой неделе", (date(2026, 8, 24), date(2026, 8, 30))),
        ("за прошлую неделю", (date(2026, 8, 24), date(2026, 8, 30))),
        ("average over the last 7 days", (date(2026, 8, 28), TODAY)),
        ("за последние 3 дня", (date(2026, 9, 1), TODAY)),
        ("за 10 дней", (date(2026, 8, 25), TODAY)),
        ("past two weeks", (date(2026, 8, 21), TODAY)),
        ("3 days ago", (date(2026, 8, 31), date(2026, 8, 31))),
        ("5 дней назад", (date(2026, 8, 29), date(2026, 8, 29))),
        ("a week ago", (date(2026, 8, 27), date(2026, 8, 27))),
        ("this month kcal", (date(2026, 9, 1), TODAY)),
        ("в этом месяце", (date(2026, 9, 1), TODAY)),
        ("last month", (date(2026, 8, 1), date(2026, 8, 31))),
        ("в прошлом месяце", (date(2026, 8, 1), date(2026, 8, 31))),
        ("за прошлый месяц", (date(2026, 8, 1), date(2026, 8, 31))),
        ("what did I eat last Tuesday", (date(2026, 9, 1), date(2026, 9, 1))),
        ("on Thursday", (date(2026, 8, 27), date(2026, 8, 27))),  # today is Thursday → last one
        ("last thu", (date(2026, 8, 27), date(2026, 8, 27))),
        ("Wednesday dinner", (date(2026, 9, 2), date(2026, 9, 2))),
        ("что я ел во вторник", (date(2026, 9, 1), date(2026, 9, 1))),
        ("в прошлую пятницу", (date(2026, 8, 28), date(2026, 8, 28))),
        ("в среду", (date(2026, 9, 2), date(2026, 9, 2))),
        ("в четверг", (date(2026, 8, 27), date(2026, 8, 27))),
        ("3 September", (date(2026, 9, 3), date(2026, 9, 3))),
        ("on 30 August", (date(2026, 8, 30), date(2026, 8, 30))),
        ("September 1", (date(2026, 9, 1), date(2026, 9, 1))),
        ("Sept 1st, 2025", (date(2025, 9, 1), date(2025, 9, 1))),
        ("3 сентября", (date(2026, 9, 3), date(2026, 9, 3))),
        ("28 августа", (date(2026, 8, 28), date(2026, 8, 28))),
        ("1-го сентября", (date(2026, 9, 1), date(2026, 9, 1))),
        ("03.09", (date(2026, 9, 3), date(2026, 9, 3))),
        ("28.08.2026", (date(2026, 8, 28), date(2026, 8, 28))),
        ("2026-09-01 lunch", (date(2026, 9, 1), date(2026, 9, 1))),
        ("15 September", (date(2025, 9, 15), date(2025, 9, 15))),  # future → last year
        ("in August", (date(2026, 8, 1), date(2026, 8, 31))),
        ("в августе", (date(2026, 8, 1), date(2026, 8, 31))),
    ],
)
def test_parse_period(text: str, expected: tuple[date, date]) -> None:
    assert parse_period(text, now_local=NOW, lang="ru") == expected


@pytest.mark.parametrize(
    "text",
    [
        "how much protein is in cottage cheese",
        "200 g cottage cheese 0.5%, 160 g yogurt",
        "2.5 kg dumbbells",
        "sunny day, sat on the couch",  # no bare abbreviations without last/on
        "",
    ],
)
def test_no_period(text: str) -> None:
    assert parse_period(text, now_local=NOW, lang="en") is None


def test_find_period_span_and_strip() -> None:
    match = find_period("what did I eat last Tuesday at Kinoya", now_local=NOW)
    assert match is not None
    assert match.label == "weekday"
    assert match.range == (date(2026, 9, 1), date(2026, 9, 1))
    assert (
        strip_period("what did I eat last Tuesday at Kinoya", match) == "what did I eat at Kinoya"
    )


def test_explicit_date_beats_relative_word() -> None:
    match = find_period("yesterday I logged for 28.08.2026", now_local=NOW)
    assert match is not None
    assert match.range == (date(2026, 8, 28), date(2026, 8, 28))


def test_invalid_explicit_date_is_skipped() -> None:
    assert parse_period("31.09", now_local=NOW) is None
    assert parse_period("2026-13-40", now_local=NOW) is None


def test_last_month_across_year_boundary() -> None:
    jan = datetime(2027, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    assert parse_period("last month", now_local=jan) == (date(2026, 12, 1), date(2026, 12, 31))
    assert parse_period("in December", now_local=jan) == (date(2026, 12, 1), date(2026, 12, 31))
