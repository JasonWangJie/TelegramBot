from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.config import Settings
from app.handlers.commands import create_command_router, parse_change
from app.storage.database import Database
from app.storage.group_settings import GroupSettingsStore, effective

BOT_USER = User(id=999, is_bot=True, first_name="AI", username="companion_bot")


def message(text="/settings", user_id=10, chat_id=-100, **updates):
    result = Message(message_id=1, date=datetime.now(timezone.utc), text=text,
                     chat=Chat(id=chat_id, type="supergroup"),
                     from_user=User(id=user_id, is_bot=False, first_name="User"))
    return result.model_copy(update=updates)


@pytest_asyncio.fixture
async def ui(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, telegram_bot_token="999:" + "x" * 35,
                        new_api_base_url="https://example.test/v1", new_api_key="test-key",
                        new_api_model="gateway-model", telegram_allowed_chat_ids="-100,-200",
                        telegram_owner_ids="10", telegram_admin_ids="20")
    db = Database(str(tmp_path / "ui.db"))
    await db.open()
    store = GroupSettingsStore(db)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    bot.get_me = AsyncMock(return_value=BOT_USER)
    bot.session.make_request = AsyncMock(return_value=message("result", from_user=BOT_USER))

    async def effective_settings(chat_id):
        return effective(settings, await store.get(chat_id))

    snapshot = dict(day="2026-10-29", timezone="Asia/Shanghai", calls=4, decision_calls=3,
                    reply_calls=1, successes=3, failures=1, pending_calls=0,
                    remaining_calls=196, prompt_tokens=40, completion_tokens=10,
                    missing_usage_calls=1, average_latency_seconds=0.5,
                    estimated_cost_usd=None, events={"model_skip": 2, "reply_sent": 1},
                    last_event="reply_sent", last_error_type="APIConnectionError")
    controller = SimpleNamespace(get_policy=store.get,
                                 update_policy=AsyncMock(side_effect=store.update),
                                 reset_policy=AsyncMock(side_effect=store.reset),
                                 effective_settings=effective_settings,
                                 usage=SimpleNamespace(snapshot=AsyncMock(return_value=snapshot)))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_command_router(settings, controller))
    clock = [100.0]
    monkeypatch.setattr("app.handlers.commands.time", SimpleNamespace(monotonic=lambda: clock[0]))
    update_id = [0]

    async def feed(incoming):
        clock[0] += 3
        update_id[0] += 1
        update = (Update(update_id=update_id[0], callback_query=incoming)
                  if isinstance(incoming, CallbackQuery) else Update(update_id=update_id[0], message=incoming))
        await dispatcher.feed_update(bot, update)

    yield SimpleNamespace(bot=bot, feed=feed, store=store, controller=controller, clock=clock,
                          dispatcher=dispatcher)
    await bot.session.close()
    await db.close()


@pytest.mark.asyncio
async def test_settings_ui_shows_real_values_and_mode_buttons(ui):
    await ui.feed(message())
    request = ui.bot.session.make_request.call_args.args[1]
    assert isinstance(request, SendMessage)
    assert "普通" in request.text
    assert "每日调用上限：200" in request.text
    assert {button.callback_data for row in request.reply_markup.inline_keyboard for button in row} == {
        "settings:mode:quiet", "settings:mode:normal", "settings:mode:active"
    }


@pytest.mark.asyncio
async def test_settings_commands_persist_custom_values_and_support_reset(ui):
    await ui.feed(message("/settings persona 简洁的技术群助手"))
    await ui.feed(message("/settings daily_call_limit 0"))
    await ui.feed(message("/mode active"))
    policy = await ui.store.get(-100)
    assert policy.mode == "active"
    assert policy.persona == "简洁的技术群助手"
    assert policy.daily_call_limit == 0
    await ui.feed(message("/settings persona default"))
    assert (await ui.store.get(-100)).persona is None
    await ui.feed(message("/settings reset"))
    assert (await ui.store.get(-100)).mode == "normal"
    assert (await ui.store.get(-100)).daily_call_limit is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/settings reply_probability nan", "/settings new_api_key anything",
                                 "/settings quiet_hours 25:00-08:00", "/settings daily_call_limit -1"])
