"""OpenAI-compatible non-streaming Chat Completions provider."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from app.domain.images import ImageDescription, ResolvedImage
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

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})


class OpenAICompatibleProvider:
    """Call one OpenAI-compatible Chat Completions endpoint with bounded retries."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        vision_model: str | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise LLMAuthenticationError(
                "OpenAI-compatible API key is not configured"
            )
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
        self._vision_model = vision_model
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
        }
        return await self._complete_payload(
            payload,
            configured_model=self._model,
            json_output=request.json_output,
            input_char_count=len(request.system_prompt) + len(request.user_prompt),
        )

    @property
    def supports_vision(self) -> bool:
        return bool(self._vision_model)

    async def describe_image(
        self,
        image: ResolvedImage,
        *,
        prompt: str,
    ) -> ImageDescription:
        if not self._vision_model:
            raise LLMRequestError("OpenAI-compatible vision model is not configured")
        if not prompt.strip():
            raise ValueError("vision prompt must not be empty")
        encoded = base64.b64encode(image.data).decode("ascii")
        payload: dict[str, Any] = {
            "model": self._vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{image.content_type};base64,{encoded}"
                            },
                        },
                    ],
                }
            ],
            "stream": False,
        }
        response = await self._complete_payload(
            payload,
            configured_model=self._vision_model,
            json_output=False,
            input_char_count=len(prompt),
        )
        return ImageDescription(description=response.content)

    async def _complete_payload(
        self,
        payload: dict[str, Any],
        *,
        configured_model: str,
        json_output: bool,
        input_char_count: int,
    ) -> LLMResponse:

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
                    "OpenAI-compatible request timed out"
                )
                if await self._retry(
                    failure,
                    attempt=attempt,
                    http_status=None,
                    input_char_count=input_char_count,
                    configured_model=configured_model,
                ):
                    continue
                raise failure from error
            except httpx.RequestError as error:
                failure = LLMConnectionError(
                    "OpenAI-compatible network request failed"
                )
                if await self._retry(
                    failure,
                    attempt=attempt,
                    http_status=None,
                    input_char_count=input_char_count,
                    configured_model=configured_model,
                ):
                    continue
                raise failure from error

            if response.status_code != 200:
                failure = _http_error(response.status_code)
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    if await self._retry(
                        failure,
                        attempt=attempt,
                        http_status=response.status_code,
                        input_char_count=input_char_count,
                        configured_model=configured_model,
                        retry_after_seconds=retry_after,
                    ):
                        continue
                raise failure

            try:
                return _decode_response(
                    response,
                    configured_model=configured_model,
                    json_output=json_output,
                )
            except LLMInvalidResponseError as failure:
                if await self._retry(
                    failure,
                    attempt=attempt,
                    http_status=response.status_code,
                    input_char_count=input_char_count,
                    configured_model=configured_model,
                ):
                    continue
                raise

        raise AssertionError("bounded OpenAI-compatible retry loop exited unexpectedly")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _retry(
        self,
        failure: LLMProviderError,
        *,
        attempt: int,
        http_status: int | None,
        input_char_count: int,
        configured_model: str,
        retry_after_seconds: float | None = None,
    ) -> bool:
        if attempt >= self._max_retries:
            return False
        # Cap the back-off to avoid the bot stalling for hours if a
        # provider sends an abusive Retry-After header.
        if retry_after_seconds is None:
            delay = 0.5 * (2**attempt)
        else:
            delay = max(0.5 * (2**attempt), min(retry_after_seconds, 60.0))
        logger.warning(
            "llm request retry provider=openai_compatible model=%s http_status=%s attempt=%s exception_type=%s input_char_count=%s delay=%s",
            configured_model,
            http_status,
            attempt + 1,
            type(failure).__name__,
            input_char_count,
            f"{delay:.2f}s",
        )
        await self._sleep(delay)
        return True


def _http_error(status_code: int) -> LLMProviderError:
    message = f"OpenAI-compatible request failed with HTTP {status_code}"
    if status_code in {401, 403}:
        return LLMAuthenticationError(message)
    if status_code == 402:
        return LLMPaymentRequiredError(message)
    if status_code in {400, 404, 422}:
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
        raise LLMInvalidResponseError(
            "OpenAI-compatible response was not a JSON object"
        ) from error
    if not isinstance(payload, Mapping):
        raise LLMInvalidResponseError(
            "OpenAI-compatible response was not a JSON object"
        )

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMInvalidResponseError(
            "OpenAI-compatible response did not contain a choice"
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise LLMInvalidResponseError(
            "OpenAI-compatible response choice was invalid"
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise LLMInvalidResponseError(
            "OpenAI-compatible finish_reason was invalid"
        )
    if finish_reason == "length":
        raise LLMInvalidResponseError("OpenAI-compatible output was truncated")

    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise LLMInvalidResponseError(
            "OpenAI-compatible response message was invalid"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMInvalidResponseError(
            "OpenAI-compatible response content was empty"
        )
    if json_output:
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            raise LLMInvalidResponseError(
                "OpenAI-compatible response content was not valid JSON"
            ) from error

    response_model = payload.get("model", configured_model)
    if not isinstance(response_model, str) or not response_model:
        raise LLMInvalidResponseError(
            "OpenAI-compatible response model was invalid"
        )
    return LLMResponse(
        content=content,
        provider="openai_compatible",
        model=response_model,
        finish_reason=finish_reason,
        usage=_decode_usage(payload.get("usage")),
    )


def _decode_usage(value: object) -> LLMUsage | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LLMInvalidResponseError(
            "OpenAI-compatible usage metadata was invalid"
        )
    return LLMUsage(
        prompt_tokens=_optional_int(value.get("prompt_tokens")),
        completion_tokens=_optional_int(value.get("completion_tokens")),
        total_tokens=_optional_int(value.get("total_tokens")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_retry_after(value: str | None) -> float | None:
    """Return the documented seconds value or None for invalid input."""

    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    # HTTP-date form is rare; we only honour the documented delta-seconds form.
    try:
        seconds = float(candidate)
    except ValueError:
        return None
    if seconds < 0 or seconds > 300.0:
        # The provider will not actually ask us to wait days; clamp.
        return None
    return seconds
