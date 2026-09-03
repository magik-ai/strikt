"""The pinned Today card: one message per local day, edited in place (PLAN §7 ``daycard.py``).

Rules:
- ``refresh`` renders the card for ``state.date``; if that day already has ``card_message_id``
  it edits in place. An unchanged card is skipped locally (no API call, and Telegram would only
  answer "message is not modified"). When the edit fails on changed text the message is gone:
  a new card is sent, pinned and stored. A day without a card gets one only when it is *today*
  in the user's timezone; past days are edited if their card exists and otherwise left alone.
- ``repost`` (``/today``) always sends a fresh card, pins it, unpins the old one, stores the id.
- ``close`` marks the state closed with the verdict and refreshes, so the card ends with the
  verdict line the renderer already knows how to draw.
- Repo functions flush; the caller commits (PLAN §14).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from strikt.core.clock import Clock, local_date
from strikt.db import repo
from strikt.telegram.keyboards import day_actions
from strikt.telegram.render import render_day_card

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.core.types import Button, DayState
    from strikt.db.models import User
    from strikt.telegram.messenger import Messenger

log = structlog.get_logger(__name__)


class DayCard:
    def __init__(self, messenger: Messenger, clock: Clock, *, with_actions: bool = True) -> None:
        self._messenger = messenger
        self._clock = clock
        self._with_actions = with_actions
        # (chat_id, message_id) → last text we put there. Lets us skip no-op edits and tell
        # "not modified" from "gone" (the Messenger protocol collapses both into False).
        self._last_text: dict[tuple[int, int], str] = {}

    # ----------------------------------------------------------------------------- public

    async def refresh(self, session: AsyncSession, user: User, state: DayState) -> int | None:
        """Edit the day's card in place, or post one for today. Returns the card's message id."""
        text = self._render(user, state)
        keyboard = self._keyboard(user, state)
        day = await repo.get_day(session, user.id, state.date)
        message_id = day.card_message_id if day is not None else None
        chat_id = user.chat_id

        if message_id is not None:
            key = (chat_id, message_id)
            if self._last_text.get(key) == text:
                return message_id
            edited = await self._messenger.edit(chat_id, message_id, text, keyboard=keyboard)
            if edited:
                self._last_text[key] = text
                return message_id
            if key not in self._last_text:
                # Unknown history (after a restart): a False here is most likely "not modified".
                # Remember the text; a real change on the next refresh will settle it.
                self._last_text[key] = text
                return message_id
            log.info("daycard_gone", user_id=user.id, message_id=message_id, day=str(state.date))
            self._last_text.pop(key, None)
        elif state.date != local_date(self._clock, user.timezone):
            log.debug("daycard_skip_past_day", user_id=user.id, day=str(state.date))
            return None

        return await self._post(session, user, state.date, text, keyboard, unpin=message_id)

    async def repost(self, session: AsyncSession, user: User, state: DayState) -> int:
        """``/today``: send a new card, pin it, unpin the old one, store the id."""
        text = self._render(user, state)
        keyboard = self._keyboard(user, state)
        day = await repo.get_day(session, user.id, state.date)
        old_id = day.card_message_id if day is not None else None
        return await self._post(session, user, state.date, text, keyboard, unpin=old_id)

    async def close(
        self, session: AsyncSession, user: User, state: DayState, verdict: str | None = None
    ) -> int | None:
        """Refresh with the day marked closed so the card carries the verdict line."""
        closed = state.model_copy(update={"closed": True, "verdict": verdict or state.verdict})
        return await self.refresh(session, user, closed)

    # ---------------------------------------------------------------------------- helpers

    def _render(self, user: User, state: DayState) -> str:
        return render_day_card(state, user.language, user.timezone)

    def _keyboard(self, user: User, state: DayState) -> list[list[Button]] | None:
        if not self._with_actions or state.closed:
            return None
        return day_actions(user.language)

    async def _post(
        self,
        session: AsyncSession,
        user: User,
        day: date,
        text: str,
        keyboard: list[list[Button]] | None,
        *,
        unpin: int | None,
    ) -> int:
        chat_id = user.chat_id
        message_id = await self._messenger.send(chat_id, text, keyboard=keyboard, silent=True)
        self._last_text[(chat_id, message_id)] = text
        if unpin is not None and unpin != message_id:
            await self._messenger.unpin(chat_id, unpin)
            self._last_text.pop((chat_id, unpin), None)
        await self._unpin_previous_day(session, user, day)
        await self._messenger.pin(chat_id, message_id)
        await repo.set_card_message(session, user.id, day, message_id, now=self._clock.now())
        log.info("daycard_posted", user_id=user.id, day=str(day), message_id=message_id)
        return message_id

    async def _unpin_previous_day(self, session: AsyncSession, user: User, day: date) -> None:
        """A new day's card replaces yesterday's pin so only one card is pinned at a time."""
        previous = await repo.get_day(session, user.id, day - timedelta(days=1))
        if previous is not None and previous.card_message_id is not None:
            await self._messenger.unpin(user.chat_id, previous.card_message_id)
