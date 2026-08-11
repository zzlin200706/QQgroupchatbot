"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from app.adapters.qq_official import (
    QQOfficialAuthClient,
    QQOfficialAuthError,
    QQOfficialGatewayClient,
    QQOfficialGatewayError,
    QQOfficialGroupMessageSender,
)
from app.config import Settings, get_settings
from app.llm.providers import create_llm_provider
from app.parsers.qq_official_message_parser import QQOfficialMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.conversation_query import ConversationQueryService
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.ping_command import PingCommandHandler
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.services.summary import SummaryService
from app.services.summary_command import SummaryCommandHandler
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
    """Create the HTTP application and manage the QQ Official runtime."""

    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(app_settings.database_url)
        await database.initialize()
        qq_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(app_settings.qq_api_timeout_seconds)
        )
        raw_repository = RawEventRepository(database.session_factory)
        raw_ingestion_service = QQOfficialRawEventIngestionService(raw_repository)
        message_repository = MessageRepository(database.session_factory)
        normalized_ingestion_service = QQOfficialNormalizedMessageIngestionService(
            repository=message_repository,
            parser=QQOfficialMessageParser(),
        )
        summary_repository = SummaryRepository(database.session_factory)
        auth_client = QQOfficialAuthClient(
            app_id=app_settings.qq_bot_app_id,
            app_secret=app_settings.qq_bot_app_secret,
            timeout_seconds=app_settings.qq_api_timeout_seconds,
            client=qq_http_client,
        )
        gateway_client = QQOfficialGatewayClient(
            auth_client=auth_client,
            timeout_seconds=app_settings.qq_api_timeout_seconds,
            http_client=qq_http_client,
        )
        group_message_sender = QQOfficialGroupMessageSender(
            auth_client=auth_client,
            timeout_seconds=app_settings.qq_api_timeout_seconds,
            http_client=qq_http_client,
        )
        ping_command_handler = PingCommandHandler(sender=group_message_sender)
        summary_command_handler: SummaryCommandHandler | None = None
        command_dispatcher = QQOfficialCommandDispatcher(
            ping_handler=ping_command_handler
        )
        llm_provider = None
        command_tasks: set[asyncio.Task[object]] = set()
        gateway_stop = asyncio.Event()

        def command_task_finished(task: asyncio.Task[object]) -> None:
            command_tasks.discard(task)
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                logger.error(
                    "qq official command task failed task_name=%s error_type=%s",
                    task.get_name(),
                    type(error).__name__,
                )

        async def handle_dispatch_loop() -> None:
            retry_delay = app_settings.qq_gateway_reconnect_initial_delay_seconds
            while not gateway_stop.is_set():
                try:
                    await gateway_client.connect()
                    logger.info("qq official gateway connected")
                    retry_delay = app_settings.qq_gateway_reconnect_initial_delay_seconds
                    while not gateway_stop.is_set():
                        dispatch = await gateway_client.next_event()
                        persisted = await raw_ingestion_service.ingest(dispatch)
                        if persisted is None:
                            continue
                        logger.info(
                            "raw event persisted id=%s event_type=%s payload_hash=%s",
                            persisted.id,
                            persisted.sub_type,
                            persisted.payload_hash,
                        )
                        normalized = await normalized_ingestion_service.ingest(
                            persisted
                        )
                        if normalized is None:
                            continue
                        logger.info(
                            "normalized message persisted raw_event_id=%s platform=%s",
                            persisted.id,
                            normalized.platform,
                        )
                        command_name = command_dispatcher.command_name(normalized)
                        if command_name is not None:
                            task = asyncio.create_task(
                                command_dispatcher.handle(normalized),
                                name=f"qq-official-{command_name}-command",
                            )
                            command_tasks.add(task)
                            task.add_done_callback(command_task_finished)
                except (QQOfficialAuthError, QQOfficialGatewayError) as error:
                    if gateway_stop.is_set():
                        break
                    logger.warning(
                        "qq official gateway loop failed error_type=%s",
                        type(error).__name__,
                    )
                except Exception:
                    if gateway_stop.is_set():
                        break
                    logger.exception("qq official gateway loop crashed")
                finally:
                    await gateway_client.close()

                if gateway_stop.is_set():
                    break

                sleep_seconds = _gateway_retry_delay_seconds(
                    gateway_client=gateway_client,
                    configured_delay=retry_delay,
                )
                logger.info(
                    "qq official gateway reconnect scheduled delay_seconds=%.2f",
                    sleep_seconds,
                )
                try:
                    await asyncio.wait_for(gateway_stop.wait(), timeout=sleep_seconds)
                except asyncio.TimeoutError:
                    pass
                retry_delay = min(
                    retry_delay * 2,
                    app_settings.qq_gateway_reconnect_max_delay_seconds,
                )

        if app_settings.summary_command_enabled:
            llm_provider = create_llm_provider(app_settings)
            summary_service = SummaryService(
                query_service=ConversationQueryService(message_repository),
                renderer=MessageRenderer(),
                provider=llm_provider,
                max_messages=app_settings.summary_max_messages,
                max_input_chars=app_settings.summary_max_input_chars,
                max_output_tokens=app_settings.llm_max_output_tokens,
            )
            summary_command_handler = SummaryCommandHandler(
                summary_service=summary_service,
                summary_repository=summary_repository,
                formatter=SummaryMessageFormatter(),
                sender=group_message_sender,
                enabled=True,
                lookback_minutes=app_settings.summary_command_lookback_minutes,
                cooldown_seconds=app_settings.summary_command_cooldown_seconds,
            )
            command_dispatcher = QQOfficialCommandDispatcher(
                ping_handler=ping_command_handler,
                summary_handler=summary_command_handler,
            )

        gateway_task = asyncio.create_task(
            handle_dispatch_loop(),
            name="qq-official-gateway-loop",
        )

        application.state.database = database
        application.state.qq_http_client = qq_http_client
        application.state.qq_auth_client = auth_client
        application.state.qq_gateway_client = gateway_client
        application.state.qq_gateway_task = gateway_task
        application.state.qq_group_message_sender = group_message_sender
        application.state.qq_command_dispatcher = command_dispatcher
        application.state.ping_command_handler = ping_command_handler
        application.state.raw_event_repository = raw_repository
        application.state.raw_event_ingestion_service = raw_ingestion_service
        application.state.message_repository = message_repository
        application.state.normalized_message_ingestion_service = normalized_ingestion_service
        application.state.summary_repository = summary_repository
        application.state.summary_command_handler = summary_command_handler
        try:
            yield
        finally:
            gateway_stop.set()
            gateway_task.cancel()
            await asyncio.gather(gateway_task, return_exceptions=True)
            for task in tuple(command_tasks):
                task.cancel()
            if command_tasks:
                await asyncio.gather(*command_tasks, return_exceptions=True)
            await gateway_client.close()
            if llm_provider is not None:
                await llm_provider.aclose()
            await qq_http_client.aclose()
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


def _gateway_retry_delay_seconds(
    *,
    gateway_client: QQOfficialGatewayClient,
    configured_delay: float,
) -> float:
    gateway_info = gateway_client.gateway_info
    if gateway_info is None or gateway_info.session_start_limit is None:
        return configured_delay
    if gateway_info.session_start_limit.remaining != 0:
        return configured_delay
    reset_after = gateway_info.session_start_limit.reset_after
    if reset_after is None or reset_after <= 0:
        return configured_delay
    return max(configured_delay, reset_after / 1000)


app = create_app()


def _configure_logging(log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level)
    else:
        root.setLevel(level)


def main() -> None:
    settings = get_settings()
    _configure_logging(settings.log_level)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
