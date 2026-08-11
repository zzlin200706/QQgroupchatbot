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
            timeout_seconds=settings.openai_compatible_timeout_seconds,
            max_retries=settings.openai_compatible_max_retries,
        )
    raise LLMProviderConfigurationError(
        f"Unsupported LLM provider: {provider_name}"
    )
