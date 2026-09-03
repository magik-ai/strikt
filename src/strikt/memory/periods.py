"""Temporal expressions in Russian and English → an inclusive local date range.

Understood: today, yesterday, day before yesterday, this/last week, last N days, N days ago,
this/last month, weekday names ("last Tuesday", "во вторник"), explicit dates (3 September,
03.09, 03.09.2026, 2026-09-03). Everything resolves relative to ``now_local``; a date without
a year is the most recent occurrence not after today. ``find_period`` also returns the matched
span so the caller can strip the phrase from a search query (research/07 §2.5: time-aware
query expansion).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from strikt.core.clock import week_start

_MONTHS_EN: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
# Russian months by stem (nominative and genitive share it): "сентябрь" / "сентября".
_MONTHS_RU: tuple[tuple[str, int], ...] = (
    ("январ", 1),
    ("феврал", 2),
    ("март", 3),
    ("апрел", 4),
    ("ма[йя]", 5),
    ("июн", 6),
    ("июл", 7),
    ("август", 8),
    ("сентябр", 9),
    ("октябр", 10),
    ("ноябр", 11),
    ("декабр", 12),
)
_WEEKDAYS_EN: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}
_WEEKDAYS_RU: tuple[tuple[str, int], ...] = (
    ("понедельник[а]?", 0),
    ("вторник[а]?", 1),
    ("сред[аеуы]", 2),
    ("четверг[а]?", 3),
    ("пятниц[аеуы]", 4),
    ("суббот[аеуы]", 5),
    ("воскресень[ея]", 6),
)

_MONTH_EN_RE = "|".join(sorted(_MONTHS_EN, key=len, reverse=True))
_MONTH_RU_RE = "|".join(f"(?:{stem}[а-я]*)" for stem, _ in _MONTHS_RU)
_WD_EN_FULL = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_WD_EN_ANY = "|".join(sorted(_WEEKDAYS_EN, key=len, reverse=True))
_WD_RU = "|".join(pattern for pattern, _ in _WEEKDAYS_RU)

Range = tuple[date, date]
Handler = Callable[["re.Match[str]", date], Range | None]


@dataclass(frozen=True)
class PeriodMatch:
    start: date
    end: date
    span: tuple[int, int]
    label: str

    @property
    def range(self) -> Range:
        return (self.start, self.end)


# ---------------------------------------------------------------------------------- helpers


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _most_recent(month: int, day: int, today: date) -> date | None:
    """The latest ``day.month`` that is not after ``today`` (this year, else last year)."""
    candidate = _valid(today.year, month, day)
    if candidate is None or candidate > today:
        candidate = _valid(today.year - 1, month, day)
    return candidate


def _month_ru(word: str) -> int | None:
    for stem, number in _MONTHS_RU:
        if re.match(stem, word, re.IGNORECASE):
            return number
    return None


def _weekday_ru_number(word: str) -> int | None:
    for pattern, number in _WEEKDAYS_RU:
        if re.fullmatch(pattern, word, re.IGNORECASE):
            return number
    return None


def _year(text: str | None, today: date) -> int | None:
    if not text:
        return None
    value = int(text)
    if value < 100:
        value += 2000
    return value


def _single(day: date | None) -> Range | None:
    return None if day is None else (day, day)


def _prev_weekday(weekday: int, today: date) -> date:
    delta = (today.weekday() - weekday) % 7
    return today - timedelta(days=delta or 7)


def _last_month(today: date) -> Range:
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return (last_prev.replace(day=1), last_prev)


# ---------------------------------------------------------------------------------- handlers


def _iso(m: re.Match[str], today: date) -> Range | None:
    return _single(_valid(int(m.group(1)), int(m.group(2)), int(m.group(3))))


def _dotted(m: re.Match[str], today: date) -> Range | None:
    day, month = int(m.group(1)), int(m.group(2))
    year = _year(m.group(3), today)
    if year is not None:
        return _single(_valid(year, month, day))
    return _single(_most_recent(month, day, today))


def _day_month_en(m: re.Match[str], today: date) -> Range | None:
    day, month = int(m.group(1)), _MONTHS_EN[m.group(2).lower()]
    year = _year(m.group(3), today)
    return _single(_valid(year, month, day) if year else _most_recent(month, day, today))


def _month_day_en(m: re.Match[str], today: date) -> Range | None:
    month, day = _MONTHS_EN[m.group(1).lower()], int(m.group(2))
    year = _year(m.group(3), today)
    return _single(_valid(year, month, day) if year else _most_recent(month, day, today))


def _day_month_ru(m: re.Match[str], today: date) -> Range | None:
    month = _month_ru(m.group(2))
    if month is None:
        return None
    day = int(m.group(1))
    year = _year(m.group(3), today)
    return _single(_valid(year, month, day) if year else _most_recent(month, day, today))


def _days_back(n: int) -> Handler:
    return lambda m, today: (today - timedelta(days=n), today - timedelta(days=n))


def _last_n_days(m: re.Match[str], today: date) -> Range | None:
    n = int(next(g for g in m.groups() if g))
    if n <= 0 or n > 3660:
        return None
    return (today - timedelta(days=n - 1), today)


def _n_days_ago(m: re.Match[str], today: date) -> Range | None:
    n = int(next(g for g in m.groups() if g))
    if n < 0 or n > 3660:
        return None
    return _single(today - timedelta(days=n))


def _this_week(m: re.Match[str], today: date) -> Range | None:
    return (week_start(today), today)


def _last_week(m: re.Match[str], today: date) -> Range | None:
    start = week_start(today) - timedelta(days=7)
    return (start, start + timedelta(days=6))


def _this_month(m: re.Match[str], today: date) -> Range | None:
    return (today.replace(day=1), today)


def _last_month_h(m: re.Match[str], today: date) -> Range | None:
    return _last_month(today)


def _month_name_only_en(m: re.Match[str], today: date) -> Range | None:
    """"in August" → the whole most recent August that has started."""
    month = _MONTHS_EN[m.group(1).lower()]
    year = _year(m.group(2), today) or (today.year if month <= today.month else today.year - 1)
    last = calendar.monthrange(year, month)[1]
    end = date(year, month, last)
    return (date(year, month, 1), min(end, today) if year == today.year else end)


def _month_name_only_ru(m: re.Match[str], today: date) -> Range | None:
    month = _month_ru(m.group(1))
    if month is None:
        return None
    year = _year(m.group(2), today) or (today.year if month <= today.month else today.year - 1)
    last = calendar.monthrange(year, month)[1]
    end = date(year, month, last)
    return (date(year, month, 1), min(end, today) if year == today.year else end)


def _weekday_en(m: re.Match[str], today: date) -> Range | None:
    return _single(_prev_weekday(_WEEKDAYS_EN[m.group(1).lower()], today))


def _weekday_ru(m: re.Match[str], today: date) -> Range | None:
    weekday = _weekday_ru_number(m.group(1))
    return None if weekday is None else _single(_prev_weekday(weekday, today))


def _last_two_weeks(m: re.Match[str], today: date) -> Range | None:
    return (today - timedelta(days=13), today)


_F = re.IGNORECASE | re.UNICODE

# Order matters: explicit dates first, then the longer phrases before the words they contain.
PATTERNS: tuple[tuple[str, re.Pattern[str], Handler], ...] = (
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b", _F), _iso),
    (
        "dotted",
        re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\d.%])", _F),
        _dotted,
    ),
    ("dotted", re.compile(r"(?<![\d.,])(\d{2})\.(\d{2})(?![\d.,%])()", _F), _dotted),
    (
        "day_month",
        re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_EN_RE})\b\.?(?:\s+(\d{{4}}))?", _F),
        _day_month_en,
    ),
    (
        "month_day",
        re.compile(rf"\b({_MONTH_EN_RE})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", _F),
        _month_day_en,
    ),
    (
        "day_month",
        re.compile(rf"\b(\d{{1,2}})(?:-?го)?\s+({_MONTH_RU_RE})\b(?:\s+(\d{{4}}))?", _F),
        _day_month_ru,
    ),
    ("day", re.compile(r"\b(?:the\s+)?day\s+before\s+yesterday\b", _F), _days_back(2)),
    ("day", re.compile(r"\bпозавчера\b", _F), _days_back(2)),
    ("day", re.compile(r"\byesterday\b", _F), _days_back(1)),
    ("day", re.compile(r"\bвчера\b", _F), _days_back(1)),
    ("day", re.compile(r"\btoday\b", _F), _days_back(0)),
    ("day", re.compile(r"\bсегодня\b", _F), _days_back(0)),
    (
        "last_n_days",
        re.compile(r"\b(?:last|past|previous)\s+(\d+)\s+days?\b", _F),
        _last_n_days,
    ),
    (
        "last_n_days",
        re.compile(r"\b(?:за\s+)?(?:последни[ех]|прошл[ыо][ех])\s+(\d+)\s+дн(?:я|ей)\b", _F),
        _last_n_days,
    ),
    ("last_n_days", re.compile(r"\bза\s+(\d+)\s+дн(?:я|ей)\b", _F), _last_n_days),
    ("last_n_days", re.compile(r"\b(?:last|past)\s+(?:two|2)\s+weeks\b", _F), _last_two_weeks),
    ("last_n_days", re.compile(r"\b(?:за\s+)?(?:последние|прошлые)\s+(?:две|2)\s+недели\b", _F), _last_two_weeks),
    ("n_days_ago", re.compile(r"\b(\d+)\s+days?\s+ago\b", _F), _n_days_ago),
    ("n_days_ago", re.compile(r"\b(\d+)\s+дн(?:я|ей)\s+назад\b", _F), _n_days_ago),
    ("n_days_ago", re.compile(r"\b(?:a\s+)?week\s+ago\b", _F), _days_back(7)),
    ("n_days_ago", re.compile(r"\bнеделю\s+назад\b", _F), _days_back(7)),
    ("this_week", re.compile(r"\bthis\s+week\b", _F), _this_week),
    (
        "this_week",
        re.compile(r"\b(?:на\s+)?(?:эт(?:а|у|ой)\s+недел[яеию]|этой\s+неделе)\b", _F),
        _this_week,
    ),
    ("last_week", re.compile(r"\b(?:last|past|previous)\s+week\b", _F), _last_week),
    (
        "last_week",
        re.compile(r"\b(?:на\s+|за\s+)?прошл(?:ая|ую|ой)\s+недел[яеию]\b", _F),
        _last_week,
    ),
    ("this_month", re.compile(r"\bthis\s+month\b", _F), _this_month),
    ("this_month", re.compile(r"\b(?:в\s+|за\s+)?эт(?:от|ом)\s+месяц[ае]?\b", _F), _this_month),
    ("last_month", re.compile(r"\b(?:last|past|previous)\s+month\b", _F), _last_month_h),
    (
        "last_month",
        re.compile(r"\b(?:в\s+|за\s+)?прошл(?:ый|ом)\s+месяц[ае]?\b", _F),
        _last_month_h,
    ),
    (
        "weekday",
        re.compile(rf"\b(?:last|this|on|since)\s+({_WD_EN_ANY})\b", _F),
        _weekday_en,
    ),
    ("weekday", re.compile(rf"\b({_WD_EN_FULL})\b", _F), _weekday_en),
    (
        "weekday",
        re.compile(rf"\b(?:(?:в|во)\s+)?(?:прошл(?:ый|ую|ое|ой)\s+)?({_WD_RU})\b", _F),
        _weekday_ru,
    ),
    (
        "month",
        re.compile(rf"\b(?:in|during|for)\s+({_MONTH_EN_RE})\b(?:\s+(\d{{4}}))?", _F),
        _month_name_only_en,
    ),
    (
        "month",
        re.compile(rf"\b(?:в|за)\s+({_MONTH_RU_RE})\b(?:\s+(\d{{4}}))?", _F),
        _month_name_only_ru,
    ),
)


def find_period(text: str, *, now_local: datetime, lang: str | None = None) -> PeriodMatch | None:
    """First temporal expression found in ``text`` (both languages are always tried)."""
    today = now_local.date()
    for label, pattern, handler in PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        result = handler(m, today)
        if result is None:
            continue
        start, end = result
        if start > end:
            start, end = end, start
        return PeriodMatch(start=start, end=end, span=m.span(), label=label)
    return None


def parse_period(text: str, *, now_local: datetime, lang: str | None = None) -> Range | None:
    """``(date_from, date_to)`` inclusive, or None when the text has no temporal expression."""
    match = find_period(text, now_local=now_local, lang=lang)
    return None if match is None else match.range


def strip_period(text: str, match: PeriodMatch) -> str:
    """The text without the matched phrase (whitespace collapsed)."""
    start, end = match.span
    return " ".join((text[:start] + " " + text[end:]).split())
