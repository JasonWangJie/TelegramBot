import asyncio
import logging
import random
import time
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters, User

from app.config import Settings
from app.services.conversation import ConversationService
from app.services.decision import DecisionService
from app.services.rate_limiter import RateLimiter
from app.storage.messages import MessageStore, StoredMessage

logger = logging.getLogger(__name__)


def is_direct(message: Message, me: User) -> bool:
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == me.id:
        return True
    text = message.text or message.caption or ""
    for entity in message.entities or message.caption_entities or []:
        if entity.type == "text_mention" and entity.user and entity.user.id == me.id:
            return True
        if entity.type == "mention" and me.username:
            if entity.extract_from(text).casefold() == f"@{me.username}".casefold():
                return True
    return False


@dataclass
class Pending:
    message: Message
    stored: StoredMessage
    direct: bool
    epoch: int


class MessageController:
    """One bounded pending slot and worker per allowed group; no per-message AI tasks."""

    def __init__(self, settings: Settings, bot: Bot, me: User, store: MessageStore,
                 limiter: RateLimiter, decision: DecisionService, conversation: ConversationService):
        self.settings, self.bot, self.me = settings, bot, me
        self.store, self.limiter = store, limiter
        self.decision, self.conversation = decision, conversation
        self.pending: dict[int, Pending] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.epochs: dict[int, int] = {}
        self.closed = False

    async def accept(self, message: Message) -> None:
        chat_id = message.chat.id
        if (self.closed or message.chat.type not in {"group", "supergroup"}
                or chat_id not in self.settings.allowed_chat_ids
                or not message.from_user or message.from_user.is_bot):
            return
        text = (message.text or message.caption or "").strip()
        if not text or text.startswith("/"):
            return
        # Do not answer stale backlog after a long outage, including old dedup entries.
        if time.time() - message.date.timestamp() > self.settings.message_max_age_seconds:
            return
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            if self.closed or await self.store.is_paused(chat_id):
                return
            stored = StoredMessage(
                chat_id=chat_id, message_id=message.message_id, user_id=message.from_user.id,
                username=(message.from_user.username or message.from_user.full_name)[:100],
                role="user", content=text[:self.settings.message_max_chars],
                created_at=message.date.timestamp(), thread_id=message.message_thread_id,
            )
            if not await self.store.add(stored):
                return
            direct = is_direct(message, self.me)
            if not self.decision.is_candidate(text, direct=direct):
                return
            recent = await self.store.recent(chat_id, limit=10, thread_id=stored.thread_id)
            if any(item.message_id != stored.message_id and item.role == "user"
                   and item.user_id == stored.user_id and item.content == stored.content
                   and abs(stored.created_at - item.created_at) < 60 for item in recent):
                return
            current = self.pending.get(chat_id)
            # Keep the newest direct request over ordinary chatter within a burst.
            if current is None or direct or not current.direct:
                self.pending[chat_id] = Pending(message, stored, direct, self.epochs.get(chat_id, 0))
            if chat_id not in self.workers:
                self.workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    async def _worker(self, chat_id: int) -> None:
        try:
            while chat_id in self.pending and not self.closed:
                await asyncio.sleep(self.settings.message_batch_window_seconds)
                item = self.pending.pop(chat_id, None)
                if item:
                    try:
                        await self._process(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("message_processing_failed", extra={"chat_id": chat_id,
                                     "error_type": type(exc).__name__})
        finally:
            self.workers.pop(chat_id, None)

    async def _active(self, item: Pending) -> bool:
        return (not self.closed and self.epochs.get(item.stored.chat_id, 0) == item.epoch
                and not await self.store.is_paused(item.stored.chat_id))

    async def _process(self, item: Pending) -> None:
        chat_id = item.stored.chat_id
        if not await self._active(item):
            return
        status = await self.limiter.try_start_attempt(chat_id)
        if not status.allowed:
            logger.debug("rate_limited", extra={"chat_id": chat_id, "reason": status.reason})
            return
        context = await self.store.recent(chat_id, thread_id=item.stored.thread_id)
        if not await self.decision.should_reply(context, item.stored, direct=item.direct):
            return
        if not await self._active(item) or not (await self.limiter.check(chat_id)).allowed:
            return
        reply = await self.conversation.generate(context, item.stored)
        if not reply:
            return
        await asyncio.sleep(random.uniform(self.settings.reply_delay_min_seconds,
                                           self.settings.reply_delay_max_seconds))
        # Commands use this short lock too, so pause is ordered with the send operation.
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            if not await self._active(item) or not (await self.limiter.check(chat_id)).allowed:
                return
            # Reserve persistently before network I/O. A crash or uncertain delivery
            # must not reset a quota; failed sends conservatively consume this slot.
            reservation_id = await self.limiter.record_reply(chat_id)
            try:
                sent = await self.bot.send_message(
                    chat_id=chat_id, text=reply, message_thread_id=item.stored.thread_id,
                    reply_parameters=ReplyParameters(message_id=item.stored.message_id,
                                                      allow_sending_without_reply=True),
                    parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramRetryAfter as exc:
                await self.limiter.defer(chat_id, exc.retry_after)
                logger.warning("telegram_backoff", extra={"chat_id": chat_id})
                return
            except TelegramAPIError as exc:
                # Delivery may have succeeded before a network failure; never resend blindly.
                logger.warning("telegram_send_failed", extra={"chat_id": chat_id,
                               "error_type": type(exc).__name__})
                return
            await self.limiter.finalize_reply(chat_id, reservation_id)
            await self.store.add(StoredMessage(
                chat_id=chat_id, message_id=sent.message_id, user_id=self.me.id,
                username=self.me.username, role="assistant", content=reply,
                created_at=sent.date.timestamp(), thread_id=item.stored.thread_id,
            ))
            logger.info("reply_sent", extra={"chat_id": chat_id, "message_id": sent.message_id})

    async def set_paused(self, chat_id: int, paused: bool) -> None:
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            self.epochs[chat_id] = self.epochs.get(chat_id, 0) + 1
            self.pending.pop(chat_id, None)
            await self.store.set_paused(chat_id, paused)

    async def close(self) -> None:
        self.closed = True
        self.pending.clear()
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_message_router(controller: MessageController) -> Router:
    router = Router(name="messages")

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def message_handler(message: Message):
        await controller.accept(message)

    return router
