from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.providers import DeepSeekProvider, OpenAICompatibleProvider
from app.llm.providers.factory import (
    LLMProviderConfigurationError,
    create_llm_provider,
)


@pytest.mark.asyncio
async def test_creates_deepseek_provider_from_settings() -> None:
    provider = create_llm_provider(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            deepseek_api_key="test-key",
        )
    )
    try:
        assert isinstance(provider, DeepSeekProvider)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_creates_openai_compatible_provider_from_settings() -> None:
    provider = create_llm_provider(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            openai_compatible_api_key="test-key",
            openai_compatible_base_url="https://relay.example/v1",
            openai_compatible_model="gpt-5.6-luna",
        )
    )
    try:
        assert isinstance(provider, OpenAICompatibleProvider)
    finally:
        await provider.aclose()


def test_unknown_provider_fails_fast() -> None:
    settings = Settings(_env_file=None, llm_provider="unknown")

    with pytest.raises(
        LLMProviderConfigurationError, match="Unsupported LLM provider: unknown"
    ):
        create_llm_provider(settings)
