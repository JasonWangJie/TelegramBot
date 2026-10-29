import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, Update, User

from app.config import Settings
from app.handlers.commands import create_command_router
from app.handlers.messages import MessageController, is_direct
from app.services.rate_limiter import RateLimiter
from app.storage.database import Database
from app.storage.messages import MessageStore

ME = User(id=999, is_bot=True, first_name="AI", username="companion_bot")


def message(number=1, *, chat_id=-100, text="@companion_bot 你好", user_id=10, is_bot=False, thread=None):
    entities = [MessageEntity(type="mention", offset=0, length=14)] if text.startswith("@companion_bot") else []
    return Message(message_id=number, date=datetime.now(timezone.utc),
                   chat=Chat(id=chat_id, type="supergroup"),
                   from_user=User(id=user_id, is_bot=is_bot, first_name="Alice"),
                   text=text, entities=entities, message_thread_id=thread)


@pytest_asyncio.fixture
async def runtime(tmp_path):
    settings = Settings(_env_file=None, telegram_bot_token="999:" + "x" * 35,
                        new_api_base_url="https://example.test/v1", new_api_key="test-key",
                        new_api_model="gateway-model", telegram_allowed_chat_ids="-100,-200",
                        telegram_owner_ids="10", message_batch_window_seconds=0.01,
                        reply_delay_min_seconds=0, reply_delay_max_seconds=0)
    db = Database(str(tmp_path / "test.db"))
    await db.open()
    store = MessageStore(db)
    bot = AsyncMock()
    bot.send_message.return_value = message(1000, user_id=999, is_bot=True, text="机器人回复")
    decision = AsyncMock()
    # is_candidate is synchronous.
    decision.is_candidate = lambda text, direct=False: direct or "?" in text
    decision.should_reply.return_value = True
    conversation = AsyncMock()
    conversation.generate.return_value = "机器人回复"
    controller = MessageController(settings, bot, ME, store, RateLimiter(db), decision, conversation)
    yield controller
    await controller.close()
    await db.close()


async def drain(controller):
    for _ in range(10):
        tasks = list(controller.workers.values())
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    raise AssertionError("workers did not drain")


@pytest.mark.asyncio
async def test_allowlist_bots_and_commands_do_not_store_or_call_ai(runtime):
    for incoming in [message(chat_id=-300), message(is_bot=True), message(text="/status")]:
        await runtime.accept(incoming)
    assert await runtime.store.count(-300) == 0
    assert await runtime.store.count(-100) == 0
    runtime.decision.should_reply.assert_not_called()
    runtime.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_burst_and_duplicate_make_one_reply(runtime):
    await asyncio.gather(*(runtime.accept(message(i)) for i in range(1, 8)))
    await runtime.accept(message(1))
    await drain(runtime)
    runtime.bot.send_message.assert_awaited_once()
    assert await runtime.store.count(-100) == 8


@pytest.mark.asyncio
async def test_direct_requests_obey_hard_rate_limit(runtime):
    await runtime.accept(message(1))
    await drain(runtime)
    await runtime.accept(message(2, text="@companion_bot 另一个问题"))
    await drain(runtime)
    runtime.bot.send_message.assert_awaited_once()
    runtime.conversation.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_pause_and_resume_invalidates_inflight_generation(runtime):
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(*args):
        started.set()
        await release.wait()
        return "过时的回答"

    runtime.conversation.generate.side_effect = generate
    await runtime.accept(message())
    await asyncio.wait_for(started.wait(), 2)
    await runtime.set_paused(-100, True)
    await runtime.accept(message(2, text="@companion_bot 暂停时不应记录"))
    assert await runtime.store.count(-100) == 1
    await runtime.set_paused(-100, False)
    release.set()
    await drain(runtime)
    runtime.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_topics_do_not_share_model_context_and_send_to_same_topic(runtime):
    await runtime.accept(message(1, text="这个是另一个话题的秘密", thread=20))
    await runtime.accept(message(2, thread=30))
    await drain(runtime)
    context = runtime.conversation.generate.call_args.args[0]
    assert all(item.thread_id == 30 for item in context)
    assert runtime.bot.send_message.call_args.kwargs["message_thread_id"] == 30


@pytest.mark.asyncio
async def test_ai_failure_does_not_send_fallback_or_leak_exception(runtime):
    runtime.conversation.generate.side_effect = RuntimeError("secret response body")
    await runtime.accept(message())
    await drain(runtime)
    runtime.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_other_group_proceeds_while_first_group_generation_waits(runtime):
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(context, trigger):
        if trigger.chat_id == -100:
            started.set()
            await release.wait()
        return "回复"

    runtime.conversation.generate.side_effect = generate
    await runtime.accept(message())
    await asyncio.wait_for(started.wait(), 2)
    await runtime.accept(message(chat_id=-200))
    await asyncio.wait_for(runtime.workers[-200], 2)
    assert runtime.bot.send_message.call_args.kwargs["chat_id"] == -200
    release.set()
    await drain(runtime)


def test_mentions_use_utf16_entities_not_substrings():
    incoming = message(text="😀 @companion_bot 你好")
    incoming = incoming.model_copy(update={
        "entities": [MessageEntity(type="mention", offset=3, length=14)]
    })
    assert is_direct(incoming, ME)
    assert not is_direct(message(text="x@companion_bot_suffix"), ME)
    incoming = incoming.model_copy(update={"reply_to_message": message(user_id=999, is_bot=True)})
    assert is_direct(incoming, ME)


