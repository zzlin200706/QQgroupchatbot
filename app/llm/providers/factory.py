"""Provider selection for composition roots."""

from __future__ import annotations

from app.config import Settings
from app.llm.providers.deepseek import DeepSeekProvider
from app.llm.providers.openai_compatible import OpenAICompatibleProvider


class LLMProviderConfigurationError(ValueError):
    """Raised when provider selection cannot be resolved from settings."""


def create_llm_provider(
    settings: Settings,
) -> DeepSeekProvider | OpenAICompatibleProvider:
    provider_name = settings.llm_provider
    if provider_name == "deepseek":
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout_seconds=settings.deepseek_timeout_seconds,
            max_retries=settings.deepseek_max_retries,
        )
    if provider_name == "openai_compatible":
        return OpenAICompatibleProvider(
            api_key=settings.openai_compatible_api_key.get_secret_value(),
            base_url=settings.openai_compatible_base_url,
            model=settings.openai_compatible_model,
            vision_model=(
                settings.vision_model
                if settings.vision_enabled
                and settings.vision_provider == "openai_compatible"
                and settings.vision_model
                else None
            ),
            timeout_seconds=settings.openai_compatible_timeout_seconds,
            max_retries=settings.openai_compatible_max_retries,
        )
    raise LLMProviderConfigurationError(
        f"Unsupported LLM provider: {provider_name}"
    )


def create_vision_provider(
    settings: Settings,
    *,
    llm_provider: DeepSeekProvider | OpenAICompatibleProvider | None = None,
) -> OpenAICompatibleProvider | None:
    if not settings.vision_enabled:
        return None
    if settings.vision_provider != "openai_compatible":
        raise LLMProviderConfigurationError(
            f"Unsupported vision provider: {settings.vision_provider}"
        )
    if not settings.vision_model:
        raise LLMProviderConfigurationError(
            "VISION_MODEL must be configured when vision is enabled"
        )
    if (
        isinstance(llm_provider, OpenAICompatibleProvider)
        and llm_provider.supports_vision
    ):
        return llm_provider
    return OpenAICompatibleProvider(
        api_key=settings.openai_compatible_api_key.get_secret_value(),
        base_url=settings.openai_compatible_base_url,
        model=settings.openai_compatible_model or settings.vision_model,
        vision_model=settings.vision_model,
        timeout_seconds=settings.openai_compatible_timeout_seconds,
        max_retries=settings.openai_compatible_max_retries,
    )
