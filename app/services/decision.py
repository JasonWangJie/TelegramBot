"""Local candidate filtering followed by a fail-closed model decision."""

from __future__ import annotations

import json
import logging
import random
import re
from typing import TYPE_CHECKING, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.services.ai_client import AIClient, AIError
from app.services.conversation import PROMPT_DIRECTORY, build_transcript

if TYPE_CHECKING:
    from app.config import Settings
    from app.storage.messages import StoredMessage

logger = logging.getLogger(__name__)
QUESTION_SIGNALS = re.compile(
    r"[?？]|怎么|如何|为什么|请问|求助|有没有|大家觉得|你觉得|建议|推荐|聊聊|怎么办|"
    r"有谁|有人知道|谁知道|吗|嘛|\b(?:why|how|what|where|when|who|help|advice|thoughts)\b",
    re.IGNORECASE,
)
ADVERTISEMENT = re.compile(r"扫码|返利|代刷|刷单|加群.{0,12}(?:赚|福利|推广)|推广.{0,12}链接")


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["reply", "skip"]
    reason: str = Field(min_length=1, max_length=160)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    output: dict = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


def parse_decision(content: str) -> Decision:
    if len(content) > 2048:
        raise ValueError("decision is too long")
    data = json.loads(content, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    return Decision.model_validate(data)


class DecisionService:
    def __init__(
        self, settings: Settings, ai_client: AIClient, rng: Callable[[], float] | None = None
    ) -> None:
        self.settings = settings
        self.ai_client = ai_client
        self._random = rng or random.random
        self._prompt = (PROMPT_DIRECTORY / "decision.txt").read_text(encoding="utf-8")

    def is_candidate(self, text: str, direct: bool = False) -> bool:
        text = text.strip()
        if not text or text.startswith("/"):
            return False
        if direct:
            return True
        if not any(character.isalnum() for character in text):
            return False
        if ADVERTISEMENT.search(text):
            return False
        return bool(QUESTION_SIGNALS.search(text))

    async def should_reply(
        self, context: list[StoredMessage], trigger: StoredMessage, direct: bool = False
    ) -> bool:
        if not self.is_candidate(trigger.content, direct=direct):
            return False
        if direct and self.settings.direct_mention_always_reply:
            return True
        normalized = " ".join(trigger.content.split()).casefold()
        if not direct and any(
            message.chat_id == trigger.chat_id
            and message.thread_id == trigger.thread_id
            and message.role == "user"
            and message.message_id != trigger.message_id
            and " ".join(message.content.split()).casefold() == normalized
            for message in context
        ):
            return False
        if self._random() >= self.settings.reply_probability:
            return False
        if not self.settings.decision_enabled:
            return True
        try:
            content = await self.ai_client.chat(
                [
                    {"role": "system", "content": self._prompt},
                    {"role": "user", "content": build_transcript(self.settings, context, trigger)},
                ],
                decision=True,
            )
            decision = parse_decision(content)
        except (AIError, ValueError, ValidationError):
            logger.info("decision_skipped_invalid_or_unavailable")
            return False
        return decision.action == "reply" and decision.confidence >= self.settings.decision_confidence_threshold
