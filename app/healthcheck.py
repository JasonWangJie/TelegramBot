import json
import logging
import os
import sys
import time
from pathlib import Path

from aiogram.methods import GetUpdates

logger = logging.getLogger(__name__)


class PollingHeartbeat:
    """Only successful getUpdates responses refresh health, even for quiet groups."""

    def __init__(self, path: Path):
        self.path = path

    async def __call__(self, make_request, bot, method):
        try:
            result = await make_request(bot, method)
        except Exception as exc:
            if isinstance(method, GetUpdates):
                logger.warning("polling_request_failed", extra={"error_type": type(exc).__name__})
            raise
        if isinstance(method, GetUpdates):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"timestamp": time.time(), "pid": os.getpid()}), encoding="utf-8")
            temporary.replace(self.path)
        return result


def healthy(path: Path, max_age: float) -> bool:
    try:
        age = time.time() - float(json.loads(path.read_text(encoding="utf-8"))["timestamp"])
        return 0 <= age <= max_age
    except (OSError, ValueError, TypeError, KeyError):
        return False


def main() -> None:
    path = Path(os.environ.get("HEARTBEAT_PATH", "data/bot-heartbeat"))
    try:
        max_age = float(os.environ.get("HEALTHCHECK_MAX_AGE_SECONDS", "180"))
    except ValueError:
        sys.exit(1)
    sys.exit(0 if healthy(path, max_age) else 1)


if __name__ == "__main__":
    main()
