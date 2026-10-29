import asyncio
import contextlib
import logging
import random
import re
import time
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters, User

from app.config import Settings
from app.services.ai_client import AIError
from app.services.conversation import ConversationService
from app.services.decision import DecisionService
from app.services.followup import FollowupTracker, is_stop_signal
from app.services.rate_limiter import RateLimiter
from app.storage.group_settings import GroupSettingsStore, effective, is_quiet
from app.storage.messages import MessageStore, StoredMessage
from app.storage.usage import UsageStore

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
    followup: bool = False
    cancelled: bool = False


class MessageController:
    """One bounded pending slot and worker per allowed group; no per-message AI tasks."""

    def __init__(self, settings: Settings, bot: Bot, me: User, store: MessageStore,
                 limiter: RateLimiter, decision: DecisionService, conversation: ConversationService,
                 *, group_settings: GroupSettingsStore | None = None, usage: UsageStore | None = None):
        self.settings, self.bot, self.me = settings, bot, me
        self.store, self.limiter = store, limiter
        self.decision, self.conversation = decision, conversation
        self.group_settings = group_settings or GroupSettingsStore(store.db)
        self.usage = usage or UsageStore(store.db, timezone_name=settings.bot_timezone,
                                        global_daily_limit=settings.global_daily_call_limit,
                                        default_chat_daily_limit=settings.chat_daily_call_limit,
                                        retention_days=settings.usage_retention_days)
        self.followups = FollowupTracker(ttl_seconds=settings.followup_ttl_seconds)
        self.pending: dict[int, Pending] = {}
        self.workers: dict[int, asyncio.Task] = {}
        self.inflight: dict[int, Pending] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.epochs: dict[int, int] = {}
        self.closed = False

    async def get_policy(self, chat_id: int):
        return await self.group_settings.get(chat_id)

    async def effective_settings(self, chat_id: int) -> Settings:
        return effective(self.settings, await self.get_policy(chat_id))

    def limiter_for(self, settings: Settings) -> RateLimiter:
        fields = ("min_reply_interval_seconds", "chat_cooldown_seconds", "max_replies_per_hour")
        if all(getattr(settings, field) == getattr(self.settings, field) for field in fields):
            return self.limiter
        return RateLimiter(self.limiter.db, min_interval_seconds=settings.min_reply_interval_seconds,
                           cooldown_seconds=settings.chat_cooldown_seconds,
                           max_per_hour=settings.max_replies_per_hour, clock=self.limiter.clock)

    def _invalidate(self, chat_id: int) -> None:
        self.epochs[chat_id] = self.epochs.get(chat_id, 0) + 1
        self.pending.pop(chat_id, None)
        self.followups.clear_chat(chat_id)

    async def update_policy(self, chat_id: int, changes: dict):
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            policy = await self.group_settings.update(chat_id, changes)
            self._invalidate(chat_id)
            return policy

    async def reset_policy(self, chat_id: int):
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            policy = await self.group_settings.reset(chat_id)
            self._invalidate(chat_id)
            return policy

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
            if self.closed:
                return
            if await self.store.is_paused(chat_id):
                await self.usage.event(chat_id, "paused")
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
            replied_user = (message.reply_to_message.from_user.id
                            if message.reply_to_message and message.reply_to_message.from_user else None)
            continuation_text = text
            if direct and self.me.username:
                continuation_text = re.sub(rf"@{re.escape(self.me.username)}\b", "", text, flags=re.IGNORECASE).strip()
            if message.reply_to_message and replied_user is None:
                replied_user = 0  # Anonymous/channel replies are not implicitly addressed to this bot.
            other_target = any(entity.type == "text_mention" and entity.user and entity.user.id != self.me.id
                               for entity in message.entities or message.caption_entities or [])
            continuation = ("none" if other_target else self.followups.classify(
                chat_id, stored.thread_id, stored.user_id, continuation_text,
                reply_to_user_id=replied_user, bot_user_id=self.me.id))
            owned_requests = [candidate for candidate in (self.pending.get(chat_id), self.inflight.get(chat_id))
                              if candidate and candidate.stored.user_id == stored.user_id
                              and candidate.stored.thread_id == stored.thread_id]
            if (owned_requests and not other_target and replied_user in {None, self.me.id}
                    and is_stop_signal(continuation_text)):
                continuation = "stop"
            if continuation == "stop":
                # Invalidate this user's pending/in-flight answer without affecting other topics.
                self.followups.clear(chat_id, stored.thread_id)
                for candidate in owned_requests:
                    candidate.cancelled = True
                await self.usage.event(chat_id, "conversation_stopped")
                return
            policy = await self.get_policy(chat_id)
            if is_quiet(policy, self.settings.bot_timezone):
                await self.usage.event(chat_id, "quiet_hours")
                return
            if policy.mode == "quiet" and not direct:
                await self.usage.event(chat_id, "quiet_mode")
                return
            followup = continuation == "followup"
            if not self.decision.is_candidate(text, direct=direct or followup):
                await self.usage.event(chat_id, "local_filter")
                return
            recent = await self.store.recent(chat_id, limit=10, thread_id=stored.thread_id)
            if any(item.message_id != stored.message_id and item.role == "user"
                   and item.user_id == stored.user_id and item.content == stored.content
                   and abs(stored.created_at - item.created_at) < 60 for item in recent):
                await self.usage.event(chat_id, "repeated_content")
                return
            current = self.pending.get(chat_id)
            # Keep the newest direct request over ordinary chatter within a burst.
            if (current is None or current.cancelled or direct or (followup and not current.direct)
                    or not (current.direct or current.followup)):
                self.pending[chat_id] = Pending(message, stored, direct, self.epochs.get(chat_id, 0), followup)
            if chat_id not in self.workers:
                self.workers[chat_id] = asyncio.create_task(self._worker(chat_id))

    async def _worker(self, chat_id: int) -> None:
        try:
            while chat_id in self.pending and not self.closed:
                await asyncio.sleep(self.settings.message_batch_window_seconds)
                item = self.pending.pop(chat_id, None)
                if item:
                    self.inflight[chat_id] = item
                    try:
                        await self._process(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await self.usage.event(chat_id, "processing_error")
                        logger.error("message_processing_failed", extra={"chat_id": chat_id,
                                     "error_type": type(exc).__name__})
                    finally:
                        self.inflight.pop(chat_id, None)
        finally:
            self.workers.pop(chat_id, None)

    async def _active(self, item: Pending) -> bool:
        if (self.closed or item.cancelled or self.epochs.get(item.stored.chat_id, 0) != item.epoch
                or await self.store.is_paused(item.stored.chat_id)):
            return False
        policy = await self.get_policy(item.stored.chat_id)
        return not is_quiet(policy, self.settings.bot_timezone) and (policy.mode != "quiet" or item.direct)

    async def _process(self, item: Pending) -> None:
        chat_id = item.stored.chat_id
        if not await self._active(item):
            return
        settings = await self.effective_settings(chat_id)
        limiter = self.limiter_for(settings)
        status = await limiter.try_start_attempt(chat_id)
        if not status.allowed:
            await self.usage.event(chat_id, status.reason)
            logger.debug("rate_limited", extra={"chat_id": chat_id, "reason": status.reason})
            return
        context = await self.store.recent(chat_id, thread_id=item.stored.thread_id)
        decision = (DecisionService(settings, self.decision.ai_client)
                    if isinstance(self.decision, DecisionService) else self.decision)
        conversation = (ConversationService(settings, self.conversation.ai_client)
                        if isinstance(self.conversation, ConversationService) else self.conversation)
        ai = conversation.ai_client if isinstance(conversation, ConversationService) else None
        scope = ai.scope(chat_id, settings.chat_daily_call_limit) if ai else contextlib.nullcontext()
        with scope:
            if not item.followup:
                if not await decision.should_reply(context, item.stored, direct=item.direct):
                    reason = decision.last_reason if isinstance(decision, DecisionService) else "decision_skip"
                    await self.usage.event(chat_id, reason)
                    return
            else:
                await self.usage.event(chat_id, "followup")
            if not await self._active(item) or not (await limiter.check(chat_id)).allowed:
                return
            try:
                reply = await conversation.generate(context, item.stored)
            except AIError as exc:
                reason = "budget_exhausted" if str(exc) == "daily_budget_exhausted" else "reply_unavailable"
                await self.usage.event(chat_id, reason)
                return
        if not reply:
            await self.usage.event(chat_id, "empty_reply")
            return
        await asyncio.sleep(random.uniform(self.settings.reply_delay_min_seconds,
                                           self.settings.reply_delay_max_seconds))
        # Commands use this short lock too, so pause is ordered with the send operation.
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            if not await self._active(item) or not (await limiter.check(chat_id)).allowed:
                return
            # Reserve persistently before network I/O. A crash or uncertain delivery
            # must not reset a quota; failed sends conservatively consume this slot.
            reservation_id = await limiter.record_reply(chat_id)
            try:
                sent = await self.bot.send_message(
                    chat_id=chat_id, text=reply, message_thread_id=item.stored.thread_id,
                    reply_parameters=ReplyParameters(message_id=item.stored.message_id,
                                                      allow_sending_without_reply=True),
                    parse_mode=None, link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramRetryAfter as exc:
                await limiter.defer(chat_id, exc.retry_after)
                await self.usage.event(chat_id, "telegram_backoff")
                logger.warning("telegram_backoff", extra={"chat_id": chat_id})
                return
            except TelegramAPIError as exc:
                await self.usage.event(chat_id, "telegram_send_failed")
                # Delivery may have succeeded before a network failure; never resend blindly.
                logger.warning("telegram_send_failed", extra={"chat_id": chat_id,
                               "error_type": type(exc).__name__})
                return
            await limiter.finalize_reply(chat_id, reservation_id)
            await self.store.add(StoredMessage(
                chat_id=chat_id, message_id=sent.message_id, user_id=self.me.id,
                username=self.me.username, role="assistant", content=reply,
                created_at=sent.date.timestamp(), thread_id=item.stored.thread_id,
            ))
            self.followups.record_reply(chat_id, item.stored.thread_id, item.stored.user_id, sent.message_id)
            await self.usage.event(chat_id, "reply_sent")
            logger.info("reply_sent", extra={"chat_id": chat_id, "message_id": sent.message_id})

    async def set_paused(self, chat_id: int, paused: bool) -> None:
        async with self.locks.setdefault(chat_id, asyncio.Lock()):
            self._invalidate(chat_id)
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
