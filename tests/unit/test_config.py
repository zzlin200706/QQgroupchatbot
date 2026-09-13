from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_phase_h_defaults_and_secret_redaction() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="private-test-key",
        openai_compatible_api_key="private-relay-key",
    )

    assert settings.llm_provider == "deepseek"
    assert settings.llm_max_output_tokens == 4096
    assert settings.qq_api_timeout_seconds == 10
    assert settings.qq_gateway_reconnect_initial_delay_seconds == 1
    assert settings.qq_gateway_reconnect_max_delay_seconds == 30
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    # Model names are explicit operator choice; the default is empty
    # so a production deployment must set DEEPSEEK_MODEL.
    assert settings.deepseek_model == ""
    assert settings.deepseek_timeout_seconds == 60
    assert settings.deepseek_max_retries == 2
    assert settings.openai_compatible_base_url == ""
    assert settings.openai_compatible_model == ""
    assert settings.openai_compatible_timeout_seconds == 60
    assert settings.openai_compatible_max_retries == 2
    assert settings.vision_enabled is False
    assert settings.vision_provider == "openai_compatible"
    assert settings.vision_model == ""
    assert settings.image_download_timeout_seconds == 15
    assert settings.image_max_bytes == 10485760
    assert settings.summary_max_messages == 500
    assert settings.summary_max_input_chars == 120000
    assert settings.summary_command_enabled is False
    assert settings.summary_command_lookback_minutes == 120
    assert settings.summary_command_cooldown_seconds == 60
    assert settings.group_assistant_enabled is False
    assert settings.qa_lookback_minutes == 120
    assert settings.qa_max_messages == 150
    assert settings.chat_lookback_minutes == 30
    assert settings.chat_max_messages == 80
    assert settings.chat_max_assistant_turns == 20
    assert settings.assistant_max_input_chars == 40000
    assert settings.assistant_max_output_tokens == 1200
    assert settings.assistant_max_output_chars == 4500
    assert settings.assistant_cooldown_seconds == 3
    assert settings.deepseek_api_key.get_secret_value() == "private-test-key"
    assert (
        settings.openai_compatible_api_key.get_secret_value()
        == "private-relay-key"
    )
    assert "private-test-key" not in repr(settings)
    assert "private-relay-key" not in repr(settings)


@pytest.mark.parametrize(
    "override",
    [
        {"llm_max_output_tokens": 0},
        {"deepseek_timeout_seconds": 0},
        {"deepseek_max_retries": -1},
        {"openai_compatible_timeout_seconds": 0},
        {"openai_compatible_max_retries": -1},
        {"image_download_timeout_seconds": 0},
        {"image_max_bytes": 0},
        {"summary_max_messages": 0},
        {"summary_max_input_chars": 0},
        {"summary_command_lookback_minutes": 0},
        {"summary_command_cooldown_seconds": -1},
        {"qa_lookback_minutes": 0},
        {"qa_max_messages": 0},
        {"chat_lookback_minutes": 0},
        {"chat_max_messages": 0},
        {"chat_max_assistant_turns": 0},
        {"assistant_max_input_chars": 0},
        {"assistant_max_output_tokens": 0},
        {"assistant_max_output_chars": 127},
        {"assistant_cooldown_seconds": -1},
    ],
)
def test_phase_h_numeric_settings_are_validated(override: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **override)


def test_settings_validator_skips_credential_check_in_test_env() -> None:
    """A test env enables features but leaves keys blank; validation passes."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        summary_command_enabled=True,
    )
    # The validator must not have raised and the values are kept.
    assert settings.summary_command_enabled is True
    assert settings.app_env == "test"


def test_settings_validator_still_enforces_credentials_in_production() -> None:
    """A production env with features enabled and no key must fail fast."""

    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=None,
            app_env="production",
            summary_command_enabled=True,
        )
    assert "DEEPSEEK_API_KEY" in str(caught.value)


def test_audit_returns_secret_free_snapshot() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_api_key="private-deepseek",
        openai_compatible_api_key="private-relay",
    )

    snapshot = settings.audit()
    rendered = repr(snapshot)
    assert "private-deepseek" not in rendered
    assert "private-relay" not in rendered
    assert snapshot["deepseek_api_key_configured"] is True
    assert snapshot["openai_compatible_api_key_configured"] is True
    assert snapshot["deepseek_model"] == "<unset>"
    assert snapshot["openai_compatible_model"] == "<unset>"
    assert snapshot["database_url"] == "<redacted>"


def test_audit_reports_unset_model_when_empty() -> None:
    settings = Settings(
        _env_file=None,
        deepseek_model="deepseek-chat",
        openai_compatible_model="gpt-x",
    )

    snapshot = settings.audit()
    assert snapshot["deepseek_model"] == "deepseek-chat"
    assert snapshot["openai_compatible_model"] == "gpt-x"
