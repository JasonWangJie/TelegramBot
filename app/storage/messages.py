from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.storage.database import Database


@dataclass(frozen=True, slots=True)
class StoredMessage:
    chat_id: int
    message_id: int
    user_id: int | None
    username: str | None
    role: str
    content: str
    created_at: float
    thread_id: int | None = None


class MessageStore:
    def __init__(
        self,
        db: Database,
        max_messages: int = 30,
        retention_days: float = 7,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_messages < 1 or retention_days <= 0:
            raise ValueError("Message count and retention must be positive")
        self.db = db
        self.max_messages = max_messages
        self.retention_seconds = retention_days * 86400
        self.clock = clock

    async def add(self, message: StoredMessage) -> bool:
        """Persist once; removing old context does not remove replay protection."""
        if message.role not in {"user", "assistant"}:
            raise ValueError("Unsupported message role")
        now = self.clock()
        async with self.db.lock:
            conn = self.db.connection
            try:
                async with conn.execute(
                    "INSERT OR IGNORE INTO seen_messages(chat_id, message_id, role, seen_at) "
                    "VALUES (?, ?, ?, ?)",
                    (message.chat_id, message.message_id, message.role, now),
                ) as cursor:
                    inserted = cursor.rowcount == 1
                if not inserted:
                    await conn.commit()
                    return False
                async with conn.execute(
                    "INSERT OR IGNORE INTO chat_messages "
                    "(chat_id, message_id, user_id, username, role, content, created_at, thread_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message.chat_id, message.message_id, message.user_id,
                        message.username, message.role, message.content,
                        message.created_at, message.thread_id,
                    ),
                ) as cursor:
                    inserted = cursor.rowcount == 1
                await conn.execute(
                    "DELETE FROM chat_messages WHERE chat_id = ? AND created_at < ?",
                    (message.chat_id, now - self.retention_seconds),
                )
                await conn.execute(
                    "DELETE FROM chat_messages WHERE chat_id = ? AND id NOT IN "
                    "(SELECT id FROM chat_messages WHERE chat_id = ? "
                    "ORDER BY created_at DESC, message_id DESC, id DESC LIMIT ?)",
                    (message.chat_id, message.chat_id, self.max_messages),
                )
                await conn.commit()
                return inserted
            except BaseException:
                await conn.rollback()
                raise

    async def recent(
        self, chat_id: int, limit: int | None = None, thread_id: int | None = None
    ) -> list[StoredMessage]:
        """Read only this group and topic; None selects the main topic."""
        count = self.max_messages if limit is None else min(max(limit, 0), self.max_messages)
        async with self.db.lock:
            async with self.db.connection.execute(
                "SELECT chat_id, message_id, user_id, username, role, content, created_at, thread_id "
                "FROM chat_messages WHERE chat_id = ? AND thread_id IS ? AND created_at >= ? "
                "ORDER BY created_at DESC, message_id DESC, id DESC LIMIT ?",
                (chat_id, thread_id, self.clock() - self.retention_seconds, count),
            ) as cursor:
                rows = await cursor.fetchall()
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    async def count(self, chat_id: int) -> int:
        async with self.db.lock:
            async with self.db.connection.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE chat_id = ? AND created_at >= ?",
                (chat_id, self.clock() - self.retention_seconds),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0])

    async def is_paused(self, chat_id: int) -> bool:
        async with self.db.lock:
            async with self.db.connection.execute(
                "SELECT paused FROM chat_state WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return bool(row[0]) if row else False

    async def set_paused(self, chat_id: int, paused: bool) -> None:
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute(
                    "INSERT INTO chat_state(chat_id, paused) VALUES (?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET paused = excluded.paused",
                    (chat_id, int(paused)),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def cleanup(self) -> None:
        """Expire all groups, including groups that have become inactive.

        Rate history is owned by RateLimiter and is never pruned with context.
        """
        cutoff = self.clock() - self.retention_seconds
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute("DELETE FROM chat_messages WHERE created_at < ?", (cutoff,))
                await conn.execute("DELETE FROM seen_messages WHERE seen_at < ?", (cutoff,))
                # Also apply changed retention counts after a configuration update.
                await conn.execute(
                    "DELETE FROM chat_messages WHERE id IN ("
                    "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY chat_id "
                    "ORDER BY created_at DESC, message_id DESC, id DESC) AS position "
                    "FROM chat_messages) WHERE position > ?)",
                    (self.max_messages,),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
