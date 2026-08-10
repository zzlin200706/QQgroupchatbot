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
from app.parsers.onebot_message_parser import OneBotMessageParser
from app.services.normalized_message_ingestion import NormalizedMessageIngestionService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.raw_event_repository import RawEventRepository


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
        database = Database(app_settings.database_url)
        await database.initialize()
        raw_repository = RawEventRepository(database.session_factory)
        raw_ingestion_service = RawEventIngestionService(raw_repository)
        message_repository = MessageRepository(database.session_factory)
        normalized_ingestion_service = NormalizedMessageIngestionService(
            repository=message_repository,
            parser=OneBotMessageParser(
                max_forward_depth=app_settings.forward_max_depth,
                max_forward_nodes=app_settings.forward_max_nodes,
                max_message_segments=app_settings.message_max_segments,
            ),
        )

        async def ingest_onebot_event(event: dict[str, object]) -> None:
            persisted = await raw_ingestion_service.ingest(event)
            if persisted is not None:
                logger.info(
                    "raw event persisted id=%s post_type=%s message_type=%s payload_hash=%s",
                    persisted.id,
                    persisted.post_type,
                    persisted.message_type,
                    persisted.payload_hash,
                )
                normalized = await normalized_ingestion_service.ingest(persisted)
                if normalized is not None:
                    logger.info(
                        "normalized message persisted raw_event_id=%s platform=%s",
                        persisted.id,
                        normalized.platform,
                    )

        client = OneBotClient(
            url=app_settings.onebot_ws_url,
            access_token=app_settings.onebot_access_token,
            event_handler=ingest_onebot_event,
            connect_timeout=app_settings.onebot_connect_timeout_seconds,
            action_timeout=app_settings.onebot_action_timeout_seconds,
            reconnect_initial_delay=app_settings.onebot_reconnect_initial_delay_seconds,
            reconnect_max_delay=app_settings.onebot_reconnect_max_delay_seconds,
        )
        application.state.database = database
        application.state.raw_event_repository = raw_repository
        application.state.raw_event_ingestion_service = raw_ingestion_service
        application.state.message_repository = message_repository
        application.state.normalized_message_ingestion_service = normalized_ingestion_service
        application.state.onebot_client = client
        await client.start()
        try:
            yield
        finally:
            await client.stop()
            await database.dispose()

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


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
