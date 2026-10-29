import json
import logging
import time
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import GetMe, GetUpdates
from pydantic import ValidationError

from app.config import Settings
from app.healthcheck import PollingHeartbeat, healthy
from app.logging_setup import JsonFormatter


def settings(**kwargs):
    return Settings(_env_file=None, telegram_bot_token="123456:" + "x" * 35,
                    new_api_key="fake-api-secret", new_api_base_url="https://example.test/v1",
                    new_api_model="test-model", **kwargs)


def test_fail_closed_defaults_and_numeric_id_parsing():
    assert not settings().allowed_chat_ids
    assert not settings().admin_ids
    configured = settings(telegram_allowed_chat_ids="-100, -200", telegram_owner_ids="123")
    assert configured.allowed_chat_ids == {-100, -200}
    assert configured.admin_ids == {123}


@pytest.mark.parametrize("overrides", [
    {"telegram_allowed_chat_ids": "100"}, {"telegram_admin_ids": "-100"},
    {"telegram_allowed_chat_ids": "-100,"}, {"reply_probability": 1.1},
    {"ai_timeout_seconds": 0}, {"reply_delay_min_seconds": 5, "reply_delay_max_seconds": 2},
    {"decision_confidence_threshold": float("nan")},
])
def test_invalid_settings(overrides):
    with pytest.raises(ValidationError):
        settings(**overrides)


def test_health_requires_fresh_successful_poll(tmp_path):
    path = tmp_path / "heartbeat"
    assert not healthy(path, 180)
    path.write_text("invalid", encoding="utf-8")
    assert not healthy(path, 180)
    path.write_text(json.dumps({"timestamp": time.time() - 181}), encoding="utf-8")
    assert not healthy(path, 180)
    path.write_text(json.dumps({"timestamp": time.time()}), encoding="utf-8")
    assert healthy(path, 180)


def test_logs_redact_secrets_and_omit_exception_bodies():
    formatter = JsonFormatter(["fake-api-secret"])
    record = logging.LogRecord("app.test", logging.ERROR, "", 1, "failed fake-api-secret", (), None)
    assert "fake-api-secret" not in formatter.format(record)
    library = logging.LogRecord("openai", logging.ERROR, "", 1, "private chat content", (), None)
    assert "private chat content" not in formatter.format(library)


@pytest.mark.asyncio
async def test_only_successful_polling_refreshes_health(tmp_path, caplog):
    path = tmp_path / "heartbeat"
    heartbeat = PollingHeartbeat(path)
    request = AsyncMock(return_value=[])
    await heartbeat(request, None, GetMe())
    assert not path.exists()
    await heartbeat(request, None, GetUpdates())
    assert healthy(path, 180)
    previous = path.read_text()
    request.side_effect = RuntimeError("private upstream response body")
    with pytest.raises(RuntimeError):
        await heartbeat(request, None, GetUpdates())
    assert path.read_text() == previous
    assert "private upstream response body" not in caplog.text
