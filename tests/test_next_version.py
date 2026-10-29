import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from aiogram.types import Chat, Message, MessageEntity, User
from openai import AsyncOpenAI

from app.config import Settings
from app.handlers.messages import MessageController
from app.services.ai_client import AIClient
from app.services.conversation import ConversationService
from app.services.decision import DecisionService
from app.services.rate_limiter import RateLimiter
from app.storage.database import Database
from app.storage.messages import MessageStore
from app.storage.usage import UsageStore

ME = User(id=999, is_bot=True, first_name="AI", username="companion_bot")


def incoming(number=1, text="@companion_bot 你好", user=10, thread=None, chat=-100):
    return Message(message_id=number, chat=Chat(id=chat, type="supergroup"),
                   from_user=User(id=user, is_bot=False, first_name="user"),
                   date=datetime.now(timezone.utc), text=text, message_thread_id=thread,
                   entities=[MessageEntity(type="mention", offset=0, length=14)]
                   if text.startswith("@companion_bot") else [])


async def drain(controller):
    for _ in range(10):
        tasks = list(controller.workers.values())
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    raise AssertionError("worker did not drain")


@pytest_asyncio.fixture
async def runtime(tmp_path):
    settings = Settings(_env_file=None, telegram_bot_token="999:" + "x" * 35,
                        telegram_allowed_chat_ids="-100,-200", telegram_owner_ids="10",
                        new_api_base_url="https://gateway.invalid/v1", new_api_key="dummy-key",
                        new_api_model="chat-model", decision_model="decision-model",
                        reply_probability=1, min_reply_interval_seconds=0, chat_cooldown_seconds=0,
                        message_batch_window_seconds=0.01, max_replies_per_hour=100,
                        reply_delay_min_seconds=0, reply_delay_max_seconds=0, ai_max_retries=0)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        content = ('{"action":"reply","reason":"question","confidence":1}'
                   if payload["model"] == "decision-model" else "这是回答")
        return httpx.Response(200, json={"id": "test", "created": 1, "model": payload["model"],
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    db = Database(str(tmp_path / "bot.db"))
    await db.open()
    usage = UsageStore(db, global_daily_limit=100, default_chat_daily_limit=100)
    sdk = AsyncOpenAI(api_key="dummy-key", base_url=settings.new_api_base_url,
                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ai = AIClient(settings, client=sdk, usage=usage)
    bot = AsyncMock()
    bot.send_message.side_effect = lambda **kw: incoming(1000 + len(requests), kw["text"], user=999,
                                                        thread=kw.get("message_thread_id"), chat=kw["chat_id"])
    controller = MessageController(settings, bot, ME, MessageStore(db),
                                   RateLimiter(db, min_interval_seconds=0, cooldown_seconds=0, max_per_hour=100),
                                   DecisionService(settings, ai), ConversationService(settings, ai), usage=usage)
    yield controller, requests
    await controller.close()
    await ai.close()
    await db.close()


@pytest.mark.asyncio
async def test_followup_only_same_user_and_topic_skips_decision(runtime):
    controller, requests = runtime
    await controller.accept(incoming())
    await drain(controller)
    await controller.accept(incoming(2, "还有呢", user=20))
    await controller.accept(incoming(3, "还有呢", thread=40))
    await drain(controller)
    assert len(requests) == 1
    await controller.accept(incoming(4, "还有呢"))
    await drain(controller)
    assert [request["model"] for request in requests] == ["chat-model", "chat-model"]
    snapshot = await controller.usage.snapshot(-100)
    assert snapshot["reply_calls"] == 2
    assert snapshot["events"]["followup"] == 1


@pytest.mark.asyncio
async def test_quiet_mode_and_quiet_hours_block_before_ai(runtime):
    controller, requests = runtime
    await controller.update_policy(-100, {"mode": "quiet"})
    await controller.accept(incoming(1, "如何学习 Python？"))
    await drain(controller)
    assert not requests
    await controller.accept(incoming(2))
    await drain(controller)
    assert len(requests) == 1
    # Two complementary time ranges cover the whole day without a clock assumption.
    from app.storage.group_settings import is_quiet

    policy = await controller.update_policy(-100, {"quiet_hours": "00:00-12:00"})
    if not is_quiet(policy, controller.settings.bot_timezone):
        await controller.update_policy(-100, {"quiet_hours": "12:00-00:00"})
    await controller.accept(incoming(3, "@companion_bot 新问题"))
    await drain(controller)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_dynamic_persona_applies_only_to_its_group(runtime):
    controller, requests = runtime
    await controller.update_policy(-100, {"persona": "扮演天文科普伙伴"})
    await controller.accept(incoming())
    await drain(controller)
    await controller.accept(incoming(1, chat=-200))
    await drain(controller)
    assert "天文科普伙伴" in requests[0]["messages"][0]["content"]
    assert "天文科普伙伴" not in requests[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_decision_spends_last_budget_slot_so_reply_is_not_sent(runtime):
    controller, requests = runtime
    await controller.update_policy(-100, {"daily_call_limit": 1})
    await controller.accept(incoming(1, "如何学习 Python？"))
    await drain(controller)
    assert [request["model"] for request in requests] == ["decision-model"]
    controller.bot.send_message.assert_not_called()
    stats = await controller.usage.snapshot(-100, chat_limit=1)
    assert stats["calls"] == 1
    assert stats["remaining_calls"] == 0
    assert stats["events"]["budget_exhausted"] == 1


@pytest.mark.asyncio
async def test_settings_change_invalidates_inflight_answer(runtime):
    controller, requests = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = controller.conversation.ai_client.chat

    async def slow_chat(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    controller.conversation.ai_client.chat = slow_chat
    await controller.accept(incoming())
    await asyncio.wait_for(entered.wait(), 2)
    await controller.update_policy(-100, {"persona": "新设定"})
    release.set()
    await drain(controller)
    controller.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_stop_signal_suppresses_pending_followup(runtime):
    controller, requests = runtime
    await controller.accept(incoming())
    await drain(controller)
    await controller.accept(incoming(2, "继续"))
    await controller.accept(incoming(3, "不用回复了"))
    await drain(controller)
    assert len(requests) == 1
    await controller.accept(incoming(4, "还有呢"))
    await drain(controller)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_user, expected_replies", [(10, 0), (20, 1)])
async def test_stop_during_first_answer_checks_request_owner(runtime, stop_user, expected_replies):
    controller, requests = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = controller.conversation.ai_client.chat

    async def slow_chat(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    controller.conversation.ai_client.chat = slow_chat
    await controller.accept(incoming())
    await asyncio.wait_for(entered.wait(), 2)
    await controller.accept(incoming(2, "不用回复了", user=stop_user))
    release.set()
    await drain(controller)
    assert controller.bot.send_message.await_count == expected_replies


@pytest.mark.asyncio
async def test_direct_stop_and_mentions_to_another_user(runtime):
    controller, requests = runtime
    await controller.accept(incoming())
    await drain(controller)
    other = User(id=20, is_bot=False, first_name="other")
    addressed_elsewhere = incoming(2, "不用回复了").model_copy(update={
        "entities": [MessageEntity(type="text_mention", offset=0, length=5, user=other)]})
    await controller.accept(addressed_elsewhere)
    await controller.accept(incoming(3, "还有呢"))
    await drain(controller)
    assert len(requests) == 2
    await controller.accept(incoming(4, "@companion_bot 不用回复了"))
    await controller.accept(incoming(5, "继续"))
    await drain(controller)
    assert len(requests) == 2
