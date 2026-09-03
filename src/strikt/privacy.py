"""``/forget_me``: hard-delete every row a user owns, in dependency order, in one transaction."""

from __future__ import annotations

from typing import Any, cast

import sqlalchemy as sa
import structlog
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from strikt.db.models import (
    ConversationTurn,
    Day,
    Integration,
    Invite,
    Lab,
    Meal,
    MealItem,
    Measurement,
    Note,
    OAuthState,
    ProactiveSend,
    Profile,
    Protocol,
    Recovery,
    Reminder,
    Sleep,
    Summary,
    TokenUsage,
    User,
    UserSecret,
    Workout,
)

log = structlog.get_logger(__name__)

# Children first, parents last. ``foods`` is a shared cache and is never touched.
_DELETE_ORDER: tuple[type[Any], ...] = (
    UserSecret,
    ProactiveSend,
    TokenUsage,
    OAuthState,
    Integration,
    Summary,
    ConversationTurn,
    Reminder,
    Note,
    Lab,
    Measurement,
    Recovery,
    Sleep,
    Workout,
    MealItem,
    Meal,
    Day,
    Protocol,
    Profile,
)


async def delete_everything(session: AsyncSession, user_id: int) -> dict[str, int]:
    """Delete every user-owned row and the user itself. Returns ``{table: rows_deleted}``.

    The caller commits (or rolls back) the session; nothing is committed here so the whole
    deletion is atomic with whatever surrounds it (e.g. unpinning the card first).
    """
    counts: dict[str, int] = {}

    # Self-referencing FK on notes: unlink before deleting so no dialect can complain.
    await session.execute(sa.update(Note).where(Note.user_id == user_id).values(superseded_by=None))
    # Invites reference users twice (created_by / used_by); detach rather than delete them so an
    # admin's minted codes survive their own deletion.
    invites = await session.execute(
        sa.update(Invite).where(Invite.used_by == user_id).values(used_by=None)
    )
    counts["invites.used_by"] = _rowcount(invites)
    invites_created = await session.execute(
        sa.update(Invite).where(Invite.created_by == user_id).values(created_by=None)
    )
    counts["invites.created_by"] = _rowcount(invites_created)

    for model in _DELETE_ORDER:
        result = await session.execute(sa.delete(model).where(model.user_id == user_id))
        counts[model.__tablename__] = _rowcount(result)

    users = await session.execute(sa.delete(User).where(User.id == user_id))
    counts["users"] = _rowcount(users)
    await session.flush()
    log.info("user_deleted", user_id=user_id, rows=sum(counts.values()))
    return counts


def _rowcount(result: Any) -> int:
    return int(cast("CursorResult[Any]", result).rowcount)
