from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    thread_id INTEGER,
    UNIQUE(chat_id, message_id, role)
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_recent
    ON chat_messages(chat_id, created_at DESC, message_id DESC, id DESC);
CREATE TABLE IF NOT EXISTS seen_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    seen_at REAL NOT NULL,
    PRIMARY KEY(chat_id, message_id, role)
);
CREATE INDEX IF NOT EXISTS idx_seen_messages_expiry ON seen_messages(seen_at);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    last_attempt_at REAL,
    deferred_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reply_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reply_events_recent ON reply_events(chat_id, sent_at);
CREATE TABLE IF NOT EXISTS group_settings (
    chat_id INTEGER PRIMARY KEY,
    settings_json TEXT NOT NULL
);
"""


class Database:
    """One connection and one lock shared by all repository operations.

    The app intentionally runs a single polling process. The lock prevents an
    await in one repository from interleaving another repository's transaction.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser())
        self.lock = asyncio.Lock()
        self._connection: aiosqlite.Connection | None = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not open")
        return self._connection

    async def open(self) -> None:
        async with self.lock:
            if self._connection is not None:
                return
            if self.path != ":memory:":
                Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self.path)
            try:
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA busy_timeout = 5000")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.executescript(SCHEMA)
                from app.storage.usage import USAGE_SCHEMA

                await connection.executescript(USAGE_SCHEMA)
                await connection.commit()
            except BaseException:
                await connection.close()
                raise
            self._connection = connection

    async def close(self) -> None:
        async with self.lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
