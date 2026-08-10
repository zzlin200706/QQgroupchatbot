"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by future application layers."""

    app_env: str = "development"
    log_level: str = "INFO"

    onebot_ws_url: str = "ws://127.0.0.1:3001"
    onebot_access_token: str = ""
    onebot_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    onebot_action_timeout_seconds: float = Field(default=10.0, gt=0)
    onebot_reconnect_initial_delay_seconds: float = Field(default=1.0, gt=0)
    onebot_reconnect_max_delay_seconds: float = Field(default=30.0, gt=0)

    qq_bot_app_id: str = ""
    qq_bot_app_secret: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/qqgroupchatbot.db"

    forward_max_depth: int = Field(default=10, ge=1)
    forward_max_nodes: int = Field(default=500, ge=1)
    message_max_segments: int = Field(default=1000, ge=1)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings instance per process."""

    return Settings()
