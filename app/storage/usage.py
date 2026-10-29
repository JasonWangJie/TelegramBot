"""Durable per-attempt budgets and aggregate operational statistics."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.storage.database import Database

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_call_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('decision', 'reply')),
    model TEXT NOT NULL,
    created_at REAL NOT NULL,
    success INTEGER CHECK(success IN (0, 1)),
    latency_seconds REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    error_type TEXT,
    estimated_cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_call_usage_day ON ai_call_usage(day, chat_id, id);
CREATE TABLE IF NOT EXISTS ai_outcomes (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    reason TEXT NOT NULL,
    count INTEGER NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY(chat_id, day, reason)
);
"""


class BudgetExceeded(Exception):
    """The next attempt exceeds a durable local-calendar-day budget."""


class UsageStore:
    def __init__(
        self, db: Database, timezone_name: str = "Asia/Shanghai", global_daily_limit: int = 1000,
        default_chat_daily_limit: int = 200, retention_days: int = 30,
        *, clock: Callable[[], float] = time.time,
    ) -> None:
        if global_daily_limit < 0 or default_chat_daily_limit < 0 or retention_days < 1:
            raise ValueError("Invalid usage budget or retention")
        self.db = db
        self.timezone = ZoneInfo(timezone_name)
        self.global_daily_limit = global_daily_limit
        self.default_chat_daily_limit = default_chat_daily_limit
        self.retention_days = retention_days
        self.clock = clock

    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock(), self.timezone).date().isoformat()

    async def reserve(self, chat_id: int, kind: str, model: str, chat_limit: int | None = None) -> int:
        """Charge one HTTP attempt before I/O, including failures and retries.

        Zero disables calls. SQLite's write transaction also serializes separate
        connections; a process crash leaves an occupied, pending reservation.
        """
        limit = self.default_chat_daily_limit if chat_limit is None else chat_limit
        if type(chat_id) is not int or chat_id >= 0 or kind not in {"decision", "reply"}:
            raise ValueError("Expected a group ID and supported call kind")
        if type(limit) is not int or limit < 0:
            raise ValueError("Invalid group daily call limit")
        if not isinstance(model, str) or not model or len(model) > 256:
            raise ValueError("Invalid model identifier")
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute("BEGIN IMMEDIATE")
                day = self._day()
                async with conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(chat_id = ?), 0) FROM ai_call_usage WHERE day = ?",
                    (chat_id, day),
                ) as cursor:
                    global_count, chat_count = await cursor.fetchone()
                if global_count >= self.global_daily_limit or chat_count >= limit:
                    raise BudgetExceeded("daily_budget_exhausted")
                async with conn.execute(
                    "INSERT INTO ai_call_usage(chat_id, day, kind, model, created_at) VALUES (?, ?, ?, ?, ?)",
                    (chat_id, day, kind, model, self.clock()),
                ) as cursor:
                    event_id = int(cursor.lastrowid)
                await conn.commit()
                return event_id
            except BaseException:
                await conn.rollback()
                raise

    async def finish(
        self, event_id: int, *, success: bool, latency_seconds: float,
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
        error_type: str | None = None, estimated_cost_usd: float | None = None,
    ) -> None:
        if not math.isfinite(latency_seconds) or latency_seconds < 0:
            raise ValueError("Invalid attempt latency")
        for value in (prompt_tokens, completion_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid token usage")
        if error_type is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_type):
            raise ValueError("Use an exception type, never an exception body")
        if estimated_cost_usd is not None and (
            not math.isfinite(estimated_cost_usd) or estimated_cost_usd < 0
        ):
            raise ValueError("Invalid estimated cost")
        async with self.db.lock:
            conn = self.db.connection
            try:
                # First finalization wins, including races with cancellation cleanup.
                async with conn.execute(
                    "UPDATE ai_call_usage SET success = ?, latency_seconds = ?, prompt_tokens = ?, "
                    "completion_tokens = ?, error_type = ?, estimated_cost_usd = ? "
                    "WHERE id = ? AND success IS NULL",
                    (int(success), latency_seconds, prompt_tokens, completion_tokens, error_type,
                     estimated_cost_usd, event_id),
                ) as cursor:
                    updated = cursor.rowcount
                if not updated:
                    async with conn.execute("SELECT 1 FROM ai_call_usage WHERE id = ?", (event_id,)) as cursor:
                        if await cursor.fetchone() is None:
                            raise ValueError("Unknown usage reservation")
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def event(self, chat_id: int, reason: str) -> None:
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
            raise ValueError("Use a constant snake_case outcome, never chat text")
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute(
                    "INSERT INTO ai_outcomes(chat_id, day, reason, count, last_at) VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT(chat_id, day, reason) DO UPDATE SET count = count + 1, last_at = excluded.last_at",
                    (chat_id, self._day(), reason, self.clock()),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def snapshot(self, chat_id: int | None, chat_limit: int | None = None) -> dict:
        day = self._day()
        scope = "day = ?" + (" AND chat_id = ?" if chat_id is not None else "")
        parameters = (day, chat_id) if chat_id is not None else (day,)
        async with self.db.lock:
            conn = self.db.connection
            async with conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(kind = 'decision'), 0) AS decision_calls, "
                "COALESCE(SUM(kind = 'reply'), 0) AS reply_calls, "
                "COALESCE(SUM(success = 1), 0) AS successes, COALESCE(SUM(success = 0), 0) AS failures, "
                "COALESCE(SUM(success IS NULL), 0) AS pending_calls, "
                "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                "COALESCE(SUM(prompt_tokens IS NULL OR completion_tokens IS NULL), 0) AS missing_usage_calls, "
                "COALESCE(AVG(latency_seconds), 0) AS average_latency_seconds, "
                "SUM(estimated_cost_usd) AS estimated_cost_usd, COUNT(estimated_cost_usd) AS priced_calls "
                f"FROM ai_call_usage WHERE {scope}", parameters,
            ) as cursor:
                output = dict(await cursor.fetchone())
            async with conn.execute("SELECT COUNT(*) FROM ai_call_usage WHERE day = ?", (day,)) as cursor:
                global_count = (await cursor.fetchone())[0]
            async with conn.execute(
                f"SELECT reason, SUM(count) AS total, MAX(last_at) AS latest FROM ai_outcomes WHERE {scope} "
                "GROUP BY reason ORDER BY latest DESC, reason", parameters,
            ) as cursor:
                outcomes = await cursor.fetchall()
            async with conn.execute(
                f"SELECT error_type FROM ai_call_usage WHERE {scope} AND error_type IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", parameters,
            ) as cursor:
                error = await cursor.fetchone()
        remaining = max(0, self.global_daily_limit - global_count)
        if chat_id is not None:
            limit = self.default_chat_daily_limit if chat_limit is None else chat_limit
            remaining = min(remaining, max(0, limit - output["calls"]))
        output.update(day=day, timezone=self.timezone.key, remaining_calls=remaining,
                      events={row["reason"]: row["total"] for row in outcomes},
                      last_event=outcomes[0]["reason"] if outcomes else None,
                      last_error_type=error[0] if error else None,
                      cost_estimate_complete=bool(output["calls"] and output["calls"] == output["priced_calls"]))
        return output

    async def cleanup(self) -> None:
        cutoff = (datetime.fromtimestamp(self.clock(), self.timezone).date()
                  - timedelta(days=self.retention_days)).isoformat()
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute("DELETE FROM ai_call_usage WHERE day < ?", (cutoff,))
                await conn.execute("DELETE FROM ai_outcomes WHERE day < ?", (cutoff,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
