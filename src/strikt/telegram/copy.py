"""Code-rendered strings (card, buttons, errors, /start, /forget_me) in ru and en.

The coach's replies are model-written; only what the *code* renders lives here. Keyed by the
user's language (``ru`` or anything else → ``en``). Keep lines short: they are read on a phone.
"""

from __future__ import annotations

from typing import Any

Lang = str

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # Today card
        "card.title": "Today · {date}",
        "card.closed": "closed",
        "card.remaining": "Left: {kcal} kcal · {p} P · {c} C · {f} F",
        "card.over": "Over by {kcal} kcal · P {p} · C {c} · F {f}",
        "card.meals": "Meals",
        "card.no_meals": "nothing logged yet",
        "card.more_meals": "+{n} more",
        "card.training": "Training",
        "card.sleep": "Sleep",
        "card.recovery": "recovery",
        "card.due": "Due",
        "card.flags": "Flags",
        "card.verdict": "Verdict",
        "card.plan": "Plan",
        "card.no_protocol": "no protocol yet — finish onboarding",
        "card.slot.breakfast": "breakfast",
        "card.slot.lunch": "lunch",
        "card.slot.dinner": "dinner",
        "card.slot.snack": "snack",
        "card.slot.unknown": "meal",
        # Buttons
        "btn.breakfast": "Breakfast",
        "btn.lunch": "Lunch",
        "btn.dinner": "Dinner",
        "btn.snack": "Snack",
        "btn.undo": "Undo",
        "btn.recalc": "Recalculate",
        "btn.close": "Close day",
        "btn.yes": "Yes",
        "btn.no": "No",
        "btn.forget_confirm": "Yes, delete everything",
        "btn.cancel": "Cancel",
        # Errors (honest, one line)
        "err.llm_down": "Claude is unavailable. I'll retry in a minute — your message is kept.",
        "err.tool_failed": "Couldn't verify — estimating from ingredients. Correct me if you know better.",
        "err.transcribe": "Voice transcription is off. Send text.",
        "err.media": "Couldn't read that file. Send a photo, PDF or text.",
        "err.too_large": "File too large (limit {mb} MB).",
        "err.not_allowed": "This coach is invite-only. Ask the owner for a code and send /start <code>.",
        "err.invite_invalid": "That invite code is not valid.",
        "err.unknown": "Something broke on my side. Send that again.",
        # /start
        "start.welcome": "Strikt. One window, no settings. Send food photos, screenshots, voice or text — I log, count and push.",
        "start.onboarding": "First a short interview, ten questions, resumable. Your name?",
        "start.resume": "Back. Where we left off:",
        "start.invite_ok": "Invite accepted.",
        # /today
        "today.reposted": "Card re-posted.",
        # /forget_me
        "forget.question": "Delete everything about you — profile, meals, training, notes, chat history? This cannot be undone.",
        "forget.done": "Deleted {rows} rows. Nothing about you remains. Send /start to begin again.",
        "forget.cancelled": "Kept everything.",
        # admin
        "invite.created": "Invite code: {code}",
        # misc
        "queue.busy": "Still on your previous message — answering in order.",
    },
    "ru": {
        "card.title": "Сегодня · {date}",
        "card.closed": "закрыт",
        "card.remaining": "Осталось: {kcal} ккал · {p} Б · {c} У · {f} Ж",
        "card.over": "Перебор: {kcal} ккал · Б {p} · У {c} · Ж {f}",
        "card.meals": "Еда",
        "card.no_meals": "пока ничего не записано",
        "card.more_meals": "+{n} ещё",
        "card.training": "Тренировка",
        "card.sleep": "Сон",
        "card.recovery": "восстановление",
        "card.due": "Пора",
        "card.flags": "Флаги",
        "card.verdict": "Вердикт",
        "card.plan": "План",
        "card.no_protocol": "протокола ещё нет — закончи онбординг",
        "card.slot.breakfast": "завтрак",
        "card.slot.lunch": "обед",
        "card.slot.dinner": "ужин",
        "card.slot.snack": "перекус",
        "card.slot.unknown": "приём",
        "btn.breakfast": "Завтрак",
        "btn.lunch": "Обед",
        "btn.dinner": "Ужин",
        "btn.snack": "Перекус",
        "btn.undo": "Отменить",
        "btn.recalc": "Пересчитать",
        "btn.close": "Закрыть день",
        "btn.yes": "Да",
        "btn.no": "Нет",
        "btn.forget_confirm": "Да, удалить всё",
        "btn.cancel": "Отмена",
        "err.llm_down": "Claude недоступен. Повторю через минуту — сообщение сохранено.",
        "err.tool_failed": "Не смог проверить — считаю по ингредиентам. Поправь, если знаешь точнее.",
        "err.transcribe": "Распознавание голоса выключено. Напиши текстом.",
        "err.media": "Не смог прочитать файл. Пришли фото, PDF или текст.",
        "err.too_large": "Файл слишком большой (лимит {mb} МБ).",
        "err.not_allowed": "Доступ по приглашению. Возьми код у владельца и отправь /start <код>.",
        "err.invite_invalid": "Код приглашения не подходит.",
        "err.unknown": "У меня что-то сломалось. Отправь ещё раз.",
        "start.welcome": "Strikt. Одно окно, без настроек. Присылай фото еды, скриншоты, голос или текст — я записываю, считаю и подгоняю.",
        "start.onboarding": "Сначала короткое интервью, десять вопросов, можно прерываться. Как тебя зовут?",
        "start.resume": "С возвращением. Остановились здесь:",
        "start.invite_ok": "Приглашение принято.",
        "today.reposted": "Карточка обновлена.",
        "forget.question": "Удалить всё о тебе — профиль, еду, тренировки, заметки, историю чата? Отменить нельзя.",
        "forget.done": "Удалено строк: {rows}. О тебе ничего не осталось. /start — начать заново.",
        "forget.cancelled": "Оставил всё как есть.",
        "invite.created": "Код приглашения: {code}",
        "queue.busy": "Ещё обрабатываю предыдущее сообщение — отвечу по порядку.",
    },
}

WEEKDAYS: dict[str, tuple[str, ...]] = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "ru": ("пн", "вт", "ср", "чт", "пт", "сб", "вс"),
}
MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ru": ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"),
}


def resolve_lang(code: str | None) -> Lang:
    """``ru``/``ru-RU``/``be``… → ``ru``; everything else → ``en``."""
    if not code:
        return "en"
    lowered = code.lower()
    return "ru" if lowered.startswith(("ru", "be", "uk", "kk")) else "en"


def t(lang: str | None, key: str, **kwargs: Any) -> str:
    """Translate ``key`` for ``lang`` with ``str.format`` args; falls back to en, then the key."""
    table = STRINGS.get(resolve_lang(lang), STRINGS["en"])
    template = table.get(key) or STRINGS["en"].get(key) or key
    return template.format(**kwargs) if kwargs else template


def weekday_name(lang: str | None, weekday: int) -> str:
    return WEEKDAYS[resolve_lang(lang)][weekday % 7]


def month_name(lang: str | None, month: int) -> str:
    return MONTHS[resolve_lang(lang)][(month - 1) % 12]
