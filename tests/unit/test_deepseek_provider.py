from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.llm import (
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMPaymentRequiredError,
    LLMRateLimitError,
    LLMRequest,
    LLMRequestError,
    LLMServerError,
    LLMTimeoutError,
)
from app.llm.providers.deepseek import DeepSeekProvider


API_KEY = "unit-test-key-never-log"


def llm_request() -> LLMRequest:
    return LLMRequest(
        system_prompt="Return JSON. private-system-prompt",
        user_prompt="private-conversation-text",
        max_output_tokens=321,
        json_output=True,
    )


def valid_payload(*, content: str | None = '{"ok": true}') -> dict[str, Any]:
    return {
        "model": "deepseek-v4-flash-actual",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": content,
                    "reasoning_content": "must-not-be-retained",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "prompt_cache_hit_tokens": 3,
        },
    }


def mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_request_shape_url_auth_and_valid_response_usage() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=valid_payload())

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            base_url="https://api.deepseek.com/",
            model="deepseek-v4-flash",
            timeout_seconds=12,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(llm_request())
        await provider.aclose()

    assert len(captured) == 1
    request = captured[0]
    assert request.url == httpx.URL("https://api.deepseek.com/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Content-Type"].startswith("application/json")
    body = json.loads(request.content)
    assert body == {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Return JSON. private-system-prompt"},
            {"role": "user", "content": "private-conversation-text"},
        ],
        "stream": False,
        "thinking": {"type": "disabled"},
        "max_tokens": 321,
        "response_format": {"type": "json_object"},
    }
    assert "user_id" not in body
    assert response.content == '{"ok": true}'
    assert response.provider == "deepseek"
    assert response.model == "deepseek-v4-flash-actual"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 12
    assert response.usage.completion_tokens == 8
    assert response.usage.total_tokens == 20
    assert "reasoning_content" not in response.__dict__


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, LLMRequestError),
        (401, LLMAuthenticationError),
        (402, LLMPaymentRequiredError),
        (422, LLMRequestError),
    ],
)
async def test_permanent_http_errors_are_not_retried(
    status: int,
    error_type: type[Exception],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "private-body"}})

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=2,
            client=client,
            sleep=_no_sleep,
        )
        with pytest.raises(error_type) as caught:
            await provider.complete(llm_request())

    assert calls == 1
    assert "private-body" not in str(caught.value)
    assert API_KEY not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retryable_http_error_retries_then_succeeds(status: int) -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status, json={"error": "temporary"})
        return httpx.Response(200, json=valid_payload())

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=2,
            client=client,
            sleep=sleep,
        )
        response = await provider.complete(llm_request())

    assert response.provider == "deepseek"
    assert calls == 2
    assert delays == [0.5]


@pytest.mark.asyncio
async def test_429_exhaustion_is_rate_limit_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": "temporary"})

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=1,
            client=client,
            sleep=_no_sleep,
        )
        with pytest.raises(LLMRateLimitError):
            await provider.complete(llm_request())

    assert calls == 2


@pytest.mark.asyncio
async def test_503_exhaustion_is_bounded_server_error() -> None:
    calls = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "temporary"})

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=2,
            client=client,
            sleep=sleep,
        )
        with pytest.raises(LLMServerError):
            await provider.complete(llm_request())

    assert calls == 3
    assert delays == [0.5, 1.0]


@pytest.mark.asyncio
async def test_timeout_retries_without_real_sleep_then_raises() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private timeout details", request=request)

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=1,
            client=client,
            sleep=_no_sleep,
        )
        with pytest.raises(LLMTimeoutError) as caught:
            await provider.complete(llm_request())

    assert calls == 2
    assert "private timeout details" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: httpx.Response(200, content=b"not-http-json"),
        lambda: httpx.Response(200, json={"model": "m", "choices": []}),
        lambda: httpx.Response(
            200,
            json={"model": "m", "choices": [{"finish_reason": "stop", "message": {"content": None}}]},
        ),
        lambda: httpx.Response(
            200,
            json={"model": "m", "choices": [{"finish_reason": "stop", "message": {"content": "   "}}]},
        ),
        lambda: httpx.Response(
            200,
            json={"model": "m", "choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]},
        ),
        lambda: httpx.Response(
            200,
            json={"model": "m", "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        ),
    ],
    ids=["malformed-envelope", "empty-choices", "none-content", "blank-content", "invalid-json", "length"],
)
async def test_invalid_responses_are_rejected(
    response_factory: Callable[[], httpx.Response],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_factory()

    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=0,
            client=client,
        )
        with pytest.raises(LLMInvalidResponseError):
            await provider.complete(llm_request())


@pytest.mark.asyncio
async def test_invalid_response_can_retry_and_safe_logs_exclude_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"model": "m", "choices": []})
        return httpx.Response(200, json=valid_payload())

    caplog.set_level(logging.WARNING)
    async with mock_client(handler) as client:
        provider = DeepSeekProvider(
            api_key=API_KEY,
            max_retries=1,
            client=client,
            sleep=_no_sleep,
        )
        await provider.complete(llm_request())

    assert calls == 2
    assert "LLMInvalidResponseError" in caplog.text
    assert "input_char_count=" in caplog.text
    assert API_KEY not in caplog.text
    assert "private-system-prompt" not in caplog.text
    assert "private-conversation-text" not in caplog.text


@pytest.mark.asyncio
async def test_usage_is_optional_and_unknown_usage_fields_are_not_guessed() -> None:
    payload = valid_payload()
    payload.pop("usage")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with mock_client(handler) as client:
        response = await DeepSeekProvider(
            api_key=API_KEY,
            max_retries=0,
            client=client,
        ).complete(llm_request())

    assert response.usage is None


async def _no_sleep(delay: float) -> None:
    del delay
