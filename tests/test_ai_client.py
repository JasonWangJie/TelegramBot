import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.services.ai_client import AIClient, AIError


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


def response(content="你好"):
    return httpx.Response(200, json={
        "id": "test",
        "created": 1,
        "model": "gateway-chat-model",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    })


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

    # The SDK's first retry backoff is at least 0.375s; leave room for a slow
    # local test host to complete the first mocked request before our deadline.
    client = AIClient(settings(ai_timeout_seconds=0.2), sdk_client(handler, max_retries=5))
    try:
        with pytest.raises(AIError):
            await asyncio.wait_for(client.chat([{"role": "user", "content": "hi"}]), timeout=1)
        assert attempts == 1
    finally:
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
