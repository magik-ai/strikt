"""Code-rendered strings (card, buttons, errors, /start, /forget_me) in ru and en.

The coach's replies are model-written; only what the *code* renders lives here. Keyed by the
user's language (``ru`` or anything else → ``en``). Keep lines short: they are read on a phone.
"""

from __future__ import annotations

from typing import Any

Lang = str

# Bring-your-own-key walkthrough (brief voice: fact first, no emoji). Shared by ``key.needed``
# (a keyless user's first message, and the second message after /start) and ``key.help`` (a
# keyless user asking about the key). Plain text: no < > & — the messenger sends HTML.
_KEY_STEPS: dict[str, str] = {
    "en": (
        "1. Open console.anthropic.com. Sign in or create an account.\n"
        "2. Billing → add credit. You pay only for what the coach uses.\n"
        "3. Settings → API keys → Create key. Name it strikt, copy the key (it starts with sk-ant-).\n"
        "4. Paste the key here as a message.\n"
        "I check it, store it encrypted, delete your message and never show it again.\n"
        "A new key replaces the old one. /forget_me deletes it with everything else."
    ),
    "ru": (
        "1. Открой console.anthropic.com. Войди или создай аккаунт.\n"
        "2. Billing → пополни баланс. Платишь только за то, что тренер израсходовал.\n"
        "3. Settings → API keys → Create key. Назови strikt, скопируй ключ (начинается с sk-ant-).\n"
        "4. Вставь ключ сюда сообщением.\n"
        "Я проверю его, сохраню в зашифрованном виде, удалю твоё сообщение и больше не покажу.\n"
        "Новый ключ заменяет старый. /forget_me удаляет его вместе со всем остальным."
    ),
}

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
        "card.paused": "targets paused — sick day",
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
        "btn.undo_done": "Already removed",
        "btn.recalc": "Recalculate",
        "btn.close": "Close day",
        "btn.yes": "Yes",
        "btn.no": "No",
        "btn.forget_confirm": "Yes, delete everything",
        "btn.cancel": "Cancel",
        # Errors (honest, one line)
        "err.llm_down": "Claude is unavailable right now. Your message is saved — send the next one and I will pick both up.",
        "err.tool_failed": "Couldn't verify — estimating from ingredients. Correct me if you know better.",
        "err.transcribe": "Voice transcription is off. Send text.",
        "err.transcribe_failed": "Couldn't transcribe that. Send text or try again.",
        "err.media": "Couldn't read that file. Send a photo, PDF or text.",
        "err.too_large": "File too large (limit {mb} MB).",
        # HTML parse mode: the placeholder must be escaped or Telegram rejects the message.
        "err.not_allowed": "This coach is invite-only. Ask the owner for a code and send /start &lt;code&gt;.",
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
        "forget.question": "Delete everything about you — profile, meals, training, notes, chat history, your API key? This cannot be undone.",
        "forget.done": "Deleted {rows} rows, your API key included. Nothing about you remains. Send /start to begin again.",
        "forget.cancelled": "Kept everything.",
        # the user's Anthropic key (bring-your-own-key; code-rendered, the model never sees a key)
        "key.needed": (
            "This coach runs on your own Anthropic API key. The coach is free; Anthropic bills the key for what it uses.\n"
            + _KEY_STEPS["en"]
        ),
        "key.help": "The key, step by step:\n" + _KEY_STEPS["en"],
        "key.saved": "Key saved, ends in …{last4}. Your message with the key is deleted.",
        "key.saved_keep": "Key saved, ends in …{last4}. I could not delete your message with the key — delete it yourself.",
        "key.unchecked": "Anthropic did not answer the check; the key is checked on your next message.",
        "key.invalid": "Anthropic rejected this key. Usual causes: a key from another console account, or an account with no credit. Redo step 3 — console.anthropic.com → Settings → API keys → Create key — top up Billing if needed, and send the new key here.",
        "key.rejected": "Anthropic rejected your key; nothing was sent. Create a new key at console.anthropic.com (Settings → API keys), check that Billing has credit, and paste it here.",
        # admin
        "invite.created": "Invite code: {code}",
        # misc
        "queue.busy": "Still on your previous message — answering in order.",
        # synthetic user messages behind the inline buttons (persisted as the user's turn)
        "synthetic.recalc": "Recalculate the day.",
        "synthetic.close": "Close the day.",
        # bot profile (setMyCommands / setMyDescription / setMyShortDescription)
        # The four strings below are BRAND.md section 9 verbatim; tests/test_brand_copy.py fails if
        # the document and the code drift apart. Limits: command 256, short 120, description 512.
        "cmd.start": "Begin, or resume where you left off",
        "cmd.today": "Re-post the Today card",
        "cmd.forget_me": "Delete everything about you",
        "bot.short": "A coach in one chat. Send food, get the number. The day ends with a verdict.",
        "bot.description": (
            "Strikt logs food, training, sleep and measurements from one Telegram chat. "
            "Send a photo, a screenshot, a voice note or text; the reply is kcal, protein, "
            "carbs, fat and fiber per item, the day so far and what is left. The Today card "
            "stays pinned and is edited in place. When you go quiet it writes first: the fact, "
            "then the pattern from your own data, then an instruction with a deadline. The day "
            "closes with a verdict. No greetings, no emoji, no praise. Invite-only."
        ),
    },
    "ru": {
        "card.title": "Сегодня · {date}",
        "card.closed": "закрыт",
        # БЖУ — protein · fat · carbs — is the order a Russian reader says and reads; Б·У·Ж would be
        # a transliteration of P·C·F. brand/images/russian-1920x1080.png shows the same order.
        "card.remaining": "Осталось: {kcal} ккал · {p} Б · {f} Ж · {c} У",
        "card.over": "Перебор: {kcal} ккал · Б {p} · Ж {f} · У {c}",
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
        "card.paused": "цели на паузе — день болезни",
        "card.slot.breakfast": "завтрак",
        "card.slot.lunch": "обед",
        "card.slot.dinner": "ужин",
        "card.slot.snack": "перекус",
        "card.slot.unknown": "приём",
        "btn.breakfast": "Завтрак",
        "btn.lunch": "Обед",
        "btn.dinner": "Ужин",
        "btn.snack": "Перекус",
        # «Отменить» is "cancel"; the button takes back a logged meal, so «Убрать».
        "btn.undo": "Убрать",
        "btn.undo_done": "Уже убрано",
        "btn.recalc": "Пересчитать",
        "btn.close": "Закрыть день",
        "btn.yes": "Да",
        "btn.no": "Нет",
        "btn.forget_confirm": "Да, удалить всё",
        "btn.cancel": "Отмена",
        "err.llm_down": "Claude недоступен. Сообщение сохранено — напиши ещё раз, отвечу на оба.",
        "err.tool_failed": "Не смог проверить — считаю по ингредиентам. Поправь, если знаешь точнее.",
        "err.transcribe": "Распознавание голоса выключено. Напиши текстом.",
        "err.transcribe_failed": "Не смог распознать голос. Напиши текстом или пришли ещё раз.",
        "err.media": "Не смог прочитать файл. Пришли фото, PDF или текст.",
        "err.too_large": "Файл слишком большой (лимит {mb} МБ).",
        "err.not_allowed": "Доступ по приглашению. Возьми код у владельца и отправь /start &lt;код&gt;.",
        "err.invite_invalid": "Код приглашения не подходит.",
        "err.unknown": "У меня что-то сломалось. Отправь ещё раз.",
        "start.welcome": "Strikt. Одно окно, без настроек. Присылай фото еды, скриншоты, голос или текст — я записываю, считаю и подгоняю.",
        "start.onboarding": "Сначала короткое интервью, десять вопросов, можно прерываться. Как тебя зовут?",
        "start.resume": "С возвращением. Остановились здесь:",
        "start.invite_ok": "Приглашение принято.",
        "today.reposted": "Карточка обновлена.",
        "forget.question": "Удалить всё о тебе — профиль, еду, тренировки, заметки, историю чата, твой API-ключ? Отменить нельзя.",
        "forget.done": "Удалено строк: {rows}. API-ключ удалён вместе с ними. О тебе ничего не осталось. /start — начать заново.",
        "forget.cancelled": "Оставил всё как есть.",
        "key.needed": (
            "Тренер работает на твоём ключе Anthropic. Сам тренер бесплатный; Anthropic списывает с ключа за то, что он потратил.\n"
            + _KEY_STEPS["ru"]
        ),
        "key.help": "Ключ, по шагам:\n" + _KEY_STEPS["ru"],
        "key.saved": "Ключ сохранён, заканчивается на …{last4}. Твоё сообщение с ключом удалено.",
        "key.saved_keep": "Ключ сохранён, заканчивается на …{last4}. Удалить твоё сообщение с ключом не смог — удали его сам.",
        "key.unchecked": "Anthropic не ответил на проверку; ключ проверится на твоём следующем сообщении.",
        "key.invalid": "Anthropic отклонил этот ключ. Обычные причины: ключ из другого аккаунта консоли или аккаунт без баланса. Повтори шаг 3 — console.anthropic.com → Settings → API keys → Create key — при необходимости пополни Billing и пришли новый ключ сюда.",
        "key.rejected": "Anthropic отклонил твой ключ; ничего не отправлено. Создай новый ключ на console.anthropic.com (Settings → API keys), проверь баланс в Billing и вставь его сюда.",
        "invite.created": "Код приглашения: {code}",
        "queue.busy": "Ещё обрабатываю предыдущее сообщение — отвечу по порядку.",
        "synthetic.recalc": "Пересчитай день.",
        "synthetic.close": "Закрой день.",
        "cmd.start": "Начать или продолжить с того же места",
        "cmd.today": "Заново отправить карточку дня",
        "cmd.forget_me": "Удалить всё о тебе",
        "bot.short": "Тренер в одном чате. Присылай еду — получай цифру. День заканчивается вердиктом.",
        "bot.description": (
            "Strikt записывает еду, тренировки, сон и замеры из одного чата в Telegram. "
            "Пришли фото, скриншот, голосовое или текст — в ответ ккал, белки, углеводы, жиры "
            "и клетчатка по каждому пункту, итог дня и остаток. Карточка дня закреплена и "
            "правится на месте. Если ты замолчал, пишет первым: факт, затем закономерность из "
            "твоих же данных, затем указание со сроком. День закрывается вердиктом. Без "
            "приветствий, без эмодзи, без похвал. Доступ по приглашению."
        ),
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
