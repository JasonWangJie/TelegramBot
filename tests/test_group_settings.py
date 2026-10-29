import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.config import Settings
from app.storage.database import Database
from app.storage.group_settings import GroupSettings, GroupSettingsStore, effective, is_quiet


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(str(tmp_path / "settings.db"))
    await db.open()
    yield GroupSettingsStore(db)
    await db.close()


def settings():
    return Settings(_env_file=None, telegram_bot_token="999:" + "x" * 35,
                    new_api_base_url="https://example.test/v1", new_api_key="test-key",
                    new_api_model="gateway-model")


@pytest.mark.asyncio
async def test_settings_persist_and_other_groups_keep_defaults(store):
    assert await store.get(-100) == GroupSettings()
    await store.update(-100, {"mode": "quiet", "persona": "  简洁的技术助手  ", "daily_call_limit": 0})
    await store.db.close()
    await store.db.open()
    assert (await store.get(-100)).mode == "quiet"
    assert (await store.get(-100)).persona == "简洁的技术助手"
    assert (await store.get(-100)).daily_call_limit == 0
    assert await store.get(-200) == GroupSettings()


@pytest.mark.asyncio
async def test_concurrent_partial_updates_do_not_lose_other_fields(store):
    await asyncio.gather(store.update(-100, {"mode": "active"}),
                         store.update(-100, {"persona": "友善的中文伙伴"}),
                         store.update(-100, {"daily_call_limit": 10}))
    result = await store.get(-100)
    assert result.mode == "active"
    assert result.persona == "友善的中文伙伴"
    assert result.daily_call_limit == 10


@pytest.mark.asyncio
async def test_invalid_update_preserves_existing_settings(store):
    before = await store.update(-100, {"reply_probability": 0.1})
    with pytest.raises(ValidationError):
        await store.update(-100, {"reply_probability": 2.0, "mode": "active"})
    assert await store.get(-100) == before
    with pytest.raises(ValidationError):
        await store.update(-100, {"new_api_key": "not-editable"})
    assert await store.get(-100) == before


@pytest.mark.asyncio
async def test_reset_restores_defaults_without_affecting_another_group(store):
    await store.update(-100, {"mode": "active", "quiet_hours": "23:00-08:00"})
    await store.update(-200, {"mode": "quiet"})
    assert await store.reset(-100) == GroupSettings()
    assert await store.get(-100) == GroupSettings()
    assert (await store.get(-200)).mode == "quiet"


@pytest.mark.parametrize("value", [
    {"mode": "loud"}, {"reply_probability": float("nan")},
    {"reply_probability": float("inf")}, {"reply_probability": 1.01},
    {"max_replies_per_hour": True}, {"max_replies_per_hour": 0},
    {"daily_call_limit": -1}, {"daily_call_limit": "100"},
    {"persona": " "}, {"persona": "a" * 4001},
    {"quiet_hours": "8:00-20:00"}, {"quiet_hours": "24:00-08:00"},
    {"quiet_hours": "08:00-08:00"}, {"quiet_hours": "08:60-10:00"},
    {"telegram_bot_token": "not-editable"},
])
def test_policy_rejects_invalid_or_unsafe_values(value):
    with pytest.raises(ValidationError):
        GroupSettings.model_validate(value)


def test_mode_presets_and_explicit_overrides_do_not_mutate_deployment_defaults():
    base = settings()
    active = effective(base, GroupSettings(mode="active"))
    assert (active.reply_probability, active.min_reply_interval_seconds,
            active.chat_cooldown_seconds, active.max_replies_per_hour) == (0.7, 10, 4, 24)
    customized = effective(base, GroupSettings(mode="active", reply_probability=0.0,
                                              persona="群里的技术助手", daily_call_limit=0))
    assert customized.reply_probability == 0
    assert customized.chat_daily_call_limit == 0
    assert customized.bot_persona == "群里的技术助手"
    assert base.reply_probability == 0.35
    assert effective(base, GroupSettings(mode="normal")).max_replies_per_hour == base.max_replies_per_hour
    assert effective(base, GroupSettings(mode="quiet")).max_replies_per_hour == base.max_replies_per_hour


@pytest.mark.parametrize("hour,minute,expected", [(22, 59, False), (23, 0, True),
                                                  (0, 0, True), (7, 59, True), (8, 0, False)])
def test_quiet_hours_cross_midnight_with_start_inclusive_end_exclusive(hour, minute, expected):
    policy = GroupSettings(quiet_hours="23:00-08:00")
    assert is_quiet(policy, "UTC", datetime(2026, 10, 29, hour, minute, tzinfo=timezone.utc)) is expected


def test_quiet_hours_use_configured_timezone_and_support_daytime_ranges():
    policy = GroupSettings(quiet_hours="09:00-17:00")
    now = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)
    assert is_quiet(policy, "Asia/Shanghai", now)
    assert not is_quiet(policy, "UTC", now)
    assert not is_quiet(GroupSettings(), "UTC", now)
    with pytest.raises(ValueError):
        is_quiet(policy, "UTC", datetime(2026, 10, 29, 1, 0))
