"""Per-chat serialisation: one agent run per chat at a time; later messages wait, never drop."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class PerChatQueue:
    """``run(chat_id, factory)`` executes ``factory()`` under the chat's lock, FIFO.

    ``heartbeat`` (e.g. a typing action) is awaited every ``heartbeat_interval`` seconds while
    the coroutine runs, including while it waits for the lock, so the user sees activity.
    """

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    def pending(self, chat_id: int) -> int:
        """Runs queued or in progress for this chat."""
        return self._waiting.get(chat_id, 0)

    def busy(self, chat_id: int) -> bool:
        return self._lock(chat_id).locked()

    async def run(
        self,
        chat_id: int,
        coro_factory: Callable[[], Awaitable[T]],
        *,
        heartbeat: Callable[[], Awaitable[None]] | None = None,
        heartbeat_interval: float = 4.0,
    ) -> T:
        self._waiting[chat_id] = self._waiting.get(chat_id, 0) + 1
        beat_task: asyncio.Task[None] | None = None
        if heartbeat is not None:
            beat_task = asyncio.create_task(self._beat(heartbeat, heartbeat_interval))
        try:
            async with self._lock(chat_id):
                return await coro_factory()
        finally:
            if beat_task is not None:
                beat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await beat_task
            self._waiting[chat_id] -= 1
            if self._waiting[chat_id] <= 0:
                del self._waiting[chat_id]

    @staticmethod
    async def _beat(heartbeat: Callable[[], Awaitable[None]], interval: float) -> None:
        while True:
            with contextlib.suppress(Exception):
                await heartbeat()
            await asyncio.sleep(interval)