async def test_invalid_settings_do_not_change_saved_policy(ui, text):
    await ui.feed(message(text))
    assert (await ui.store.get(-100)).reply_probability is None
    assert (await ui.store.get(-100)).daily_call_limit is None
    request = ui.bot.session.make_request.call_args.args[1]
    assert "设置无效" in request.text


@pytest.mark.asyncio
async def test_nonadmin_unlisted_groups_and_anonymous_senders_cannot_change_settings(ui):
    await ui.feed(message("/mode active", user_id=30))
    await ui.feed(message("/mode active", chat_id=-300))
    await ui.feed(message("/mode active", sender_chat=Chat(id=-100, type="supergroup")))
    ui.controller.update_policy.assert_not_called()
    ui.bot.session.make_request.assert_not_called()


def callback(*, user_id=10, chat_id=-100, source_user=BOT_USER, data="settings:mode:active"):
    return CallbackQuery(id="test-query", chat_instance="instance", data=data,
                         from_user=User(id=user_id, is_bot=False, first_name="User"),
                         message=message("settings", chat_id=chat_id, from_user=source_user))


@pytest.mark.asyncio
async def test_valid_callback_updates_only_source_group_and_acknowledges(ui):
    await ui.feed(callback())
    assert (await ui.store.get(-100)).mode == "active"
    assert (await ui.store.get(-200)).mode == "normal"
    requests = [call.args[1] for call in ui.bot.session.make_request.call_args_list]
    assert isinstance(requests[0], AnswerCallbackQuery)
    assert any(isinstance(request, EditMessageText) for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"user_id": 30}, {"chat_id": -300},
    {"source_user": User(id=888, is_bot=True, first_name="Other bot")},
    {"source_user": User(id=10, is_bot=False, first_name="Admin")},
    {"data": "settings:mode:unrecognized"},
])
async def test_untrusted_callbacks_always_acknowledge_without_writing(ui, options):
    await ui.feed(callback(**options))
    ui.controller.update_policy.assert_not_called()
    requests = [call.args[1] for call in ui.bot.session.make_request.call_args_list]
    assert len(requests) == 1
    assert isinstance(requests[0], AnswerCallbackQuery)


@pytest.mark.asyncio
async def test_group_stats_are_scoped_and_global_stats_require_owner(ui):
    await ui.feed(message("/stats", user_id=20))
    ui.controller.usage.snapshot.assert_awaited_with(-100, chat_limit=200)
    request = ui.bot.session.make_request.call_args.args[1]
    assert "输入 40 / 输出 10" in request.text
    assert "模型判断无需回应=2" in request.text
    assert "失败率：25.0%" in request.text
    ui.controller.usage.snapshot.reset_mock()
    await ui.feed(message("/stats global", user_id=20))
    ui.controller.usage.snapshot.assert_not_called()
    await ui.feed(message("/stats global", user_id=10))
    ui.controller.usage.snapshot.assert_awaited_with(None, chat_limit=200)


@pytest.mark.parametrize("arguments, expected", [
    ("daily_call_limit 0", {"daily_call_limit": 0}),
    ("reply_probability 0.5", {"reply_probability": 0.5}),
    ("persona 带空格 的性格", {"persona": "带空格 的性格"}),
    ("quiet_hours off", {"quiet_hours": None}),
    ("reply_probability default", {"reply_probability": None}),
])
def test_command_parser_keeps_typed_values_and_reset_semantics(arguments, expected):
    assert parse_change(arguments) == expected


@pytest.mark.asyncio
async def test_partial_cost_estimate_never_looks_like_complete_bill(ui):
    snapshot = ui.controller.usage.snapshot.return_value
    snapshot.update(estimated_cost_usd=0.001, cost_estimate_complete=False, priced_calls=2)
    await ui.feed(message("/stats"))
    text = ui.bot.session.make_request.call_args.args[1].text
    assert "仅部分调用可估算，非完整费用" in text
    assert "已计价 2 次" in text


@pytest.mark.asyncio
async def test_clicking_current_mode_only_acknowledges_without_editing_unchanged_message(ui):
    await ui.feed(callback(data="settings:mode:normal"))
    ui.controller.update_policy.assert_not_called()
    requests = [call.args[1] for call in ui.bot.session.make_request.call_args_list]
    assert len(requests) == 1
    assert isinstance(requests[0], AnswerCallbackQuery)
    assert "当前已是普通模式" in requests[0].text
