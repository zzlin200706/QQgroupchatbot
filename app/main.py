"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from app.adapters.onebot.client import OneBotClient
from app.config import Settings, get_settings
from app.llm.providers import DeepSeekProvider
from app.parsers.onebot_message_parser import OneBotMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.conversation_query import ConversationQueryService
from app.services.normalized_message_ingestion import NormalizedMessageIngestionService
from app.services.raw_event_ingestion import RawEventIngestionService
from app.services.summary import SummaryService
from app.services.summary_command import SummaryCommandHandler, is_summary_command
from app.storage.database import Database
from app.storage.message_repository import MessageRepository
from app.storage.raw_event_repository import RawEventRepository
from app.storage.summary_repository import SummaryRepository


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
        summary_repository = SummaryRepository(database.session_factory)
        summary_command_handler: SummaryCommandHandler | None = None
        deepseek_provider: DeepSeekProvider | None = None
        command_tasks: set[asyncio.Task[object]] = set()

        def command_task_finished(task: asyncio.Task[object]) -> None:
            command_tasks.discard(task)
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                logger.error(
                    "summary command task failed error_type=%s",
                    type(error).__name__,
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
                    if (
                        summary_command_handler is not None
                        and is_summary_command(normalized)
                    ):
                        task = asyncio.create_task(
                            summary_command_handler.handle(
                                normalized,
                                post_type=_scalar_string(event.get("post_type")),
                                self_id=_scalar_string(event.get("self_id")),
                            ),
                            name="onebot-summary-command",
                        )
                        command_tasks.add(task)
                        task.add_done_callback(command_task_finished)

        client = OneBotClient(
            url=app_settings.onebot_ws_url,
            access_token=app_settings.onebot_access_token,
            event_handler=ingest_onebot_event,
            connect_timeout=app_settings.onebot_connect_timeout_seconds,
            action_timeout=app_settings.onebot_action_timeout_seconds,
            reconnect_initial_delay=app_settings.onebot_reconnect_initial_delay_seconds,
            reconnect_max_delay=app_settings.onebot_reconnect_max_delay_seconds,
        )
        if app_settings.summary_command_enabled:
            deepseek_provider = DeepSeekProvider(
                api_key=app_settings.deepseek_api_key.get_secret_value(),
                base_url=app_settings.deepseek_base_url,
                model=app_settings.deepseek_model,
                timeout_seconds=app_settings.deepseek_timeout_seconds,
                max_retries=app_settings.deepseek_max_retries,
            )
            summary_service = SummaryService(
                query_service=ConversationQueryService(message_repository),
                renderer=MessageRenderer(),
                provider=deepseek_provider,
                max_messages=app_settings.summary_max_messages,
                max_input_chars=app_settings.summary_max_input_chars,
                max_output_tokens=app_settings.deepseek_max_output_tokens,
            )
            summary_command_handler = SummaryCommandHandler(
                summary_service=summary_service,
                summary_repository=summary_repository,
                formatter=SummaryMessageFormatter(),
                sender=client,
                enabled=True,
                lookback_minutes=app_settings.summary_command_lookback_minutes,
                cooldown_seconds=app_settings.summary_command_cooldown_seconds,
            )
        application.state.database = database
        application.state.raw_event_repository = raw_repository
        application.state.raw_event_ingestion_service = raw_ingestion_service
        application.state.message_repository = message_repository
        application.state.normalized_message_ingestion_service = normalized_ingestion_service
        application.state.summary_repository = summary_repository
        application.state.summary_command_handler = summary_command_handler
        application.state.onebot_client = client
        await client.start()
        try:
            yield
        finally:
            await client.stop()
            for task in tuple(command_tasks):
                task.cancel()
            if command_tasks:
                await asyncio.gather(*command_tasks, return_exceptions=True)
            if deepseek_provider is not None:
                await deepseek_provider.aclose()
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


def _scalar_string(value: object) -> str | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
