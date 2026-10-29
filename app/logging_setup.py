import json
import logging
import re
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        # Third-party exceptions may contain response bodies or request URLs.
        event = record.getMessage() if record.name.startswith("app") else "library_event"
        for secret in self.secrets:
            event = event.replace(secret, "[REDACTED]")
        event = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}", "[REDACTED]", event)
        output = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                  "logger": record.name, "event": event}
        for name in ("chat_id", "message_id", "error_type", "reason", "count"):
            if hasattr(record, name):
                output[name] = getattr(record, name)
        if record.exc_info:
            output["error_type"] = record.exc_info[0].__name__
        return json.dumps(output, ensure_ascii=False)


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secrets))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "openai", "aiohttp", "aiogram"):
        logging.getLogger(name).setLevel(logging.WARNING)
