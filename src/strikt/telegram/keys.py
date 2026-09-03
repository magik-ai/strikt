"""Spotting a secret pasted into the chat, and a question about one.

Three secrets can arrive as a message: the user's Anthropic key (the coach runs on it), an
OpenAI key (voice notes get transcribed) and a USDA key (the food database answers faster and
more often). The first two have a shape worth matching; a USDA key is forty plain characters
and looks like nothing, so it is only taken when the bot has just asked for it and the user is
in ``awaiting_secret``.

Pure functions. The handlers that store them live in ``telegram/handlers.py``.
"""

from __future__ import annotations

import re

#: What an Anthropic API key looks like (``sk-ant-api03-…``): the prefix and at least twenty
#: url-safe characters. Anywhere in the text - the user may paste it with a word before it.
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
#: An OpenAI key: the same ``sk-`` family without Anthropic's ``ant-`` marker (``sk-proj-…``,
#: ``sk-svcacct-…``, or the plain old form). Checked after ``KEY_RE`` so it never steals one.
OPENAI_KEY_RE = re.compile(r"sk-(?!ant-)[A-Za-z0-9_\-]{20,}")
#: A USDA key from api.data.gov: forty alphanumerics, nothing else. Far too plain to match on
#: sight, so this is used only to sanity-check what arrives while the bot is waiting for one.
USDA_KEY_RE = re.compile(r"^[A-Za-z0-9]{30,50}$")
#: A keyless user asking about the key (en/ru): the walkthrough goes out instead of nothing.
_KEY_TALK = re.compile(
    r"(?i)(?<![\w-])(?:api|key|keys|token|ключ\w*|токен\w*|anthropic|console)(?![\w-])"
)


def extract_key(text: str | None) -> str | None:
    """The first Anthropic key in ``text``, or None."""
    if not text:
        return None
    match = KEY_RE.search(text)
    return match.group(0) if match else None


def extract_openai_key(text: str | None) -> str | None:
    """The first OpenAI key in ``text``, or None. An Anthropic key never matches."""
    if not text:
        return None
    match = OPENAI_KEY_RE.search(text)
    return match.group(0) if match else None


def looks_like_usda_key(text: str | None) -> bool:
    return bool(text) and USDA_KEY_RE.match((text or "").strip()) is not None


def is_key_message(text: str | None) -> bool:
    return extract_key(text) is not None


def mentions_key(text: str | None) -> bool:
    """``"where do I get the key?"`` / ``"какой ключ?"`` → True; ``"omelette"`` → False."""
    return bool(text) and _KEY_TALK.search(text or "") is not None
