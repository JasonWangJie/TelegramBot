import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from openai import AsyncOpenAI, RateLimitError
from pydantic import SecretStr

from app.services.ai_client import AIBudgetExceeded, AIClient, AIError
from app.storage.database import Database
from app.storage.usage import UsageStore


def test_long_retry_after_is_honored_instead_of_replaced_with_short_backoff():
    request = httpx.Request("POST", "https://gateway.invalid/v1/chat/completions")
    upstream = httpx.Response(429, request=request, headers={"Retry-After": "120"})
    error = RateLimitError("rate limited", response=upstream, body=None)
    assert AIClient._retry_delay(error, 0) == 120


def settings(**overrides):
    values = dict(
        new_api_key=SecretStr("test-not-a-real-key"),
        new_api_base_url="https://gateway.invalid/v1",
        new_api_model="gateway-chat-model",
        decision_model="gateway-small-model",
        ai_timeout_seconds=10.0,
        ai_max_retries=0,
        ai_max_output_tokens=300,
        ai_max_concurrent_requests=1,
        ai_token_parameter="max_tokens",
        decision_json_mode=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def response(content="你好", usage=None):
    payload = {
        "id": "test",
        "created": 1,
        "model": "gateway-chat-model",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }
    if usage is not None:
        payload["usage"] = usage
    return httpx.Response(200, json=payload)


def sdk_client(handler, max_retries=0):
    return AsyncOpenAI(
        api_key="test-not-a-real-key",
        base_url="https://gateway.invalid/v1",
        max_retries=max_retries,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_gateway_route_model_and_token_parameter():
    requests = []

    def handler(request):
        requests.append(request)
        return response()

    client = AIClient(settings(), sdk_client(handler))
    try:
        assert await client.chat([{"role": "user", "content": "你好"}]) == "你好"
        assert requests[0].url == "https://gateway.invalid/v1/chat/completions"
        data = json.loads(requests[0].content)
        assert data["model"] == "gateway-chat-model"
        assert data["max_tokens"] == 300
        assert "response_format" not in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_decision_model_json_mode_and_alternate_tokens():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response('{"action":"skip","reason":"无需回应","confidence":0.9}')

    client = AIClient(settings(decision_json_mode=True, ai_token_parameter="max_completion_tokens"), sdk_client(handler))
    try:
        await client.chat([{"role": "user", "content": "test"}], decision=True)
        assert requests[0]["model"] == "gateway-small-model"
        assert requests[0]["max_completion_tokens"] == 200
        assert "max_tokens" not in requests[0]
        assert requests[0]["response_format"] == {"type": "json_object"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_does_not_leak_upstream_body(caplog):
    secret = "private-prompt-or-api-key"
    client = AIClient(settings(), sdk_client(lambda request: httpx.Response(400, json={"error": {"message": secret}})))
    try:
        with pytest.raises(AIError) as caught:
            await client.chat([{"role": "user", "content": secret}])
        assert secret not in str(caught.value)
        assert secret not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_total_timeout_limits_retry_backoff():
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, json={"error": {"message": "temporary"}})

    # The manual first retry backoff is at least 0.375s; leave room for a slow
    # local test host to complete the first mocked request before our deadline.
    client = AIClient(settings(ai_timeout_seconds=0.2, ai_max_retries=5), sdk_client(handler, max_retries=5))
    try:
        with pytest.raises(AIError):
            await asyncio.wait_for(client.chat([{"role": "user", "content": "hi"}]), timeout=1)
        assert attempts == 1
    finally:
        await client.close()


@pytest_asyncio.fixture
async def usage(tmp_path):
    db = Database(str(tmp_path / "ai-usage.db"))
    await db.open()
    store = UsageStore(db)
    yield store
    await db.close()


@pytest.mark.asyncio
async def test_each_retry_reserves_budget_and_sdk_retries_are_disabled(usage, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return response(usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})

    monkeypatch.setattr(AIClient, "_retry_delay", staticmethod(lambda exc, attempt: 0))
    client = AIClient(settings(ai_max_retries=1), sdk_client(handler, max_retries=5), usage)
    try:
        with client.scope(-100, 2):
            assert await client.chat([{"role": "user", "content": "question"}]) == "你好"
        snapshot = await usage.snapshot(-100)
        assert len(requests) == snapshot["calls"] == 2
        assert snapshot["successes"] == snapshot["failures"] == 1
        assert snapshot["pending_calls"] == 0
        assert snapshot["prompt_tokens"] == 12
        assert snapshot["completion_tokens"] == 5
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_budget_stops_retry_before_second_http_attempt(usage, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "temporary"}})

    monkeypatch.setattr(AIClient, "_retry_delay", staticmethod(lambda exc, attempt: 0))
    client = AIClient(settings(ai_max_retries=3), sdk_client(handler, max_retries=5), usage)
    try:
        with client.scope(-100, 1):
            with pytest.raises(AIBudgetExceeded, match="daily_budget_exhausted"):
                await client.chat([{"role": "user", "content": "question"}], decision=True)
        snapshot = await usage.snapshot(-100)
        assert len(requests) == snapshot["calls"] == snapshot["decision_calls"] == 1
        assert snapshot["failures"] == 1
        assert snapshot["remaining_calls"] == 199  # Store default, unless caller supplies the override.
        assert (await usage.snapshot(-100, chat_limit=1))["remaining_calls"] == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_400_is_not_retried_or_leaked(usage):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": "secret prompt in upstream body"}})

    client = AIClient(settings(ai_max_retries=5), sdk_client(handler, max_retries=5), usage)
    try:
        with client.scope(-100):
            with pytest.raises(AIError) as error:
                await client.chat([{"role": "user", "content": "question"}])
        assert len(requests) == 1
        assert "secret prompt" not in str(error.value)
        snapshot = await usage.snapshot(-100)
        assert snapshot["last_error_type"] == "BadRequestError"
        async with usage.db.connection.execute("SELECT model, error_type FROM ai_call_usage") as cursor:
            row = await cursor.fetchone()
            assert "secret prompt" not in str(tuple(row))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_scope_required_and_context_isolated_between_tasks(usage):
    requests = []

    def handler(request):
        requests.append(request)
        return response()

    client = AIClient(settings(ai_max_concurrent_requests=2), sdk_client(handler), usage)

    async def call(chat_id):
        with client.scope(chat_id, 1):
            await asyncio.sleep(0)
            return await client.chat([{"role": "user", "content": "question"}])

    try:
        with pytest.raises(AIError, match="missing_call_scope"):
            await client.chat([{"role": "user", "content": "question"}])
        assert not requests
        assert await asyncio.gather(call(-100), call(-200)) == ["你好", "你好"]
        assert (await usage.snapshot(-100))["calls"] == 1
        assert (await usage.snapshot(-200))["calls"] == 1
        with pytest.raises(AIError, match="missing_call_scope"):
            await client.chat([{"role": "user", "content": "question"}])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_usage_is_unknown_and_prices_are_only_estimates(usage):
    client = AIClient(settings(ai_input_price_per_million=1, ai_output_price_per_million=2),
                      sdk_client(lambda request: response()), usage)
    try:
        with client.scope(-100):
            await client.chat([{"role": "user", "content": "question"}])
        snapshot = await usage.snapshot(-100)
        assert snapshot["missing_usage_calls"] == 1
        assert snapshot["estimated_cost_usd"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_token_prices_and_decision_override_are_per_attempt(usage):
    tokens = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    client = AIClient(settings(ai_input_price_per_million=1, ai_output_price_per_million=2,
                               decision_input_price_per_million=0.1, decision_output_price_per_million=0.2),
                      sdk_client(lambda request: response(usage=tokens)), usage)
    try:
        with client.scope(-100):
            await client.chat([{"role": "user", "content": "question"}])
            await client.chat([{"role": "user", "content": "question"}], decision=True)
        snapshot = await usage.snapshot(-100)
        assert snapshot["estimated_cost_usd"] == pytest.approx(0.00022)
        assert snapshot["cost_estimate_complete"]
        assert snapshot["priced_calls"] == 2
        assert snapshot["prompt_tokens"] == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancellation_charges_and_finalizes_attempt_without_releasing_budget(usage):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    client = AIClient(settings(), sdk_client(handler), usage)

    async def run():
        with client.scope(-100, 1):
            await client.chat([{"role": "user", "content": "question"}])

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        snapshot = await usage.snapshot(-100, chat_limit=1)
        assert snapshot["calls"] == snapshot["failures"] == 1
        assert snapshot["remaining_calls"] == snapshot["pending_calls"] == 0
        assert snapshot["last_error_type"] == "CancelledError"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_concurrency_limit_and_cancellation_release_capacity():
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    highest = 0

    async def handler(request):
        nonlocal active, highest
        active += 1
        highest = max(highest, active)
        entered.set()
        try:
            await release.wait()
            return response()
        finally:
            active -= 1

    client = AIClient(settings(ai_timeout_seconds=2), sdk_client(handler))
    first = asyncio.create_task(client.chat([{"role": "user", "content": "one"}]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        results = await asyncio.gather(
            client.chat([{"role": "user", "content": "two"}]),
            client.chat([{"role": "user", "content": "three"}]),
        )
        assert results == ["你好", "你好"]
        assert highest == 1
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_model_output_is_an_error(content):
    client = AIClient(settings(), sdk_client(lambda request: response(content)))
    try:
        with pytest.raises(AIError, match="empty_response"):
            await client.chat([{"role": "user", "content": "hi"}])
    finally:
        await client.close()
