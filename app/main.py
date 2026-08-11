"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.adapters.qq_official import (
    QQOfficialAuthClient,
    QQOfficialAuthError,
    QQOfficialGatewayClient,
    QQOfficialGatewayError,
    QQOfficialGroupMessageSender,
    QQOfficialWebhookAdapter,
    QQOfficialWebhookPayloadError,
    QQOfficialWebhookSignatureError,
    inbound_event_from_gateway_dispatch,
)
from app.config import Settings, get_settings
from app.llm.providers import create_llm_provider
from app.parsers.qq_official_message_parser import QQOfficialMessageParser
from app.rendering import MessageRenderer, SummaryMessageFormatter
from app.services.command_dispatch import QQOfficialCommandDispatcher
from app.services.conversation_query import ConversationQueryService
from app.services.group_assistant import GroupAssistantService
from app.services.group_assistant_context import GroupAssistantContextBuilder
from app.services.group_assistant_handler import GroupAssistantHandler
from app.services.interaction_dispatch import QQOfficialInteractionDispatcher
from app.services.normalized_message_ingestion import (
    QQOfficialNormalizedMessageIngestionService,
)
from app.services.ping_command import PingCommandHandler
from app.services.qq_official_event_processor import QQOfficialEventProcessor
from app.services.raw_event_ingestion import QQOfficialRawEventIngestionService
from app.services.summary import SummaryService
from app.services.summary_command import SummaryCommandHandler
from app.storage.database import Database
from app.storage.assistant_interaction_repository import AssistantInteractionRepository
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
        assistant_interaction_repository = AssistantInteractionRepository(
            database.session_factory
        )
        auth_client = QQOfficialAuthClient(
            app_id=app_settings.qq_bot_app_id,
            app_secret=app_settings.qq_bot_app_secret,
            timeout_seconds=app_settings.qq_api_timeout_seconds,
            client=qq_http_client,
        )
        group_message_sender = QQOfficialGroupMessageSender(
            auth_client=auth_client,
            timeout_seconds=app_settings.qq_api_timeout_seconds,
            http_client=qq_http_client,
        )
        ping_command_handler = PingCommandHandler(sender=group_message_sender)
        summary_command_handler: SummaryCommandHandler | None = None
        assistant_handler: GroupAssistantHandler | None = None
        command_dispatcher = QQOfficialCommandDispatcher(
            ping_handler=ping_command_handler
        )
        llm_provider = None
        gateway_stop = asyncio.Event()
        gateway_client: QQOfficialGatewayClient | None = None
        gateway_task: asyncio.Task[None] | None = None
        webhook_adapter: QQOfficialWebhookAdapter | None = None

        async def handle_dispatch_loop() -> None:
            assert gateway_client is not None
            retry_delay = app_settings.qq_gateway_reconnect_initial_delay_seconds
            while not gateway_stop.is_set():
                try:
                    await gateway_client.connect()
                    logger.info("qq official gateway connected")
                    retry_delay = app_settings.qq_gateway_reconnect_initial_delay_seconds
                    while not gateway_stop.is_set():
                        dispatch = await gateway_client.next_event()
                        result = await event_processor.process(
                            inbound_event_from_gateway_dispatch(dispatch)
                        )
                        if not result.raw_persisted:
                            logger.error(
                                "qq official inbound event processing failed transport=%s event_type=%s stage=raw_persistence",
                                result.transport,
                                result.event_type,
                            )
                            continue
                        logger.info(
                            "qq official inbound event processed transport=%s event_type=%s raw_event_id=%s normalized=%s interaction=%s",
                            result.transport,
                            result.event_type,
                            result.raw_event_id,
                            result.normalized_persisted,
                            result.interaction_name,
                        )
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

        if (
            app_settings.summary_command_enabled
            or app_settings.group_assistant_enabled
        ):
            llm_provider = create_llm_provider(app_settings)

        conversation_query_service = ConversationQueryService(message_repository)
        if app_settings.summary_command_enabled:
            assert llm_provider is not None
            summary_service = SummaryService(
                query_service=conversation_query_service,
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

        if app_settings.group_assistant_enabled:
            assert llm_provider is not None
            assistant_handler = GroupAssistantHandler(
                service=GroupAssistantService(
                    context_builder=GroupAssistantContextBuilder(
                        query_service=conversation_query_service,
                        interaction_repository=assistant_interaction_repository,
                        renderer=MessageRenderer(),
                        qa_lookback_minutes=app_settings.qa_lookback_minutes,
                        qa_max_messages=app_settings.qa_max_messages,
                        chat_lookback_minutes=app_settings.chat_lookback_minutes,
                        chat_max_messages=app_settings.chat_max_messages,
                        chat_max_assistant_turns=(
                            app_settings.chat_max_assistant_turns
                        ),
                        max_input_chars=app_settings.assistant_max_input_chars,
                    ),
                    provider=llm_provider,
                    max_output_tokens=app_settings.assistant_max_output_tokens,
                    max_output_chars=app_settings.assistant_max_output_chars,
                ),
                repository=assistant_interaction_repository,
                sender=group_message_sender,
                enabled=True,
                cooldown_seconds=app_settings.assistant_cooldown_seconds,
            )

        interaction_dispatcher = QQOfficialInteractionDispatcher(
            command_dispatcher=command_dispatcher,
            assistant_handler=assistant_handler,
        )

        event_processor = QQOfficialEventProcessor(
            raw_ingestion_service=raw_ingestion_service,
            normalized_ingestion_service=normalized_ingestion_service,
            interaction_dispatcher=interaction_dispatcher,
        )
        if app_settings.qq_event_transport == "websocket":
            gateway_client = QQOfficialGatewayClient(
                auth_client=auth_client,
                timeout_seconds=app_settings.qq_api_timeout_seconds,
                http_client=qq_http_client,
            )
            gateway_task = asyncio.create_task(
                handle_dispatch_loop(),
                name="qq-official-gateway-loop",
            )
        else:
            webhook_adapter = QQOfficialWebhookAdapter(
                bot_secret=app_settings.qq_bot_app_secret,
                app_id=app_settings.qq_bot_app_id,
            )

        application.state.database = database
        application.state.qq_http_client = qq_http_client
        application.state.qq_auth_client = auth_client
        application.state.qq_gateway_client = gateway_client
        application.state.qq_gateway_task = gateway_task
        application.state.qq_webhook_adapter = webhook_adapter
        application.state.qq_event_processor = event_processor
        application.state.qq_event_transport = app_settings.qq_event_transport
        application.state.qq_group_message_sender = group_message_sender
        application.state.llm_provider = llm_provider
        application.state.qq_command_dispatcher = command_dispatcher
        application.state.qq_interaction_dispatcher = interaction_dispatcher
        application.state.group_assistant_handler = assistant_handler
        application.state.assistant_interaction_repository = (
            assistant_interaction_repository
        )
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
            if gateway_task is not None:
                gateway_task.cancel()
                await asyncio.gather(gateway_task, return_exceptions=True)
            if gateway_client is not None:
                await gateway_client.close()
            await event_processor.aclose()
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

    @application.post("/qq-official/webhook")
    async def qq_official_webhook(request: Request) -> Response:
        if app_settings.qq_event_transport != "webhook":
            raise HTTPException(status_code=404, detail="webhook transport disabled")

        adapter = application.state.qq_webhook_adapter
        processor = application.state.qq_event_processor
        body = await request.body()
        try:
            parsed = adapter.parse_request(headers=request.headers, body=body)
        except QQOfficialWebhookSignatureError as error:
            logger.warning(
                "qq official webhook rejected error_type=%s",
                type(error).__name__,
            )
            raise HTTPException(status_code=401, detail="invalid webhook signature") from error
        except QQOfficialWebhookPayloadError as error:
            logger.warning(
                "qq official webhook payload invalid error_type=%s",
                type(error).__name__,
            )
            raise HTTPException(status_code=400, detail="invalid webhook payload") from error

        if parsed.validation_response is not None:
            return JSONResponse(
                {
                    "plain_token": parsed.validation_response.plain_token,
                    "signature": parsed.validation_response.signature,
                }
            )

        assert parsed.event is not None
        result = await processor.process(parsed.event)
        if not result.raw_persisted:
            logger.error(
                "qq official webhook processing failed event_type=%s stage=raw_persistence",
                result.event_type,
            )
            raise HTTPException(status_code=500, detail="webhook event persistence failed")
        return JSONResponse(adapter.ack_payload())

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
