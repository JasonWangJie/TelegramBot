"""Safe offline configuration check, with an explicit optional gateway probe."""
import argparse
import asyncio

from pydantic import ValidationError

from app.config import Settings
from app.logging_setup import setup_logging
from app.services.ai_client import AIClient


async def probe(settings: Settings) -> bool:
    ai = AIClient(settings)
    try:
        response = await ai.chat([{"role": "user", "content": "Reply with OK."}])
        if not response:
            return False
        # Check configured decision transport/model too. No real chat data is sent.
        if settings.decision_enabled:
            # Strict response validation is covered by tests; this probe only checks transport.
            decision = await ai.chat([{"role": "user", "content":
                'Return JSON: {"action":"skip","reason":"probe","confidence":1}'}], decision=True)
            if not decision:
                return False
        return True
    finally:
        await ai.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="校验 .env；--probe 会调用网关，可能产生少量费用")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = [".".join(map(str, item["loc"])) or "settings"
                  for item in exc.errors(include_input=False, include_context=False)]
        print("配置错误字段：" + ", ".join(fields))
        raise SystemExit(2) from None
    setup_logging(settings.log_level, [settings.telegram_bot_token.get_secret_value(),
                                      settings.new_api_key.get_secret_value()])
    print(f"配置有效；允许 {len(settings.allowed_chat_ids)} 个群；管理用户 {len(settings.admin_ids)} 个。")
    if not settings.allowed_chat_ids:
        print("群白名单为空：所有群 AI 处理均关闭。")
    if not settings.admin_ids:
        print("管理用户列表为空：群管理命令不可用。")
    if args.probe:
        try:
            success = asyncio.run(probe(settings))
        except Exception as exc:
            print(f"网关验证失败：{type(exc).__name__}（不输出响应正文或凭据）")
            raise SystemExit(1) from None
        if not success:
            print("网关返回空内容，请检查模型兼容性。")
            raise SystemExit(1)
        print("网关回复与决策调用成功。" if settings.decision_enabled else "网关回复调用成功。")


if __name__ == "__main__":
    main()
