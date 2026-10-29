"""Construct bounded, role-safe transcripts and generate one concise reply."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from app.services.ai_client import AIClient

if TYPE_CHECKING:
    from app.config import Settings
    from app.storage.messages import StoredMessage

PROMPT_DIRECTORY = Path(__file__).resolve().parent.parent / "prompts"


def _encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_transcript(
    settings: Settings, context: list[StoredMessage], trigger: StoredMessage
) -> str:
    """No Telegram text is promoted to an API system/developer/assistant message.

    Enforce isolation here as well as in storage, and always retain the trigger.
    Limits apply to serialized characters, including JSON escaping and metadata.
    """
    records: dict[tuple[int, str], StoredMessage] = {}
    for message in context:
        if message.chat_id == trigger.chat_id and message.thread_id == trigger.thread_id:
            records[(message.message_id, message.role)] = message
    records[(trigger.message_id, trigger.role)] = trigger
    ordered = sorted(records.values(), key=lambda item: (item.created_at, item.message_id))
    selected = ordered[-settings.context_max_messages :]
    if trigger not in selected:
        recent_count = settings.context_max_messages - 1
        selected = [trigger] + (selected[-recent_count:] if recent_count else [])
        selected.sort(key=lambda item: (item.created_at, item.message_id))

    def as_record(message: StoredMessage) -> dict:
        return {
            "message_id": message.message_id,
            "speaker_id": message.user_id,
            "speaker_name": (message.username or "")[:80],
            "role": message.role,
            "text": message.content[: settings.message_max_chars],
        }

    items = [as_record(message) for message in selected]
    payload = {"trigger_message_id": trigger.message_id, "messages": items}
    # Prefer dropping old complete records to silently cutting many sentences.
    while len(_encode(payload)) > settings.context_max_chars and len(items) > 1:
        index = next(
            index
            for index, item in enumerate(items)
            if (item["message_id"], item["role"]) != (trigger.message_id, trigger.role)
        )
        items.pop(index)
    if len(_encode(payload)) > settings.context_max_chars:
        # Unusual display names can expand to many escaped JSON characters.
        # Text and stable speaker IDs matter more than preserving that name.
        items[0]["speaker_name"] = ""
        original = items[0]["text"]
        low, high = 0, len(original)
        while low < high:
            midpoint = (low + high + 1) // 2
            items[0]["text"] = original[:midpoint]
            if len(_encode(payload)) <= settings.context_max_chars:
                low = midpoint
            else:
                high = midpoint - 1
        items[0]["text"] = original[:low]
    encoded = _encode(payload)
    if len(encoded) > settings.context_max_chars:
        raise ValueError("context_max_chars is too small for transcript metadata")
    return encoded


def telegram_text(text: str, max_units: int = 4000) -> str:
    """Bound text in UTF-16 units, without splitting surrogate pairs or messages."""
    return text.encode("utf-16-le", errors="replace")[: max_units * 2].decode(
        "utf-16-le", errors="ignore"
    ).strip()


class ConversationService:
    def __init__(self, settings: Settings, ai_client: AIClient) -> None:
        self.settings = settings
        self.ai_client = ai_client
        self._prompt = (PROMPT_DIRECTORY / "persona.txt").read_text(encoding="utf-8")

    async def generate(self, context: list[StoredMessage], trigger: StoredMessage) -> str:
        messages = [
            {"role": "system", "content": self._prompt + "\n管理员设定的聊天风格：" + self.settings.bot_persona},
            {"role": "user", "content": build_transcript(self.settings, context, trigger)},
        ]
        return telegram_text(await self.ai_client.chat(messages))
