"""Provider contract and implementation-neutral failure categories."""

from __future__ import annotations

from typing import Protocol

from app.llm.models import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return one non-streaming completion or raise an LLMProviderError."""


class LLMProviderError(Exception):
    """Base class for safe provider failures."""


class LLMAuthenticationError(LLMProviderError):
    """Provider credentials are missing or rejected."""


class LLMPaymentRequiredError(LLMProviderError):
    """The provider account has insufficient balance."""


class LLMRequestError(LLMProviderError):
    """The provider rejected a permanent request error."""


class LLMRateLimitError(LLMProviderError):
    """The bounded retry budget was exhausted after rate limiting."""


class LLMServerError(LLMProviderError):
    """The provider returned a server-side failure."""


class LLMTimeoutError(LLMProviderError):
    """The bounded retry budget was exhausted after timeouts."""


class LLMConnectionError(LLMProviderError):
    """The bounded retry budget was exhausted after network failures."""


class LLMInvalidResponseError(LLMProviderError):
    """The provider returned an unusable envelope or completion."""
