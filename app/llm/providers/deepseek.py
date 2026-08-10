"""DeepSeek OpenAI-compatible non-streaming Chat Completions provider."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.llm.models import LLMRequest, LLMResponse, LLMUsage
from app.llm.providers.base import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMInvalidResponseError,
    LLMPaymentRequiredError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMServerError,
    LLMTimeoutError,
)


logger = logging.getLogger(__name__)

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})


class DeepSeekProvider:
    """Call DeepSeek without exposing transport details to business services.

    ``max_retries`` means retries after the first request. For example, two
    retries allow at most three total attempts, with 0.5s and 1.0s backoffs.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise LLMAuthenticationError("DeepSeek API key is not configured")
        if not base_url:
            raise ValueError("base_url must not be empty")
        if not model:
            raise ValueError("model must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_retries = max_retries
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "stream": False,
            "thinking": {"type": "disabled"},
            "max_tokens": request.max_output_tokens,
        }
        if request.json_output:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as error:
                failure: LLMProviderError = LLMTimeoutError(
                    "DeepSeek request timed out"
                )
                if await self._retry(failure, attempt=attempt, http_status=None, request=request):
                    continue
                raise failure from error
            except httpx.RequestError as error:
                failure = LLMConnectionError("DeepSeek network request failed")
                if await self._retry(failure, attempt=attempt, http_status=None, request=request):
                    continue
                raise failure from error

            if response.status_code != 200:
                failure = _http_error(response.status_code)
                if (
                    response.status_code in _RETRYABLE_STATUS_CODES
                    and await self._retry(
                        failure,
                        attempt=attempt,
                        http_status=response.status_code,
                        request=request,
                    )
                ):
                    continue
                raise failure

            try:
                return _decode_response(
                    response,
                    configured_model=self._model,
                    json_output=request.json_output,
                )
            except LLMInvalidResponseError as failure:
                if await self._retry(
                    failure,
                    attempt=attempt,
                    http_status=response.status_code,
                    request=request,
                ):
                    continue
                raise

        raise AssertionError("bounded DeepSeek retry loop exited unexpectedly")

    async def aclose(self) -> None:
        """Close the internally-created HTTP client; injected clients stay caller-owned."""

        if self._owns_client:
            await self._client.aclose()

    async def _retry(
        self,
        failure: LLMProviderError,
        *,
        attempt: int,
        http_status: int | None,
        request: LLMRequest,
    ) -> bool:
        if attempt >= self._max_retries:
            return False
        logger.warning(
            "llm request retry provider=deepseek model=%s http_status=%s attempt=%s exception_type=%s input_char_count=%s",
            self._model,
            http_status,
            attempt + 1,
            type(failure).__name__,
            len(request.system_prompt) + len(request.user_prompt),
        )
        await self._sleep(0.5 * (2**attempt))
        return True


def _http_error(status_code: int) -> LLMProviderError:
    message = f"DeepSeek request failed with HTTP {status_code}"
    if status_code == 401:
        return LLMAuthenticationError(message)
    if status_code == 402:
        return LLMPaymentRequiredError(message)
    if status_code in {400, 422}:
        return LLMRequestError(message)
    if status_code == 429:
        return LLMRateLimitError(message)
    if status_code >= 500:
        return LLMServerError(message)
    return LLMRequestError(message)


def _decode_response(
    response: httpx.Response,
    *,
    configured_model: str,
    json_output: bool,
) -> LLMResponse:
    try:
        payload = response.json()
    except ValueError as error:
        raise LLMInvalidResponseError("DeepSeek response was not a JSON object") from error
    if not isinstance(payload, Mapping):
        raise LLMInvalidResponseError("DeepSeek response was not a JSON object")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMInvalidResponseError("DeepSeek response did not contain a choice")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise LLMInvalidResponseError("DeepSeek response choice was invalid")
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise LLMInvalidResponseError("DeepSeek finish_reason was invalid")
    if finish_reason == "length":
        raise LLMInvalidResponseError("DeepSeek output was truncated")

    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise LLMInvalidResponseError("DeepSeek response message was invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMInvalidResponseError("DeepSeek response content was empty")
    if json_output:
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            raise LLMInvalidResponseError(
                "DeepSeek response content was not valid JSON"
            ) from error

    response_model = payload.get("model", configured_model)
    if not isinstance(response_model, str) or not response_model:
        raise LLMInvalidResponseError("DeepSeek response model was invalid")
    return LLMResponse(
        content=content,
        provider="deepseek",
        model=response_model,
        finish_reason=finish_reason,
        usage=_decode_usage(payload.get("usage")),
    )


def _decode_usage(value: object) -> LLMUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LLMInvalidResponseError("DeepSeek usage metadata was invalid")
    return LLMUsage(
        prompt_tokens=_optional_int(value.get("prompt_tokens")),
        completion_tokens=_optional_int(value.get("completion_tokens")),
        total_tokens=_optional_int(value.get("total_tokens")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
