"""Adapter components for the QQ Official Bot API."""

from app.adapters.qq_official.auth import (
    QQAccessToken,
    QQOfficialAuthClient,
    QQOfficialAuthConfigurationError,
    QQOfficialAuthError,
    QQOfficialAuthHTTPError,
    QQOfficialAuthResponseError,
    QQOfficialAuthTransportError,
)
from app.adapters.qq_official.gateway import (
    GROUP_AND_C2C_EVENT_INTENT,
    QQGatewayDispatch,
    QQGatewayInfo,
    QQGatewayReady,
    QQOfficialGatewayClient,
    QQOfficialGatewayError,
    QQOfficialGatewayHTTPError,
    QQOfficialGatewayProtocolError,
    QQOfficialGatewayResponseError,
    QQOfficialGatewayTransportError,
)

__all__ = [
    "QQAccessToken",
    "QQOfficialAuthClient",
    "QQOfficialAuthConfigurationError",
    "QQOfficialAuthError",
    "QQOfficialAuthHTTPError",
    "QQOfficialAuthResponseError",
    "QQOfficialAuthTransportError",
    "GROUP_AND_C2C_EVENT_INTENT",
    "QQGatewayDispatch",
    "QQGatewayInfo",
    "QQGatewayReady",
    "QQOfficialGatewayClient",
    "QQOfficialGatewayError",
    "QQOfficialGatewayHTTPError",
    "QQOfficialGatewayProtocolError",
    "QQOfficialGatewayResponseError",
    "QQOfficialGatewayTransportError",
]
