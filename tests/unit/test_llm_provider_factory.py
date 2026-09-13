from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.providers import DeepSeekProvider, OpenAICompatibleProvider
from app.llm.providers.factory import (
    LLMProviderConfigurationError,
    create_llm_provider,
    create_vision_provider,
)


@pytest.mark.asyncio
async def test_creates_deepseek_provider_from_settings() -> None:
    provider = create_llm_provider(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            deepseek_api_key="test-key",
            deepseek_model="deepseek-chat",
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


def test_disabled_vision_does_not_create_provider() -> None:
    settings = Settings(_env_file=None, vision_enabled=False)

    assert create_vision_provider(settings) is None


@pytest.mark.asyncio
async def test_openai_text_and_vision_reuse_one_configured_provider() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        openai_compatible_api_key="test-key",
        openai_compatible_base_url="https://relay.example/v1",
        openai_compatible_model="text-model",
        vision_enabled=True,
        vision_model="vision-model",
    )
    provider = create_llm_provider(settings)
    try:
        vision = create_vision_provider(settings, llm_provider=provider)
        assert vision is provider
        assert vision.supports_vision is True
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_deepseek_text_uses_separate_openai_vision_provider() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        deepseek_api_key="test-deepseek-key",
        deepseek_model="deepseek-chat",
        openai_compatible_api_key="test-vision-key",
        openai_compatible_base_url="https://relay.example/v1",
        vision_enabled=True,
        vision_model="vision-model",
    )
    text_provider = create_llm_provider(settings)
    vision = create_vision_provider(settings, llm_provider=text_provider)
    assert vision is not None
    try:
        assert vision is not text_provider
        assert isinstance(vision, OpenAICompatibleProvider)
        assert vision.supports_vision is True
    finally:
        await text_provider.aclose()
        await vision.aclose()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"vision_provider": "unsupported", "vision_model": "vision"}, "Unsupported vision provider"),
        ({"vision_provider": "openai_compatible", "vision_model": ""}, "VISION_MODEL"),
    ],
)
def test_invalid_vision_configuration_fails_clearly(
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, vision_enabled=True, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary_command_enabled": True, "llm_provider": "deepseek"},
        {
            "summary_command_enabled": True,
            "llm_provider": "openai_compatible",
            "openai_compatible_api_key": "k",
        },
        {
            "group_assistant_enabled": True,
            "llm_provider": "openai_compatible",
            "openai_compatible_base_url": "https://relay.example/v1",
        },
        {"summary_command_enabled": True, "llm_provider": "unknown"},
    ],
)
def test_enabled_text_features_require_text_provider_credentials(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_enabled_vision_requires_credentials_and_model() -> None:
    with pytest.raises(ValidationError, match="VISION_MODEL"):
        Settings(
            _env_file=None,
            vision_enabled=True,
            openai_compatible_api_key="k",
            openai_compatible_base_url="https://relay.example/v1",
        )

    with pytest.raises(ValidationError, match="OPENAI_COMPATIBLE_API_KEY"):
        Settings(
            _env_file=None,
            vision_enabled=True,
            vision_model="vision-model",
            openai_compatible_base_url="https://relay.example/v1",
        )

    with pytest.raises(ValidationError, match="OPENAI_COMPATIBLE_BASE_URL"):
        Settings(
            _env_file=None,
            vision_enabled=True,
            vision_model="vision-model",
            openai_compatible_api_key="k",
        )


def test_all_features_disabled_skips_provider_validation() -> None:
    # An unsupported llm_provider must still be loadable when nothing is
    # enabled; the validator only runs when text or vision is actually used.
    settings = Settings(_env_file=None, llm_provider="unknown")
    assert settings.llm_provider == "unknown"

    settings = Settings(_env_file=None, vision_enabled=False, vision_model="")
    assert settings.vision_enabled is False
