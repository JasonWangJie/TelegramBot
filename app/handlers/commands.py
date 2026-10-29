import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from pydantic import ValidationError

from app.config import Settings
from app.handlers.messages import MessageController
from app.storage.group_settings import GroupSettings

HELP = ("我是 AI 群聊机器人。普通群聊按场景接话，@我或回复我的消息会优先处理，仍受限频和预算约束。\n"
        "经配置的管理员可用：/chatid /status /pause /resume /context /settings /mode /stats /help\n"
        "群聊消息会保存到本地并发送至所配置的 AI 网关用于决策和回复；请先告知群友。")

MODE_NAMES = {"quiet": "安静", "normal": "普通", "active": "活跃"}
EVENT_NAMES = {
    "allowed": "可处理",
    "paused": "当前群已暂停",
    "conversation_stopped": "用户已结束对话",
    "quiet_hours": "处于安静时段",
    "quiet_mode": "安静模式忽略主动接话",
    "local_filter": "未达到本地接话条件",
    "repeated_content": "重复内容已跳过",
    "processing_error": "消息处理异常",
    "probability_skip": "概率采样跳过",
    "decision_skip": "模型判断无需回应",
    "model_skip": "模型判断无需回应",
    "decision_unavailable": "决策模型调用失败",
    "invalid_decision": "决策格式无效",
    "budget_exhausted": "当日预算已用完",
    "reply_unavailable": "回复模型调用失败",
    "empty_reply": "模型返回空回复",
    "followup": "识别为连续追问",
    "telegram_backoff": "Telegram 要求等待",
    "telegram_send_failed": "Telegram 发送失败",
    "reply_sent": "已发送回复",
    "reply_interval": "尚未达到回复间隔",
    "hourly_quota": "小时发送额度已用完",
    "attempt_cooldown": "仍在处理冷却期",
}
INTEGER_FIELDS = {"max_replies_per_hour", "daily_call_limit"}
NUMBER_FIELDS = {"reply_probability", "min_reply_interval_seconds", "chat_cooldown_seconds"}
SETTINGS_HELP = (
    "修改：/settings 参数 值；恢复某项：/settings 参数 default；全部恢复：/settings reset\n"
    "可修改：mode、persona、reply_probability、min_reply_interval_seconds、"
    "chat_cooldown_seconds、max_replies_per_hour、quiet_hours、daily_call_limit\n"
    "例：/settings quiet_hours 23:00-08:00（off 关闭）\n"
    "每日预算：/settings daily_call_limit 200（0 禁止调用；default 使用部署默认值）"
)


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=name, callback_data=f"settings:mode:{mode}")
        for mode, name in MODE_NAMES.items()
    ]])


def parse_change(arguments: str) -> dict:
    parts = arguments.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0] not in GroupSettings.model_fields:
        raise ValueError("unknown setting or missing value")
    name, value = parts
    if value.casefold() in {"default", "off"}:
        return {name: "normal" if name == "mode" else None}
    if name in INTEGER_FIELDS:
        return {name: int(value)}
    if name in NUMBER_FIELDS:
        return {name: float(value)}
    return {name: value}


def format_stats(snapshot: dict, *, global_view: bool) -> str:
    scope = "全局" if global_view else "当前群"
    events = snapshot.get("events", {})
    top_events = sorted(events.items(), key=lambda pair: (-pair[1], pair[0]))[:8]
    reasons = "、".join(f"{EVENT_NAMES.get(reason, reason)}={count}"
                       for reason, count in top_events) or "暂无"
    cost = snapshot.get("estimated_cost_usd")
    if cost is None:
        cost_text = "未提供（缺少单价或用量）"
    elif snapshot.get("cost_estimate_complete", False):
        cost_text = f"${cost:.6f}（按配置单价估算）"
    else:
        cost_text = f"${cost:.6f}（仅部分调用可估算，非完整费用；已计价 {snapshot.get('priced_calls', 0)} 次）"
    error_type = snapshot.get("last_error_type") or "无"
    last_event = snapshot.get("last_event") or "暂无"
    completed_calls = snapshot["successes"] + snapshot["failures"]
    failure_rate = f"{snapshot['failures'] / completed_calls:.1%}" if completed_calls else "暂无已完成调用"
    return (
        f"{scope}今日统计：{snapshot['day']}（{snapshot['timezone']}）\n"
        f"调用：{snapshot['calls']}；决策 {snapshot['decision_calls']}；回复 {snapshot['reply_calls']}\n"
        f"成功：{snapshot['successes']}；失败：{snapshot['failures']}；未完成：{snapshot['pending_calls']}\n"
        f"已完成调用失败率：{failure_rate}\n"
        f"每日调用剩余：{snapshot['remaining_calls']}\n"
        f"已知 Token：输入 {snapshot['prompt_tokens']} / 输出 {snapshot['completion_tokens']}\n"
        f"未返回用量的调用：{snapshot['missing_usage_calls']}\n"
        f"平均请求耗时：{snapshot['average_latency_seconds']:.2f} 秒\n"
        f"费用估算：{cost_text}\n"
        f"最近处理结果：{EVENT_NAMES.get(last_event, last_event)}\n"
        f"最近失败类型：{error_type}\n"
        f"处理结果计数：{reasons}"
    )


