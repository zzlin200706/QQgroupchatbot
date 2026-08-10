"""Parsers from stored platform payloads into internal domain models."""

from app.parsers.onebot_message_parser import OneBotMessageParser
from app.parsers.qq_official_message_parser import QQOfficialMessageParser

__all__ = ["OneBotMessageParser", "QQOfficialMessageParser"]
