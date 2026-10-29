from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr
    telegram_allowed_chat_ids: str = ""
    telegram_owner_ids: str = ""
    telegram_admin_ids: str = ""
    new_api_base_url: str
    new_api_key: SecretStr
    new_api_model: str = Field(min_length=1)
    decision_model: str | None = None
    bot_persona: str = Field(default="自然、友善、有幽默感的中文群聊伙伴", max_length=4000)
    context_max_messages: int = Field(default=30, ge=1, le=200)
    context_max_chars: int = Field(default=16000, ge=500, le=100000)
    message_max_chars: int = Field(default=2000, ge=100, le=10000)
    history_retention_days: int = Field(default=7, ge=1, le=365)
    decision_enabled: bool = True
    decision_confidence_threshold: float = Field(default=0.65, ge=0, le=1, allow_inf_nan=False)
    decision_json_mode: bool = False
    reply_probability: float = Field(default=0.35, ge=0, le=1, allow_inf_nan=False)
    direct_mention_always_reply: bool = True
    min_reply_interval_seconds: float = Field(default=20, ge=0, allow_inf_nan=False)
    chat_cooldown_seconds: float = Field(default=8, ge=0, allow_inf_nan=False)
    max_replies_per_hour: int = Field(default=12, ge=1, le=3600)
    message_batch_window_seconds: float = Field(default=3, ge=0, le=10, allow_inf_nan=False)
    reply_delay_min_seconds: float = Field(default=1, ge=0, le=30, allow_inf_nan=False)
    reply_delay_max_seconds: float = Field(default=3, ge=0, le=30, allow_inf_nan=False)
    message_max_age_seconds: float = Field(default=300, ge=10, le=86400, allow_inf_nan=False)
    ai_timeout_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    ai_max_retries: int = Field(default=2, ge=0, le=5)
    ai_max_output_tokens: int = Field(default=300, ge=1, le=4000)
    ai_max_concurrent_requests: int = Field(default=4, ge=1, le=32)
    ai_token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    bot_timezone: str = "Asia/Shanghai"
    followup_ttl_seconds: int = Field(default=120, ge=0, le=1800)
    chat_daily_call_limit: int = Field(default=200, ge=0, le=100000)
    global_daily_call_limit: int = Field(default=1000, ge=0, le=1000000)
    usage_retention_days: int = Field(default=30, ge=1, le=365)
    ai_input_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    ai_output_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    decision_input_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    decision_output_price_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    database_path: Path = Path("data/bot.db")
    heartbeat_path: Path = Path("data/bot-heartbeat")
    healthcheck_max_age_seconds: float = Field(default=180, ge=60, le=600, allow_inf_nan=False)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("bot_timezone")
    @classmethod
    def timezone_name(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("expected an IANA timezone name") from None
        return value

    @field_validator("telegram_bot_token", "new_api_key")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if not raw or "替换" in raw or raw.startswith("replace-with-"):
            raise ValueError("must supply a real nonempty credential")
        return SecretStr(raw)

    @field_validator("new_api_model", "decision_model")
    @classmethod
    def model_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            if value is None:
                return None
            return ""
        if "填写" in value or "替换" in value or value.startswith("replace-with-"):
            raise ValueError("must supply a gateway model identifier")
        return value.strip()

    @field_validator("new_api_base_url")
    @classmethod
    def base_url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or "你的" in value):
            raise ValueError("must be an HTTP(S) API base URL without credentials, query or fragment")
        return value.strip().rstrip("/")

    @field_validator("telegram_allowed_chat_ids", "telegram_owner_ids", "telegram_admin_ids")
    @classmethod
    def numeric_ids(cls, value: str, info) -> str:
        if value.strip():
            try:
                ids = [int(part.strip()) for part in value.split(",")]
            except ValueError:
                raise ValueError("expected comma-separated integer IDs") from None
            groups = info.field_name == "telegram_allowed_chat_ids"
            if any((item >= 0 if groups else item <= 0) for item in ids):
                raise ValueError("group IDs must be negative; user IDs must be positive")
        return value

    @model_validator(mode="after")
    def validate_settings(self):
        from aiogram.utils.token import validate_token

        try:
            validate_token(self.telegram_bot_token.get_secret_value())
        except Exception:
            raise ValueError("invalid Telegram token format") from None
        if self.reply_delay_max_seconds < self.reply_delay_min_seconds:
            raise ValueError("reply delay maximum must be >= minimum")
        if not self.new_api_model.strip():
            raise ValueError("new_api_model must not be blank")
        return self

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return frozenset(int(part.strip()) for part in self.telegram_allowed_chat_ids.split(",") if part.strip())

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(int(part.strip()) for value in (self.telegram_owner_ids, self.telegram_admin_ids)
                         for part in value.split(",") if part.strip())
