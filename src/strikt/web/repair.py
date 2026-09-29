"""Admin repair of logged food: ``POST /admin/repair`` with the export's bearer token.

A month of per-item edits by id left rows the model damaged and "restored" by hand: a tartare
holding the numbers of Brussels sprouts, a salmon renamed "Люля без жира", three eggs stored as
two. This route fixes named fields of named items and reopens the days they belong to, so the
nightly job re-closes them with verdicts written from the corrected numbers. It never deletes.

Body::

    {
        "user": 2,
        "items": [{"id": 23, "fields": {"kcal": 275, "protein_g": 17}, "reason": "..."}],
        "reopen_days": ["2026-09-06"],
    }

Only the fields in ``FIELDS`` can change; each change is recorded in the item's
``user_correction`` (``before`` / ``changes`` / ``reason``) like any correction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

from strikt.db import repo
from strikt.web.export import authorized

if TYPE_CHECKING:
    from strikt.core.clock import Clock

log = structlog.get_logger(__name__)

NUMBER_FIELDS = frozenset(
    {"grams", "quantity", "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "alcohol_g"}
)
TEXT_FIELDS = frozenset({"name", "unit"})
FIELDS = NUMBER_FIELDS | TEXT_FIELDS
MAX_ITEMS = 50


class RepairError(ValueError):
    pass


def _validate(body: Any) -> tuple[int, list[dict[str, Any]], list[date]]:
    if not isinstance(body, dict):
        raise RepairError("body must be a JSON object")
    user_id = body.get("user")
    if not isinstance(user_id, int):
        raise RepairError("user must be an integer id")
    items = body.get("items") or []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise RepairError(f"items must be a list of at most {MAX_ITEMS}")
    clean: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            raise RepairError("every item needs an integer id")
        fields = entry.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise RepairError(f"item {entry['id']}: fields must be a non-empty object")
        unknown = set(fields) - FIELDS
        if unknown:
            raise RepairError(f"item {entry['id']}: cannot change {sorted(unknown)}")
        for key, value in fields.items():
            if key in NUMBER_FIELDS and not (
                value is None or (isinstance(value, int | float) and not isinstance(value, bool))
            ):
                raise RepairError(f"item {entry['id']}: {key} must be a number")
            if key in TEXT_FIELDS and not (value is None or isinstance(value, str)):
                raise RepairError(f"item {entry['id']}: {key} must be a string")
        clean.append(
            {"id": entry["id"], "fields": fields, "reason": str(entry.get("reason") or "")}
        )
    days: list[date] = []
    for raw in body.get("reopen_days") or []:
        try:
            days.append(date.fromisoformat(str(raw)))
        except ValueError as exc:
            raise RepairError(f"bad day: {raw}") from exc
    return user_id, clean, days


def make_repair_handler(
    token: str, sessions: Callable[[], Any], clock: Clock
) -> Callable[[web.Request], Awaitable[web.Response]]:
    async def handler(request: web.Request) -> web.Response:
        if not authorized(request.headers.get("Authorization"), token):
            log.warning("admin_repair_denied", remote=request.remote)
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            user_id, items, days = _validate(await request.json())
        except (RepairError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        done: list[dict[str, Any]] = []
        async with sessions() as session:
            if await repo.get_user(session, user_id) is None:
                return web.json_response({"error": "unknown user"}, status=404)
            for entry in items:
                row = await repo.get_meal_item(session, user_id, entry["id"])
                if row is None:
                    await session.rollback()
                    return web.json_response(
                        {"error": f"item {entry['id']} not found for user {user_id}"}, status=404
                    )
                before = {key: getattr(row, key) for key in entry["fields"]}
                for key, value in entry["fields"].items():
                    setattr(row, key, value)
                row.user_correction = {
                    "before": before,
                    "changes": entry["fields"],
                    "reason": f"admin repair: {entry['reason']}".strip(),
                }
                done.append({"id": row.id, "before": before, "after": entry["fields"]})
            reopened: list[str] = []
            for day in days:
                day_row = await repo.get_day(session, user_id, day)
                if day_row is not None:
                    day_row.closed_at = None
                    day_row.verdict = None
                    reopened.append(day.isoformat())
            await session.flush()
            await session.commit()
        log.info("admin_repair", user_id=user_id, items=len(done), reopened=reopened)
        return web.json_response({"items": done, "reopened": reopened})

    return handler
