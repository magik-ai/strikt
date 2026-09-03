"""Code-rendered strings (card, buttons, errors, /start, /forget_me) in ru and en.

The coach's replies are model-written; only what the *code* renders lives here. Keyed by the
user's language (``ru`` or anything else → ``en``). Keep lines short: they are read on a phone.

Voice (chernyakov.ai ``STYLE.md``): a person talking to a person. Real sentences, never staccato
fragments. A short dash with spaces, never a long one. No emoji, no praise, no marketing words.
Messages stay short: a blank line between blocks reads better in Telegram than one dense block,
and a list is at most four one-line bullets.
"""

from __future__ import annotations

import re
from typing import Any

Lang = str

# Bring-your-own-key walkthrough. Shared by ``key.needed`` (a keyless user's first message, and
# the second message after /start) and ``key.help`` (a keyless user asking about the key). Plain
# text: no < > & — the messenger sends HTML.
_KEY_STEPS: dict[str, str] = {
    "en": (
        "- console.anthropic.com, sign in\n"
        "- Billing, put a few dollars on the account\n"
        "- Settings, API keys, Create key. It starts with sk-ant-\n"
        "- paste the key here"
    ),
    "ru": (
        "- console.anthropic.com, войди\n"
        "- Billing, положи несколько долларов\n"
        "- Settings, API keys, Create key. Он начинается с sk-ant-\n"
        "- вставь ключ сюда"
    ),
}

