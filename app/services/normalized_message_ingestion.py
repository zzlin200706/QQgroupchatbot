"""Safe raw-receipt to normalized-message pipeline for live OneBot events."""

from __future__ import annotations

import logging

from app.domain.messages.models import InternalMessage
from app.parsers.onebot_message_parser import OneBotMessageParser
from app.storage.message_repository import MessageRepository
from app.storage.models import RawEvent


logger = logging.getLogger(__name__)

ONEBOT_PARSER_NAME = "onebot_message_parser"
ONEBOT_PARSER_VERSION = "1"


class NormalizedMessageIngestionService:
    """Pure-parse a committed raw receipt and persist its complete tree."""

    def __init__(
        self,
        *,
        repository: MessageRepository,
        parser: OneBotMessageParser,
    ) -> None:
        self._repository = repository
        self._parser = parser

    async def ingest(self, raw_event: RawEvent) -> InternalMessage | None:
        """Return the normalized message, or safely retain only the raw receipt."""

        try:
            message = self._parser.parse(
                raw_event.raw_payload,
                source_raw_event_id=raw_event.id,
            )
            if message is None:
                return None
            return await self._repository.persist(
                message,
                parser_name=ONEBOT_PARSER_NAME,
                parser_version=ONEBOT_PARSER_VERSION,
            )
        except Exception as error:
            logger.error(
                "normalized message persistence failed raw_event_id=%s platform=%s parser_name=%s error_type=%s",
                raw_event.id,
                raw_event.platform,
                ONEBOT_PARSER_NAME,
                type(error).__name__,
            )
            return None
