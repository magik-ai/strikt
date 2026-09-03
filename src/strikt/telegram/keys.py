"""Bring-your-own-key helpers for the chat: spotting a pasted Anthropic key, spotting a
question about the key. Pure functions; the handler that stores the key lives in
``telegram/handlers.py`` (``handle_key_message``)."""

from __future__ import annotations

import re

#: What an Anthropic API key looks like (``sk-ant-api03-…``): the prefix and at least twenty
#: url-safe characters. Anywhere in the text — the user may paste it with a word before it.
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
#: A keyless user asking about the key (en/ru): the walkthrough goes out instead of nothing.
_KEY_TALK = re.compile(
    r"(?i)(?<![\w-])(?:api|key|keys|token|ключ\w*|токен\w*|anthropic|console)(?![\w-])"
)


def extract_key(text: str | None) -> str | None:
    """The first key-looking token in ``text``, or None."""
    if not text:
        return None
    match = KEY_RE.search(text)
    return match.group(0) if match else None


def is_key_message(text: str | None) -> bool:
    return extract_key(text) is not None


def mentions_key(text: str | None) -> bool:
    """``"where do I get the key?"`` / ``"какой ключ?"`` → True; ``"omelette"`` → False."""
    return bool(text) and _KEY_TALK.search(text or "") is not None
