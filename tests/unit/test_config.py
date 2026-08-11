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
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_timeout_seconds == 60
    assert settings.deepseek_max_retries == 2
    assert settings.openai_compatible_base_url == ""
    assert settings.openai_compatible_model == "gpt-5.6-luna"
    assert settings.openai_compatible_timeout_seconds == 60
    assert settings.openai_compatible_max_retries == 2
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
