"""Provider contract and implementation-neutral failure categories."""

from __future__ import annotations

from typing import Protocol

from app.domain.images import ImageDescription, ResolvedImage
from app.llm.models import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return one non-streaming completion or raise an LLMProviderError."""


class VisionProvider(Protocol):
    @property
    def supports_vision(self) -> bool:
        """Return whether image description is configured."""

    async def describe_image(
        self,
        image: ResolvedImage,
        *,
        prompt: str,
    ) -> ImageDescription:
        """Describe one resolved image or raise an LLMProviderError."""

    async def aclose(self) -> None:
        """Release provider-owned resources."""


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