@pytest.mark.asyncio
async def test_commands_require_explicit_admin_and_allowlist(runtime):
    bot = Bot("999:" + "x" * 35)
    bot.session.make_request = AsyncMock(return_value=message(50, is_bot=True))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_command_router(runtime.settings, runtime))
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=message(text="/pause", user_id=20)))
        assert not await runtime.store.is_paused(-100)
        await dispatcher.feed_update(bot, Update(update_id=2, message=message(text="/pause", chat_id=-300)))
        assert not await runtime.store.is_paused(-300)
        await dispatcher.feed_update(bot, Update(update_id=3, message=message(text="/pause")))
        assert await runtime.store.is_paused(-100)
        assert bot.session.make_request.await_count == 1
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_telegram_429_defers_chat_without_automatic_resend(runtime):
    now = [1000.0]
    runtime.limiter = RateLimiter(runtime.store.db, clock=lambda: now[0])
    runtime.bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=-100, text="reply"), message="Too Many Requests", retry_after=120
    )
    await runtime.accept(message())
    await drain(runtime)
    status = await runtime.limiter.check(-100)
    assert status.reason == "telegram_backoff"
    assert status.retry_after == 120
    assert status.remaining == 11
    assert await runtime.store.count(-100) == 1
    now[0] += 30  # The normal 20-second reply interval has expired, but the 429 has not.
    await runtime.accept(message(2, text="@companion_bot 再问一个不同的问题"))
    await drain(runtime)
    runtime.bot.send_message.assert_awaited_once()
    runtime.conversation.generate.assert_awaited_once()
    runtime.bot.send_message.side_effect = None
    now[0] = 1120.0
    await runtime.accept(message(3, text="@companion_bot 等待结束后的问题"))
    await drain(runtime)
    assert runtime.bot.send_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["api_timeout", "unknown_delivery"])
async def test_uncertain_send_consumes_persistent_quota_without_retry(runtime, caplog, error_kind):
    runtime.limiter = RateLimiter(
        runtime.store.db, min_interval_seconds=0, cooldown_seconds=0,
        max_per_hour=1, clock=lambda: 1000.0,
    )
    secret = "private-upstream-response-body"
    error = (
        TelegramNetworkError(method=SendMessage(chat_id=-100, text="reply"), message=secret)
        if error_kind == "api_timeout" else TimeoutError(secret)
    )
    runtime.bot.send_message.side_effect = error
    await runtime.accept(message())
    await drain(runtime)
    assert await runtime.store.count(-100) == 1
    # Recreating the limiter must retain the reserved quota after ambiguous delivery.
    runtime.limiter = RateLimiter(
        runtime.store.db, min_interval_seconds=0, cooldown_seconds=0,
        max_per_hour=1, clock=lambda: 1000.0,
    )
    status = await runtime.limiter.check(-100)
    assert not status.allowed
    assert status.reason == "hourly_quota"
    assert status.remaining == 0
    await runtime.accept(message(2, text="@companion_bot 又一个问题"))
    await drain(runtime)
    runtime.bot.send_message.assert_awaited_once()
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_stale_backlog_is_not_stored_or_answered(runtime):
    incoming = message().model_copy(update={
        "date": datetime.now(timezone.utc) - timedelta(seconds=601)
    })
    await runtime.accept(incoming)
    await drain(runtime)
    assert await runtime.store.count(-100) == 0
    runtime.decision.should_reply.assert_not_called()
    runtime.bot.send_message.assert_not_called()


def test_caption_and_text_mention_are_direct():
    incoming = message(text="photo").model_copy(update={
        "text": None,
        "caption": "@companion_bot 这是什么？",
        "caption_entities": [MessageEntity(type="mention", offset=0, length=14)],
    })
    assert is_direct(incoming, ME)
    incoming = message(text="AI 请回答").model_copy(update={
        "entities": [MessageEntity(type="text_mention", offset=0, length=2, user=ME)]
    })
    assert is_direct(incoming, ME)


@pytest.mark.asyncio
async def test_command_mentions_only_route_to_this_bot(runtime):
    bot = Bot("999:" + "x" * 35)
    bot.get_me = AsyncMock(return_value=ME)
    bot.session.make_request = AsyncMock(return_value=message(50, is_bot=True))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_command_router(runtime.settings, runtime))
    try:
        await dispatcher.feed_update(
            bot, Update(update_id=1, message=message(text="/pause@some_other_bot"))
        )
        assert not await runtime.store.is_paused(-100)
        bot.session.make_request.assert_not_called()
        await dispatcher.feed_update(
            bot, Update(update_id=2, message=message(text="/pause@companion_bot"))
        )
        assert await runtime.store.is_paused(-100)
        bot.session.make_request.assert_awaited_once()
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_anonymous_sender_cannot_use_admin_command(runtime):
    bot = Bot("999:" + "x" * 35)
    bot.session.make_request = AsyncMock(return_value=message(50, is_bot=True))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_command_router(runtime.settings, runtime))
    incoming = message(text="/pause").model_copy(update={
        "sender_chat": Chat(id=-100, type="supergroup")
    })
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=incoming))
        assert not await runtime.store.is_paused(-100)
        bot.session.make_request.assert_not_called()
    finally:
        await bot.session.close()
