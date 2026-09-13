"""Application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by future application layers."""

    app_env: str = "development"
    log_level: str = "INFO"
    llm_provider: str = "deepseek"
    llm_max_output_tokens: int = Field(default=4096, gt=0)

    qq_bot_app_id: str = ""
    qq_bot_app_secret: str = ""
    qq_event_transport: Literal["websocket", "webhook"] = "websocket"
    qq_api_timeout_seconds: float = Field(default=10.0, gt=0)
    qq_gateway_reconnect_initial_delay_seconds: float = Field(default=1.0, gt=0)
    qq_gateway_reconnect_max_delay_seconds: float = Field(default=30.0, gt=0)
    qq_shutdown_drain_timeout_seconds: float = Field(default=5.0, ge=0)

    database_url: str = "sqlite+aiosqlite:///./data/qqgroupchatbot.db"

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = Field(default=60.0, gt=0)
    deepseek_max_retries: int = Field(default=2, ge=0)

    openai_compatible_api_key: SecretStr = SecretStr("")
    openai_compatible_base_url: str = ""
    openai_compatible_model: str = "gpt-5.6-luna"
    openai_compatible_timeout_seconds: float = Field(default=60.0, gt=0)
    openai_compatible_max_retries: int = Field(default=2, ge=0)

    vision_enabled: bool = False
    vision_provider: str = "openai_compatible"
    vision_model: str = ""
    image_download_timeout_seconds: float = Field(default=15.0, gt=0)
    image_max_bytes: int = Field(default=10485760, ge=1)

    summary_max_messages: int = Field(default=500, ge=1)
    summary_max_input_chars: int = Field(default=120000, ge=1)
    summary_command_enabled: bool = False
    summary_command_lookback_minutes: int = Field(default=120, ge=1)
    summary_command_cooldown_seconds: int = Field(default=60, ge=0)

    # Process-wide bound on in-flight LLM-bound interactions (summary / assistant).
    # A large group of users can otherwise fan out N concurrent LLM calls.
    llm_max_concurrent_interactions: int = Field(default=4, ge=1)

    # Retention for the durable outbox: terminal (completed/failed)
    # rows older than this many days are deleted on worker startup.
    # 0 disables pruning.  Pending and running rows are never pruned.
    interaction_outbox_retention_days: int = Field(default=7, ge=0)

    # Retention for raw_events: receipts older than this many days
    # are pruned on worker startup, subject to FK safety: a row
    # referenced by any ``messages`` row or by any pending/running
    # ``interaction_outbox`` row is preserved.  0 disables pruning.
    raw_events_retention_days: int = Field(default=30, ge=0)
    # Bound on the per-tx delete batch to keep long-table locks
    # short.  Chunks are processed in a loop until the table is
    # caught up to the cutoff.
    raw_events_prune_chunk_size: int = Field(default=1000, ge=1)

    group_assistant_enabled: bool = False
    qa_lookback_minutes: int = Field(default=120, ge=1)
    qa_max_messages: int = Field(default=150, ge=1)
    chat_lookback_minutes: int = Field(default=30, ge=1)
    chat_max_messages: int = Field(default=80, ge=1)
    chat_max_assistant_turns: int = Field(default=20, ge=1)
    assistant_max_input_chars: int = Field(default=40000, ge=1)
    assistant_max_output_tokens: int = Field(default=1200, ge=1)
    assistant_max_output_chars: int = Field(default=4500, ge=128)
    assistant_cooldown_seconds: int = Field(default=3, ge=0)

    forward_max_depth: int = Field(default=10, ge=1)
    forward_max_nodes: int = Field(default=500, ge=1)
    message_max_segments: int = Field(default=1000, ge=1)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    @model_validator(mode="after")
    def _validate_enabled_features(self) -> "Settings":
        """Fail fast when an enabled feature is missing required config.

        Only validate when at least one text or vision feature is enabled,
        so unit tests that build a Settings with everything disabled can
        still leave the provider credentials blank.  When the
        application is started in a test environment, the credential
        check is also skipped: the test harness patches the LLM provider
        factory directly, so a blank key is the documented test seam.
        """
        text_enabled = self.summary_command_enabled or self.group_assistant_enabled
        vision_enabled = self.vision_enabled
        if not text_enabled and not vision_enabled:
            return self
        if self.app_env.lower() in {"test", "testing"}:
            return self

        if self.llm_provider not in {"deepseek", "openai_compatible"}:
            raise ValueError(
                f"Unsupported LLM provider: {self.llm_provider!r}. "
                "Expected 'deepseek' or 'openai_compatible'."
            )

        if text_enabled:
            if self.llm_provider == "deepseek":
                if not self.deepseek_api_key.get_secret_value():
                    raise ValueError(
                        "DEEPSEEK_API_KEY is required when "
                        "SUMMARY_COMMAND_ENABLED or GROUP_ASSISTANT_ENABLED is true"
                    )
            else:  # openai_compatible
                if not self.openai_compatible_api_key.get_secret_value():
                    raise ValueError(
                        "OPENAI_COMPATIBLE_API_KEY is required when "
                        "SUMMARY_COMMAND_ENABLED or GROUP_ASSISTANT_ENABLED is true"
                    )
                if not self.openai_compatible_base_url:
                    raise ValueError(
                        "OPENAI_COMPATIBLE_BASE_URL is required when "
                        "SUMMARY_COMMAND_ENABLED or GROUP_ASSISTANT_ENABLED is true"
                    )

        if vision_enabled:
            if self.vision_provider != "openai_compatible":
                raise ValueError(
                    f"Unsupported vision provider: {self.vision_provider!r}. "
                    "Expected 'openai_compatible'."
                )
            if not self.vision_model:
                raise ValueError(
                    "VISION_MODEL is required when VISION_ENABLED is true"
                )
            if not self.openai_compatible_api_key.get_secret_value():
                raise ValueError(
                    "OPENAI_COMPATIBLE_API_KEY is required when "
                    "VISION_ENABLED is true"
                )
            if not self.openai_compatible_base_url:
                    raise ValueError(
                        "OPENAI_COMPATIBLE_BASE_URL is required when "
                        "VISION_ENABLED is true"
                    )

        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""

    return Settings()