#: Explicit answers to the language question, checked before the alphabet heuristic: «английский»
#: is Cyrillic but asks for English.
_LANG_WORDS: dict[str, str] = {
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "рус": "ru",
    "русский": "ru",
    "русском": "ru",
    "по-русски": "ru",
    "en": "en",
    "eng": "en",
    "english": "en",
    "англ": "en",
    "английский": "en",
    "английском": "en",
}
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)

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
        "card.no_protocol": "no protocol yet, finish onboarding",
        "card.paused": "targets paused, sick day",
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
        "err.llm_down": (
            "Claude is not answering right now. Your message is saved, send the next one and I "
            "will pick up both."
        ),
        "err.llm_no_credit": (
            "Your Anthropic key has no credit left, so the call was refused.\n\n"
            "Top it up at console.anthropic.com, Plans and Billing, then send the message again."
        ),
        "err.tool_failed": "Couldn't verify, estimating from ingredients. Correct me if you know better.",
        "err.transcribe": "Voice transcription is off. Send text.",
        "err.transcribe_failed": "Couldn't transcribe that. Send text or try again.",
        "err.media": "Couldn't read that file. Send a photo, PDF or text.",
        "err.too_large": "File too large (limit {mb} MB).",
        # HTML parse mode: the placeholder must be escaped or Telegram rejects the message.
        "err.not_allowed": "This coach is invite-only. Ask the owner for a code and send /start &lt;code&gt;.",
        "err.invite_invalid": "That invite code is not valid.",
        "err.unknown": "Something broke on my side. Send that again.",
        # Language, asked once before anything else (the answer is free text or a button)
        "lang.ask": "Говорим по-русски или на английском?\n\nRussian or English? Just say it.",
        "btn.lang_ru": "Русский",
        "btn.lang_en": "English",
        # /start
        "start.welcome": (
            "Strikt. One chat, no settings.\n\n"
            "Send food photos, screenshots, voice or text. I log it, count it and push."
        ),
        "start.onboarding": "First a short interview, ten questions, you can stop anywhere. Your name?",
        "start.resume": "Back. Where we left off:",
        "start.invite_ok": "Invite accepted.",
        # /today
        "today.reposted": "Card re-posted.",
        # /forget_me
        "forget.question": (
            "Delete everything about you? Profile, meals, training, notes, chat history and your "
            "API key. This cannot be undone."
        ),
        "forget.done": (
            "Deleted {rows} rows, your API key with them. Nothing about you remains.\n\n"
            "/start to begin again."
        ),
        "forget.cancelled": "Kept everything.",
        # the user's Anthropic key (bring-your-own-key; code-rendered, the model never sees a key)
        "key.needed": (
            "The coach runs on your own Anthropic key. Strikt is free, Anthropic bills the key "
            "for what it spends.\n\n" + _KEY_STEPS["en"] + "\n\n"
            "I check it, store it encrypted and delete your message."
        ),
        "key.help": "The key, step by step:\n\n" + _KEY_STEPS["en"],
        "key.saved": "Key saved, ends in …{last4}. Your message with it is deleted.",
        "key.saved_keep": (
            "Key saved, ends in …{last4}. I could not delete your message with it, so delete it "
            "yourself."
        ),
        "key.unchecked": "Anthropic did not answer the check. The key gets checked on your next message.",
        "key.invalid": (
            "Anthropic did not accept this key. Usually that is a key from another console "
            "account, or an account with no credit.\n\n"
            "Make a new one in Settings, API keys, check Billing, and send it here."
        ),
        "key.rejected": (
            "Anthropic rejected your key, so nothing was sent.\n\n"
            "Make a new one at console.anthropic.com, check that Billing has credit, and paste "
            "it here."
        ),
        # admin
        "invite.created": "Invite code: {code}",
        # misc
        "queue.busy": "Still on your previous message, answering in order.",
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
        "card.no_protocol": "протокола ещё нет, закончи онбординг",
        "card.paused": "цели на паузе, день болезни",
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
        "err.llm_down": (
            "Claude сейчас не отвечает. Сообщение сохранил, пришли следующее и разберу оба."
        ),
        "err.llm_no_credit": (
            "На ключе Anthropic закончились деньги, запрос отклонён.\n\n"
            "Пополни на console.anthropic.com, раздел Plans and Billing, и пришли сообщение "
            "ещё раз."
        ),
        "err.tool_failed": "Не смог проверить, считаю по ингредиентам. Поправь, если знаешь точнее.",
        "err.transcribe": "Распознавание голоса выключено. Напиши текстом.",
        "err.transcribe_failed": "Не смог распознать голос. Напиши текстом или пришли ещё раз.",
        "err.media": "Не смог прочитать файл. Пришли фото, PDF или текст.",
        "err.too_large": "Файл слишком большой (лимит {mb} МБ).",
        "err.not_allowed": "Доступ по приглашению. Возьми код у владельца и отправь /start &lt;код&gt;.",
        "err.invite_invalid": "Код приглашения не подходит.",
        "err.unknown": "У меня что-то сломалось. Отправь ещё раз.",
        "lang.ask": "Говорим по-русски или на английском?\n\nRussian or English? Just say it.",
        "btn.lang_ru": "Русский",
        "btn.lang_en": "English",
        "start.welcome": (
            "Strikt. Один чат, без настроек.\n\n"
            "Присылай фото еды, скриншоты, голос или текст. Я записываю, считаю и подгоняю."
        ),
        "start.onboarding": "Сначала короткое интервью, десять вопросов, прерваться можно в любой момент. Как тебя зовут?",
        "start.resume": "С возвращением. Остановились здесь:",
        "start.invite_ok": "Приглашение принято.",
        "today.reposted": "Карточка обновлена.",
        "forget.question": (
            "Удалить всё о тебе? Профиль, еду, тренировки, заметки, историю чата и твой "
            "API-ключ. Отменить нельзя."
        ),
        "forget.done": (
            "Удалено строк: {rows}, API-ключ вместе с ними. О тебе ничего не осталось.\n\n"
            "/start чтобы начать заново."
        ),
        "forget.cancelled": "Оставил всё как есть.",
        "key.needed": (
            "Тренер работает на твоём ключе Anthropic. Strikt бесплатный, Anthropic списывает "
            "с ключа за расход.\n\n" + _KEY_STEPS["ru"] + "\n\n"
            "Я проверю ключ, сохраню в шифрованном виде и удалю твоё сообщение."
        ),
        "key.help": "Ключ, по шагам:\n\n" + _KEY_STEPS["ru"],
        "key.saved": "Ключ сохранён, заканчивается на …{last4}. Твоё сообщение с ним удалено.",
        "key.saved_keep": (
            "Ключ сохранён, заканчивается на …{last4}. Удалить твоё сообщение с ним не смог, "
            "удали его сам."
        ),
        "key.unchecked": "Anthropic не ответил на проверку. Ключ проверится на следующем сообщении.",
        "key.invalid": (
            "Anthropic не принял этот ключ. Обычно это ключ из другого аккаунта консоли или "
            "аккаунт без баланса.\n\n"
            "Создай новый в Settings, API keys, проверь Billing и пришли сюда."
        ),
        "key.rejected": (
            "Anthropic отклонил ключ, ничего не отправлено.\n\n"
            "Создай новый на console.anthropic.com, проверь баланс в Billing и вставь сюда."
        ),
        "invite.created": "Код приглашения: {code}",
        "queue.busy": "Ещё отвечаю на предыдущее сообщение, отвечу по порядку.",
        "synthetic.recalc": "Пересчитай день.",
        "synthetic.close": "Закрой день.",
        "cmd.start": "Начать или продолжить с того же места",
        "cmd.today": "Заново отправить карточку дня",
        "cmd.forget_me": "Удалить всё о тебе",
        "bot.short": "Тренер в одном чате. Присылай еду, получай цифру. День заканчивается вердиктом.",
        "bot.description": (
            "Strikt записывает еду, тренировки, сон и замеры из одного чата в Telegram. "
            "Пришли фото, скриншот, голосовое или текст, в ответ придут ккал, белки, углеводы, "
            "жиры и клетчатка по каждому пункту, итог дня и остаток. Карточка дня закреплена и "
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


def detect_lang(text: str | None) -> Lang | None:
    """The language the user answered ``lang.ask`` with, or None when the message says nothing.

    A named language wins over the alphabet it is written in (``английский`` is Cyrillic and
    means English); otherwise any Cyrillic letter means Russian and any Latin letter English.
    """
    if not text:
        return None
    lowered = text.strip().lower()
    for word in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", lowered, flags=re.UNICODE):
        named = _LANG_WORDS.get(word)
        if named is not None:
            return named
    if _CYRILLIC.search(lowered):
        return "ru"
    if re.search(r"[a-z]", lowered):
        return "en"
    return None


def t(lang: str | None, key: str, **kwargs: Any) -> str:
    """Translate ``key`` for ``lang`` with ``str.format`` args; falls back to en, then the key."""
    table = STRINGS.get(resolve_lang(lang), STRINGS["en"])
    template = table.get(key) or STRINGS["en"].get(key) or key
    return template.format(**kwargs) if kwargs else template


def weekday_name(lang: str | None, weekday: int) -> str:
    return WEEKDAYS[resolve_lang(lang)][weekday % 7]


def month_name(lang: str | None, month: int) -> str:
    return MONTHS[resolve_lang(lang)][(month - 1) % 12]
