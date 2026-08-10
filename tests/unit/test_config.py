from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_phase_h_defaults_and_secret_redaction() -> None:
    settings = Settings(_env_file=None, deepseek_api_key="private-test-key")

    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_timeout_seconds == 60
    assert settings.deepseek_max_retries == 2
    assert settings.deepseek_max_output_tokens == 4096
    assert settings.summary_max_messages == 500
    assert settings.summary_max_input_chars == 120000
    assert settings.summary_command_enabled is False
    assert settings.summary_command_lookback_minutes == 120
    assert settings.summary_command_cooldown_seconds == 60
    assert settings.deepseek_api_key.get_secret_value() == "private-test-key"
    assert "private-test-key" not in repr(settings)


@pytest.mark.parametrize(
    "override",
    [
        {"deepseek_timeout_seconds": 0},
        {"deepseek_max_retries": -1},
        {"deepseek_max_output_tokens": 0},
        {"summary_max_messages": 0},
        {"summary_max_input_chars": 0},
        {"summary_command_lookback_minutes": 0},
        {"summary_command_cooldown_seconds": -1},
    ],
)
def test_phase_h_numeric_settings_are_validated(override: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **override)
