"""Validated, persistent overrides for one allowed Telegram group."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.storage.database import Database

if TYPE_CHECKING:
    from app.config import Settings


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["quiet", "normal", "active"] = "normal"
    persona: str | None = Field(default=None, min_length=1, max_length=4000)
    reply_probability: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    min_reply_interval_seconds: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    chat_cooldown_seconds: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    max_replies_per_hour: int | None = Field(default=None, ge=1, le=3600)
    quiet_hours: str | None = None
    daily_call_limit: int | None = Field(default=None, ge=0, le=1000000)

    @field_validator("persona")
    @classmethod
    def persona_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("persona must not be blank")
        return value.strip() if value is not None else None

    @field_validator("quiet_hours")
    @classmethod
    def validate_quiet_hours(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("quiet hours must use HH:MM-HH:MM")
        start, end = value.split("-")
        if start == end:
            raise ValueError("quiet hours must have different start and end times")
        return value


def effective(base: Settings, policy: GroupSettings) -> Settings:
    """Copy deployment settings; never mutate other groups or global defaults."""
    overrides: dict = {}
    if policy.mode == "active":
        overrides.update(reply_probability=0.7, min_reply_interval_seconds=10.0,
                         chat_cooldown_seconds=4.0, max_replies_per_hour=24)
    for name in ("reply_probability", "min_reply_interval_seconds", "chat_cooldown_seconds",
                 "max_replies_per_hour"):
        value = getattr(policy, name)
        if value is not None:
            overrides[name] = value
    if policy.persona is not None:
        overrides["bot_persona"] = policy.persona
    if policy.daily_call_limit is not None:
        overrides["chat_daily_call_limit"] = policy.daily_call_limit
    return base.model_copy(update=overrides)


def is_quiet(policy: GroupSettings, timezone_name: str, now: datetime | None = None) -> bool:
    """Quiet hours silence all AI replies, including mentions; mode is separate."""
    if policy.quiet_hours is None:
        return False
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("quiet-hour checks require a timezone-aware datetime")
    local = instant.astimezone(ZoneInfo(timezone_name))
    current = local.hour * 60 + local.minute
    start_text, end_text = policy.quiet_hours.split("-")
    start = int(start_text[:2]) * 60 + int(start_text[3:])
    end = int(end_text[:2]) * 60 + int(end_text[3:])
    return start <= current < end if start < end else current >= start or current < end


class GroupSettingsStore:
    def __init__(self, db: Database):
        self.db = db

    async def _get_locked(self, chat_id: int) -> GroupSettings:
        async with self.db.connection.execute(
            "SELECT settings_json FROM group_settings WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return GroupSettings.model_validate_json(row[0]) if row else GroupSettings()

    async def get(self, chat_id: int) -> GroupSettings:
        async with self.db.lock:
            return await self._get_locked(chat_id)

    async def update(self, chat_id: int, changes: dict) -> GroupSettings:
        async with self.db.lock:
            conn = self.db.connection
            try:
                previous = await self._get_locked(chat_id)
                updated = GroupSettings.model_validate(previous.model_dump() | changes)
                await conn.execute(
                    "INSERT INTO group_settings(chat_id, settings_json) VALUES (?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET settings_json = excluded.settings_json",
                    (chat_id, updated.model_dump_json()),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return updated

    async def reset(self, chat_id: int) -> GroupSettings:
        async with self.db.lock:
            conn = self.db.connection
            try:
                await conn.execute("DELETE FROM group_settings WHERE chat_id = ?", (chat_id,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return GroupSettings()
