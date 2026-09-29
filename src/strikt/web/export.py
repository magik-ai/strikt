"""Read-only admin export of one user's days: every turn, every meal item, every nudge.

``GET /admin/export?user=<id>&from=YYYY-MM-DD&to=YYYY-MM-DD`` with
``Authorization: Bearer <ADMIN_EXPORT_TOKEN>``. The route exists only when the token is set;
a wrong or missing token is a 401 and nothing is read. Dates are the user's local calendar
days (``to`` defaults to today, ``from`` to ``to``). It is how a bad day in the chat gets
debugged turn by turn without a shell on the server: the database stays private, the export
goes over the same HTTPS the webhooks use.

What it leaves out on purpose: the user's encrypted keys and secrets, and the picture bytes
(the database never holds them; a turn shows its ``[image: <sha256>]`` stub).
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

from strikt.core.clock import ensure_utc, local_day_bounds, to_local
from strikt.db import repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from strikt.core.clock import Clock
    from strikt.db.models import ConversationTurn, Meal, MealItem, User

log = structlog.get_logger(__name__)

#: A range longer than this is refused: the export is for debugging days, not dumping history.
MAX_DAYS = 31
#: Keys ``loop.stub_media_blocks`` adds to a stored stub; the Telegram file id is not needed.
_MEDIA_KEYS = ("media_file_id",)


class ExportError(ValueError):
    """A bad query (unknown user, unreadable date, range too long)."""


def authorized(header: str | None, token: str) -> bool:
    """``Authorization: Bearer <token>``, compared in constant time."""
    if not token or not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _local(value: datetime | None, tz: str) -> str | None:
    return f"{to_local(value, tz):%Y-%m-%d %H:%M}" if value is not None else None


def _turn(turn: ConversationTurn, tz: str) -> dict[str, Any]:
    blocks = turn.content if isinstance(turn.content, list) else []
    content = [
        {k: v for k, v in block.items() if k not in _MEDIA_KEYS}
        for block in blocks
        if isinstance(block, dict)
    ]
    return {
        "id": turn.id,
        "role": _plain(turn.role),
        "at_local": _local(turn.created_at, tz),
        "telegram_message_id": turn.telegram_message_id,
        "content": content,
        "tokens": {
            "input": turn.input_tokens,
            "output": turn.output_tokens,
            "cache_read": turn.cache_read_tokens,
            "cache_write": turn.cache_write_tokens,
        },
    }


def _item(item: MealItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "brand": item.brand,
        "restaurant": item.restaurant,
        "quantity": item.quantity,
        "unit": item.unit,
        "grams": item.grams,
        "stored": {
            "kcal": item.kcal,
            "protein_g": item.protein_g,
            "carbs_g": item.carbs_g,
            "fat_g": item.fat_g,
            "fiber_g": item.fiber_g,
            "alcohol_g": item.alcohol_g,
            "sodium_mg": item.sodium_mg,
        },
        "model_estimate": item.model_estimate,
        "user_correction": item.user_correction,
        "flags": item.flags,
        "source": _plain(item.source),
        "confidence": item.confidence,
        "countable": item.countable,
    }


def _meal(meal: Meal, tz: str) -> dict[str, Any]:
    return {
        "id": meal.id,
        "day": meal.day_date.isoformat(),
        "slot": _plain(meal.slot),
        "source": _plain(meal.source),
        "logged_local": _local(meal.logged_at, tz),
        "eaten_local": _local(meal.eaten_at, tz),
        "deleted_local": _local(meal.deleted_at, tz),
        "note": meal.note,
        "raw_ref": meal.raw_ref,
        "items": [_item(item) for item in meal.items],
    }


def parse_range(raw_from: str | None, raw_to: str | None, *, today: date) -> tuple[date, date]:
    """``from``/``to`` as local dates; ``to`` defaults to today and ``from`` to ``to``."""
    try:
        day_to = date.fromisoformat(raw_to) if raw_to else today
        day_from = date.fromisoformat(raw_from) if raw_from else day_to
    except ValueError as exc:
        raise ExportError(f"bad date: {exc}") from exc
    if day_from > day_to:
        raise ExportError("from is after to")
    if (day_to - day_from).days + 1 > MAX_DAYS:
        raise ExportError(f"at most {MAX_DAYS} days per export")
    return day_from, day_to


async def export_user(
    session: AsyncSession, user: User, day_from: date, day_to: date
) -> dict[str, Any]:
    """Everything the coach saw and wrote for ``user`` over the local days ``day_from..day_to``."""
    tz = user.timezone or "UTC"
    start, _ = local_day_bounds(day_from, tz)
    _, end = local_day_bounds(day_to, tz)
    # a coaching day runs past local midnight (core/clock.day_rollover): take the night after
    end = end + timedelta(hours=6)
    turns = await repo.turns_between(session, user.id, start, end)
    meals = await repo.list_meals_range_with_deleted(session, user.id, day_from, day_to)
    days = await repo.list_days_range(session, user.id, day_from, day_to)
    sends = [
        s for s in await repo.list_sends_since(session, user.id, since=start) if s.sent_at < end
    ]
    protocol = await repo.get_active_protocol(session, user.id)
    profile = await repo.get_profile(session, user.id)
    return {
        "user": {
            "id": user.id,
            "language": user.language,
            "timezone": tz,
            "status": _plain(user.status),
        },
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "profile": {
            "wake_time": _plain(profile.wake_time) if profile else None,
            "bed_time": _plain(profile.bed_time) if profile else None,
        },
        "protocol": (
            {
                "version": protocol.version,
                "kcal": protocol.kcal,
                "protein_g": protocol.protein_g,
                "carbs_g": protocol.carbs_g,
                "fat_g": protocol.fat_g,
                "fiber_g": protocol.fiber_g,
            }
            if protocol is not None
            else None
        ),
        "days": [
            {
                "date": d.date.isoformat(),
                "opened_local": _local(d.opened_at, tz),
                "closed_local": _local(d.closed_at, tz),
                "plan": d.plan,
                "flags": d.flags,
                "verdict": d.verdict,
                "notes": d.notes,
            }
            for d in days
        ],
        "turns": [_turn(turn, tz) for turn in turns],
        "meals": [_meal(meal, tz) for meal in meals],
        "proactive": [
            {
                "at_local": _local(s.sent_at, tz),
                "trigger": s.trigger,
                "step": s.step,
                "text": s.text,
                "responded_local": _local(s.responded_at, tz),
            }
            for s in sends
        ],
    }


def make_handler(
    token: str, sessions: Callable[[], Any], clock: Clock
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """The aiohttp handler, closed over the configured token, the sessions and the clock."""

    async def handler(request: web.Request) -> web.Response:
        if not authorized(request.headers.get("Authorization"), token):
            log.warning("admin_export_denied", remote=request.remote)
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            user_id = int(request.query.get("user", ""))
        except ValueError:
            return web.json_response({"error": "user=<id> is required"}, status=400)
        async with sessions() as session:
            user = await repo.get_user(session, user_id)
            if user is None:
                return web.json_response({"error": "unknown user"}, status=404)
            today = to_local(clock.now(), user.timezone or "UTC").date()
            try:
                day_from, day_to = parse_range(
                    request.query.get("from"), request.query.get("to"), today=today
                )
            except ExportError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            payload = await export_user(session, user, day_from, day_to)
        log.info(
            "admin_export",
            user_id=user_id,
            day_from=str(day_from),
            day_to=str(day_to),
            turns=len(payload["turns"]),
            meals=len(payload["meals"]),
        )
        return web.json_response(payload, dumps=_dumps)

    return handler


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