def create_command_router(settings: Settings, controller: MessageController) -> Router:
    router = Router(name="commands")
    last_command: dict[tuple[int, int], float] = {}

    def permitted(user, chat, sender_chat=None, *, require_allowed=True) -> bool:
        return bool(user and not user.is_bot and not sender_chat
                    and user.id in settings.admin_ids
                    and chat.type in {"group", "supergroup"}
                    and (not require_allowed or chat.id in settings.allowed_chat_ids))

    def throttle(chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        now = time.monotonic()
        if now - last_command.get(key, float("-inf")) < 2:
            return False
        for old in [old for old, timestamp in last_command.items() if now - timestamp > 60]:
            last_command.pop(old, None)
        last_command[key] = now
        return True

    def authorized(message: Message, require_allowed: bool = True) -> bool:
        return (permitted(message.from_user, message.chat, message.sender_chat,
                          require_allowed=require_allowed)
                and throttle(message.chat.id, message.from_user.id))

    async def settings_text(chat_id: int) -> str:
        policy = await controller.get_policy(chat_id)
        current = await controller.effective_settings(chat_id)
        persona = current.bot_persona[:300]
        if len(current.bot_persona) > 300:
            persona += "…"
        overridden = ", ".join(name for name, value in policy.model_dump().items()
                               if name != "mode" and value is not None) or "无"
        return (
            f"当前群设置\n模式：{MODE_NAMES[policy.mode]}（{policy.mode}）\n"
            f"性格：{persona}\n普通候选采样率：{current.reply_probability:g}\n"
            f"回复间隔：{current.min_reply_interval_seconds:g} 秒；"
            f"处理冷却：{current.chat_cooldown_seconds:g} 秒\n"
            f"小时发送上限：{current.max_replies_per_hour}；每日调用上限：{current.chat_daily_call_limit}\n"
            f"安静时段：{policy.quiet_hours or '关闭'}（{settings.bot_timezone}）\n"
            f"单独覆盖项：{overridden}\n"
            "安静模式只响应明确 @或回复；安静时段暂停所有 AI 回复。模式切换保留单独覆盖项。\n\n"
            f"{SETTINGS_HELP}"
        )

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
            current = await controller.effective_settings(message.chat.id)
            policy = await controller.get_policy(message.chat.id)
            limit = await controller.limiter_for(current).check(message.chat.id)
            await message.answer(
                f"状态：{'已暂停' if paused else '运行中'}\n模型：{current.new_api_model}\n"
                f"模式：{MODE_NAMES[policy.mode]}\n小时剩余额度：{limit.remaining}\n"
                f"限频：{EVENT_NAMES.get(limit.reason, limit.reason)}\n"
                f"需等待：{limit.retry_after:.0f} 秒\nAI 决策：{current.decision_enabled}"
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

    @router.message(Command("settings"))
    async def group_settings(message: Message, command: CommandObject):
        if not authorized(message):
            return
        arguments = (command.args or "").strip()
        try:
            if arguments == "reset":
                await controller.reset_policy(message.chat.id)
            elif arguments:
                await controller.update_policy(message.chat.id, parse_change(arguments))
        except (ValidationError, ValueError):
            await message.answer("设置无效，请检查参数名称、格式和取值范围。\n" + SETTINGS_HELP)
            return
        await message.answer(await settings_text(message.chat.id), reply_markup=mode_keyboard())

    @router.message(Command("mode"))
    async def group_mode(message: Message, command: CommandObject):
        if not authorized(message):
            return
        mode = (command.args or "").strip()
        if mode not in MODE_NAMES:
            await message.answer("用法：/mode quiet、/mode normal 或 /mode active")
            return
        await controller.update_policy(message.chat.id, {"mode": mode})
        await message.answer(await settings_text(message.chat.id), reply_markup=mode_keyboard())

    @router.callback_query(F.data.startswith("settings:"))
    async def change_mode(callback: CallbackQuery):
        message = callback.message
        if (not isinstance(message, Message) or not message.from_user
                or message.from_user.id != callback.bot.id
                or not permitted(callback.from_user, message.chat)):
            await callback.answer("仅允许群内配置的管理员操作。", show_alert=True)
            return
        mode = (callback.data or "").removeprefix("settings:mode:")
        if not (callback.data or "").startswith("settings:mode:") or mode not in MODE_NAMES:
            await callback.answer("设置按钮无效。", show_alert=True)
            return
        if not throttle(message.chat.id, callback.from_user.id):
            await callback.answer("操作过快，请稍后重试。")
            return
        if (await controller.get_policy(message.chat.id)).mode == mode:
            await callback.answer(f"当前已是{MODE_NAMES[mode]}模式。")
            return
        await callback.answer()
        await controller.update_policy(message.chat.id, {"mode": mode})
        await message.edit_text(await settings_text(message.chat.id), reply_markup=mode_keyboard())

    @router.message(Command("stats"))
    async def stats(message: Message, command: CommandObject):
        if not authorized(message):
            return
        argument = (command.args or "").strip()
        if argument not in {"", "global"}:
            await message.answer("用法：/stats；所有者可使用 /stats global")
            return
        global_view = argument == "global"
        owners = {int(value.strip()) for value in settings.telegram_owner_ids.split(",") if value.strip()}
        if global_view and message.from_user.id not in owners:
            await message.answer("仅配置的机器人所有者可查看全局统计。")
            return
        current = await controller.effective_settings(message.chat.id)
        snapshot = await controller.usage.snapshot(
            None if global_view else message.chat.id, chat_limit=current.chat_daily_call_limit
        )
        await message.answer(format_stats(snapshot, global_view=global_view))

    return router
