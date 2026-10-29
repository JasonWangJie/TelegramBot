from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.storage.database import Database


@dataclass(frozen=True, slots=True)
class LimitStatus:
    allowed: bool
    reason: str
    retry_after: float
    remaining: int


class RateLimiter:
    """Persistent rolling-hour quotas, per-chat intervals and attempt cooldowns.

    check() excludes attempt cooldown so a decision can be checked again before
    sending. try_start_attempt() checks it atomically and records the attempt.
    The controller must serialize generation and sending for each chat.
    """

    def __init__(
        self,
        db: Database,
        min_interval_seconds: float = 20,
        cooldown_seconds: float = 8,
        max_per_hour: int = 12,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if min_interval_seconds < 0 or cooldown_seconds < 0 or max_per_hour < 1:
            raise ValueError("Invalid rate limit configuration")
        self.db = db
        self.min_interval_seconds = min_interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_per_hour = max_per_hour
        self.clock = clock

    async def _check_locked(self, chat_id: int, now: float, *, attempt: bool) -> LimitStatus:
        conn = self.db.connection
        async with conn.execute(
            "SELECT sent_at FROM reply_events WHERE chat_id = ? AND sent_at > ? "
            "ORDER BY sent_at ASC", (chat_id, now - 3600),
        ) as cursor:
            rows = await cursor.fetchall()
        async with conn.execute(
            "SELECT last_attempt_at, deferred_until FROM chat_state WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            state = await cursor.fetchone()
        # The most recent reply can be older than the hourly window if a custom
        # minimum interval exceeds one hour.
        async with conn.execute(
            "SELECT MAX(sent_at) FROM reply_events WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            last_reply = (await cursor.fetchone())[0]
        remaining = max(0, self.max_per_hour - len(rows))
        waits: list[tuple[float, str]] = []
        if len(rows) >= self.max_per_hour:
            # When the limit was reduced at runtime, wait until enough events
            # expire, not merely until the oldest event expires.
            waits.append((rows[len(rows) - self.max_per_hour][0] + 3600 - now, "hourly_quota"))
        if last_reply is not None:
            waits.append((last_reply + self.min_interval_seconds - now, "reply_interval"))
        if state:
            waits.append((state["deferred_until"] - now, "telegram_backoff"))
            if attempt and state["last_attempt_at"] is not None:
                waits.append((state["last_attempt_at"] + self.cooldown_seconds - now, "attempt_cooldown"))
        delay, reason = max(waits, default=(0.0, "allowed"))
        if delay > 0:
            return LimitStatus(False, reason, delay, remaining)
        return LimitStatus(True, "allowed", 0.0, remaining)

    async def check(self, chat_id: int) -> LimitStatus:
        async with self.db.lock:
            return await self._check_locked(chat_id, self.clock(), attempt=False)

    async def try_start_attempt(self, chat_id: int) -> LimitStatus:
        async with self.db.lock:
            now = self.clock()
            status = await self._check_locked(chat_id, now, attempt=True)
            if not status.allowed:
                return status
            conn = self.db.connection
            try:
                await conn.execute(
                    "INSERT INTO chat_state(chat_id, last_attempt_at) VALUES (?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET last_attempt_at = excluded.last_attempt_at",
                    (chat_id, now),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            return status

    async def record_reply(self, chat_id: int) -> int:
        """Reserve quota before sending; return the durable reservation ID."""
        async with self.db.lock:
            now = self.clock()
            conn = self.db.connection
            try:
                async with conn.execute(
                    "INSERT INTO reply_events(chat_id, sent_at) VALUES (?, ?)", (chat_id, now)
                ) as cursor:
                    event_id = cursor.lastrowid
                # Keep enough history for both independent constraints; a longer
                # minimum interval must survive pruning and process restarts.
                await conn.execute(
                    "DELETE FROM reply_events WHERE sent_at <= ?",
                    (now - max(3600, self.min_interval_seconds),),
                )
                await conn.commit()
                return int(event_id)
            except BaseException:
                await conn.rollback()
                raise

    async def finalize_reply(self, chat_id: int, event_id: int) -> None:
        """Move a successful send's reservation to delivery time, without recounting.

        Failed or uncertain sends retain their original reservation. This update
        ensures slow Telegram requests cannot shorten the next reply interval.
        """
        async with self.db.lock:
            conn = self.db.connection
            try:
                async with conn.execute(
                    "UPDATE reply_events SET sent_at = MAX(sent_at, ?) "
                    "WHERE chat_id = ? AND id = ?",
                    (self.clock(), chat_id, event_id),
                ) as cursor:
                    if cursor.rowcount != 1:
                        raise ValueError("Reply reservation does not exist for this chat")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def defer(self, chat_id: int, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Backoff cannot be negative")
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute(
                    "INSERT INTO chat_state(chat_id, deferred_until) VALUES (?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET deferred_until = "
                    "MAX(chat_state.deferred_until, excluded.deferred_until)",
                    (chat_id, self.clock() + seconds),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
