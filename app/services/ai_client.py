"""Bounded, asynchronous access to the configured OpenAI-compatible gateway."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from app.storage.usage import BudgetExceeded

if TYPE_CHECKING:
    from app.config import Settings
    from app.storage.usage import UsageStore

logger = logging.getLogger(__name__)


class AIError(Exception):
    """Safe public error: never includes gateway bodies, prompts, or credentials."""


class AIBudgetExceeded(AIError):
    def __init__(self) -> None:
        super().__init__("daily_budget_exhausted")


class AIClient:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None,
                 usage: UsageStore | None = None) -> None:
        self.settings = settings
        self.usage = usage
        self._scope: ContextVar[tuple[int, int | None] | None] = ContextVar("ai_call_scope", default=None)
        self._semaphore = asyncio.Semaphore(settings.ai_max_concurrent_requests)
        # SDK auto retries would hide attempts from the budget. We own retries.
        self._client = client.with_options(max_retries=0) if client else AsyncOpenAI(
            api_key=settings.new_api_key.get_secret_value(),
            base_url=settings.new_api_base_url,
            timeout=settings.ai_timeout_seconds,
            max_retries=0,
        )

    @contextmanager
    def scope(self, chat_id: int, daily_call_limit: int | None = None):
        token = self._scope.set((chat_id, daily_call_limit))
        try:
            yield
        finally:
            self._scope.reset(token)

    def _tokens(self, response) -> tuple[int | None, int | None]:
        usage = getattr(response, "usage", None)
        values = [getattr(usage, name, None) for name in ("prompt_tokens", "completion_tokens")]
        return tuple(value if type(value) is int and value >= 0 else None for value in values)

    def _estimated_cost(self, response, decision: bool) -> float | None:
        prompt, completion = self._tokens(response)
        input_price = getattr(self.settings, "ai_input_price_per_million", 0)
        output_price = getattr(self.settings, "ai_output_price_per_million", 0)
        if decision:
            input_override = getattr(self.settings, "decision_input_price_per_million", None)
            output_override = getattr(self.settings, "decision_output_price_per_million", None)
            input_price = input_price if input_override is None else input_override
            output_price = output_price if output_override is None else output_override
        if prompt is None or completion is None or input_price <= 0 or output_price <= 0:
            return None
        return (prompt * input_price + completion * output_price) / 1_000_000

    async def _finish(self, reservation: int | None, started: float, *, response=None,
                      success: bool, error_type: str | None, decision: bool) -> None:
        if self.usage is None or reservation is None:
            return
        prompt, completion = self._tokens(response)
        # On cancellation, finish the metadata update without releasing the quota.
        await asyncio.shield(self.usage.finish(
            reservation, success=success, latency_seconds=time.perf_counter() - started,
            prompt_tokens=prompt, completion_tokens=completion, error_type=error_type,
            estimated_cost_usd=self._estimated_cost(response, decision),
        ))

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float | None:
        if isinstance(exc, APIStatusError):
            if exc.status_code not in {408, 409, 429} and exc.status_code < 500:
                return None
            # Respect the gateway's Retry-After within the overall call deadline.
            try:
                seconds = float(exc.response.headers.get("retry-after", ""))
                if math.isfinite(seconds) and seconds > 0:
                    return seconds
            except (TypeError, ValueError):
                pass
        elif not isinstance(exc, APIConnectionError):
            return None
        return min(0.5 * 2 ** attempt, 8) * random.uniform(0.75, 1)

    async def _request(self, kwargs: dict, *, decision: bool) -> str:
        scope = self._scope.get()
        if self.usage is not None and scope is None:
            raise AIError("missing_call_scope")
        for attempt in range(self.settings.ai_max_retries + 1):
            reservation = None
            if self.usage is not None:
                try:
                    reservation = await self.usage.reserve(
                        scope[0], "decision" if decision else "reply", kwargs["model"], chat_limit=scope[1],
                    )
                except BudgetExceeded:
                    raise AIBudgetExceeded() from None
            started = time.perf_counter()
            response = None
            try:
                response = await self._client.chat.completions.create(**kwargs)
                if not response.choices:
                    raise AIError("empty_response")
                content = response.choices[0].message.content
                if not isinstance(content, str) or not content.strip():
                    raise AIError("empty_response")
                await self._finish(reservation, started, response=response, success=True,
                                   error_type=None, decision=decision)
                return content.strip()
            except asyncio.CancelledError:
                await self._finish(reservation, started, response=response, success=False,
                                   error_type="CancelledError", decision=decision)
                raise
            except Exception as exc:
                await self._finish(reservation, started, response=response, success=False,
                                   error_type=type(exc).__name__, decision=decision)
                delay = self._retry_delay(exc, attempt)
                if delay is None or attempt >= self.settings.ai_max_retries:
                    raise
                await asyncio.sleep(delay)
        raise AIError("gateway_request_failed")  # Defensive; the loop always returns or raises.

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
            # Includes queue wait, metered retries and backoff, not just each HTTP request.
            async with asyncio.timeout(self.settings.ai_timeout_seconds):
                async with self._semaphore:
                    return await self._request(kwargs, decision=decision)
        except AIError:
            raise
        except Exception as exc:
            # Error bodies often echo user messages or contain credential-bearing URLs.
            logger.warning("ai_request_failed", extra={"error_type": type(exc).__name__})
            raise AIError("gateway_request_failed") from None

    async def close(self) -> None:
        await self._client.close()
