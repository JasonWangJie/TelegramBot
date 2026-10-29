"""Bounded, asynchronous access to the configured OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)


class AIError(Exception):
    """Safe public error: never includes gateway bodies, prompts, or credentials."""


class AIClient:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.ai_max_concurrent_requests)
        self._client = client or AsyncOpenAI(
            api_key=settings.new_api_key.get_secret_value(),
            base_url=settings.new_api_base_url,
            timeout=settings.ai_timeout_seconds,
            max_retries=settings.ai_max_retries,
        )

    async def chat(self, messages: list[dict], *, decision: bool = False) -> str:
        model = self.settings.new_api_model
        if decision:
            model = self.settings.decision_model or model
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            self.settings.ai_token_parameter: (
                200 if decision else self.settings.ai_max_output_tokens
            ),
        }
        if decision and self.settings.decision_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            # Includes queue wait, SDK retries and backoff, not just each HTTP request.
            async with asyncio.timeout(self.settings.ai_timeout_seconds):
                async with self._semaphore:
                    response = await self._client.chat.completions.create(**kwargs)
            if not response.choices:
                raise AIError("empty_response")
            content = response.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise AIError("empty_response")
            return content.strip()
        except AIError:
            raise
        except Exception as exc:
            # Error bodies often echo user messages or contain credential-bearing URLs.
            logger.warning("ai_request_failed", extra={"error_type": type(exc).__name__})
            raise AIError("gateway_request_failed") from None

    async def close(self) -> None:
        await self._client.close()
