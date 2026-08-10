"""FastAPI application entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from app.adapters.onebot.client import OneBotClient
from app.config import Settings, get_settings


logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    app: str
    environment: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the HTTP application and manage the OneBot adapter lifecycle."""

    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        client = OneBotClient(
            url=app_settings.onebot_ws_url,
            access_token=app_settings.onebot_access_token,
            event_handler=_log_onebot_event,
            connect_timeout=app_settings.onebot_connect_timeout_seconds,
            action_timeout=app_settings.onebot_action_timeout_seconds,
            reconnect_initial_delay=app_settings.onebot_reconnect_initial_delay_seconds,
            reconnect_max_delay=app_settings.onebot_reconnect_max_delay_seconds,
        )
        application.state.onebot_client = client
        await client.start()
        try:
            yield
        finally:
            await client.stop()

    application = FastAPI(
        title="qqgroupchatbot",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            app="qqgroupchatbot",
            environment=app_settings.app_env,
        )

    return application


async def _log_onebot_event(event: dict[str, object]) -> None:
    """Log a safe transport-level event summary without parsing its contents."""

    if event.get("post_type") == "message" and event.get("message_type") == "group":
        logger.info(
            "event=message.group message_id=%s group_id=%s",
            event.get("message_id"),
            event.get("group_id"),
        )
        return

    logger.info("event=onebot.%s", event.get("post_type", "unknown"))


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
