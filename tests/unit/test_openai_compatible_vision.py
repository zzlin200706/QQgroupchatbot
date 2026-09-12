from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from app.domain.images import ResolvedImage
from app.llm import (
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMRequestError,
    LLMTimeoutError,
)
from app.llm.providers.openai_compatible import OpenAICompatibleProvider


API_KEY = "test-vision-key"
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nmock-image"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def payload(content: object = "  screenshot showing ModuleNotFoundError  ") -> dict:
    return {
        "model": "vision-model-actual",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": content},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def image() -> ResolvedImage:
    return ResolvedImage(data=IMAGE_BYTES, content_type="image/png")


@pytest.mark.asyncio
async def test_vision_request_uses_configured_model_prompt_and_data_url() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload())

    async with client(handler) as http_client:
        provider = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            vision_model="vision-model",
            max_retries=0,
            client=http_client,
        )
        result = await provider.describe_image(image(), prompt="Describe factually")

    assert result.description == "screenshot showing ModuleNotFoundError"
    assert len(captured) == 1
    request = captured[0]
    assert request.url == httpx.URL("https://relay.example/v1/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "vision-model"
    assert body["stream"] is False
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Describe factually"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,"
            + base64.b64encode(IMAGE_BYTES).decode("ascii")
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json=payload(content=None)),
        httpx.Response(200, json=payload(content="   ")),
    ],
)
async def test_malformed_vision_response_is_rejected(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with client(handler) as http_client:
        provider = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            vision_model="vision-model",
            max_retries=0,
            client=http_client,
        )
        with pytest.raises(LLMInvalidResponseError):
            await provider.describe_image(image(), prompt="Describe")


@pytest.mark.asyncio
async def test_vision_api_error_is_mapped_without_response_body_or_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "private body"}})

    async with client(handler) as http_client:
        provider = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            vision_model="vision-model",
            max_retries=0,
            client=http_client,
        )
        with pytest.raises(LLMAuthenticationError) as caught:
            await provider.describe_image(image(), prompt="Describe")

    assert API_KEY not in str(caught.value)
    assert "private body" not in str(caught.value)


@pytest.mark.asyncio
async def test_vision_timeout_and_retry_logs_do_not_expose_image_or_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("private timeout", request=request)

    async def no_sleep(delay: float) -> None:
        del delay

    caplog.set_level(logging.WARNING)
    async with client(handler) as http_client:
        provider = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            vision_model="vision-model",
            max_retries=1,
            client=http_client,
            sleep=no_sleep,
        )
        with pytest.raises(LLMTimeoutError):
            await provider.describe_image(image(), prompt="Describe")

    assert calls == 2
    assert API_KEY not in caplog.text
    assert base64.b64encode(IMAGE_BYTES).decode("ascii") not in caplog.text
    assert "private timeout" not in caplog.text


@pytest.mark.asyncio
async def test_vision_requires_explicit_model_and_nonempty_prompt() -> None:
    async with client(lambda request: httpx.Response(500)) as http_client:
        provider = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            max_retries=0,
            client=http_client,
        )
        assert provider.supports_vision is False
        with pytest.raises(LLMRequestError, match="not configured"):
            await provider.describe_image(image(), prompt="Describe")

        configured = OpenAICompatibleProvider(
            api_key=API_KEY,
            base_url="https://relay.example/v1",
            model="text-model",
            vision_model="vision-model",
            max_retries=0,
            client=http_client,
        )
        with pytest.raises(ValueError, match="prompt"):
            await configured.describe_image(image(), prompt="  ")
