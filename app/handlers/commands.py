import time

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import Settings
from app.handlers.messages import MessageController

HELP = ("我是 AI 群聊机器人。普通群聊按场景接话，@我或回复我的消息会优先处理，仍受限频约束。\n"
        "经配置的管理员可用：/chatid /status /pause /resume /context /help\n"
        "群聊消息会保存到本地并发送至所配置的 AI 网关用于决策和回复；请先告知群友。")


def create_command_router(settings: Settings, controller: MessageController) -> Router:
    router = Router(name="commands")
    last_command: dict[tuple[int, int], float] = {}

    def authorized(message: Message, require_allowed: bool = True) -> bool:
        if (not message.from_user or message.from_user.is_bot or message.sender_chat
                or message.from_user.id not in settings.admin_ids
                or message.chat.type not in {"group", "supergroup"}):
            return False
        if require_allowed and message.chat.id not in settings.allowed_chat_ids:
            return False
        key = (message.chat.id, message.from_user.id)
        now = time.monotonic()
        if now - last_command.get(key, float("-inf")) < 2:
            return False
        # Bound transient command throttle state, including groups outside the allowlist.
        for old in [old for old, timestamp in last_command.items() if now - timestamp > 60]:
            last_command.pop(old, None)
        last_command[key] = now
        return True

    @router.message(CommandStart())
    async def start(message: Message):
        if message.chat.type == "private" and message.from_user:
            await message.answer(f"{HELP}\n你的用户 ID：{message.from_user.id}\n私聊仅提供使用说明，不调用 AI。")

    @router.message(Command("help", "chatid"))
    async def help_or_chatid(message: Message):
        if authorized(message, require_allowed=False):
            await message.answer(f"{HELP}\n当前群 ID：{message.chat.id}")

    @router.message(Command("status"))
    async def status(message: Message):
        if authorized(message):
            paused = await controller.store.is_paused(message.chat.id)
            limit = await controller.limiter.check(message.chat.id)
            await message.answer(
                f"状态：{'已暂停' if paused else '运行中'}\n模型：{settings.new_api_model}\n"
                f"小时剩余额度：{limit.remaining}\n限频：{limit.reason}\n"
                f"需等待：{limit.retry_after:.0f} 秒\nAI 决策：{settings.decision_enabled}"
            )

    @router.message(Command("pause", "resume"))
    async def pause_or_resume(message: Message):
        if authorized(message):
            paused = (message.text or "").split()[0].split("@")[0] == "/pause"
            await controller.set_paused(message.chat.id, paused)
            await message.answer("已暂停 AI 回复和新消息记录。" if paused else "已恢复 AI 回复和消息记录。")

    @router.message(Command("context"))
    async def context(message: Message):
        if authorized(message):
            count = await controller.store.count(message.chat.id)
            await message.answer(f"当前群保存 {count} 条上下文，最多 {settings.context_max_messages} 条。")

    return router
