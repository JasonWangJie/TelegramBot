import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from pydantic import ValidationError

from app.config import Settings
from app.handlers.commands import create_command_router
from app.handlers.messages import MessageController, create_message_router
from app.healthcheck import PollingHeartbeat
from app.logging_setup import setup_logging
from app.services.ai_client import AIClient
from app.services.conversation import ConversationService
from app.services.decision import DecisionService
from app.services.rate_limiter import RateLimiter
from app.storage.database import Database
from app.storage.group_settings import GroupSettingsStore
from app.storage.messages import MessageStore
from app.storage.usage import UsageStore

logger = logging.getLogger(__name__)


async def maintain(store: MessageStore, usage: UsageStore) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await store.cleanup()
            await usage.cleanup()
        except Exception as exc:
            logger.error("cleanup_failed", extra={"error_type": type(exc).__name__})


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level, [settings.telegram_bot_token.get_secret_value(),
                                      settings.new_api_key.get_secret_value()])
    if not settings.allowed_chat_ids:
        logger.warning("empty_allowlist_all_groups_disabled")
    if not settings.admin_ids:
        logger.warning("empty_admin_list_management_disabled")
    async with contextlib.AsyncExitStack() as stack:
        db = Database(str(settings.database_path))
        await db.open()
        stack.push_async_callback(db.close)
        store = MessageStore(db, max_messages=settings.context_max_messages,
                             retention_days=settings.history_retention_days)
        await store.cleanup()
        usage = UsageStore(db, timezone_name=settings.bot_timezone,
                           global_daily_limit=settings.global_daily_call_limit,
                           default_chat_daily_limit=settings.chat_daily_call_limit,
                           retention_days=settings.usage_retention_days)
        await usage.cleanup()
        ai = AIClient(settings, usage=usage)
        stack.push_async_callback(ai.close)
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        stack.push_async_callback(bot.session.close)
        # Do not remove an existing webhook silently or discard queued Telegram updates.
        if (await bot.get_webhook_info()).url:
            logger.error("existing_webhook_polling_disabled")
            raise RuntimeError("webhook_configured_remove_before_polling")
        me = await bot.get_me()
        limiter = RateLimiter(db, min_interval_seconds=settings.min_reply_interval_seconds,
                              cooldown_seconds=settings.chat_cooldown_seconds,
                              max_per_hour=settings.max_replies_per_hour)
        controller = MessageController(settings, bot, me, store, limiter,
                                       DecisionService(settings, ai), ConversationService(settings, ai),
                                       group_settings=GroupSettingsStore(db), usage=usage)
        stack.push_async_callback(controller.close)
        cleanup_task = asyncio.create_task(maintain(store, usage))

        async def stop_cleanup():
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task

        stack.push_async_callback(stop_cleanup)
        settings.heartbeat_path.unlink(missing_ok=True)
        bot.session.middleware(PollingHeartbeat(settings.heartbeat_path))
        dispatcher = Dispatcher()
        dispatcher.include_router(create_command_router(settings, controller))
        dispatcher.include_router(create_message_router(controller))

        @dispatcher.errors()
        async def handle_error(event: ErrorEvent):
            logger.error("update_failed", extra={"error_type": type(event.exception).__name__})
            return True

        logger.info("polling_started", extra={"count": len(settings.allowed_chat_ids)})
        try:
            await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"], polling_timeout=30,
                                            tasks_concurrency_limit=100, close_bot_session=False,
                                            handle_signals=os.name != "nt")
        finally:
            settings.heartbeat_path.unlink(missing_ok=True)
            logger.info("polling_stopped")


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        # Pydantic's default repr includes invalid input values, potentially credentials.
        fields = ", ".join(".".join(str(part) for part in item["loc"]) or "settings"
                           for item in exc.errors(include_input=False, include_context=False))
        print(f"配置校验失败，请检查 .env 字段：{fields}")
        raise SystemExit(2) from None
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("startup_or_runtime_failed", extra={"error_type": type(exc).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
