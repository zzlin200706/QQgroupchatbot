"""Manually verify QQ Official Gateway safely; no token or event data is printed."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.qq_official.auth import QQOfficialAuthClient, QQOfficialAuthError
from app.adapters.qq_official.gateway import QQOfficialGatewayClient, QQOfficialGatewayError
from app.config import get_settings


async def main() -> int:
    settings = get_settings()
    gateway: QQOfficialGatewayClient | None = None
    try:
        gateway = QQOfficialGatewayClient(
            auth_client=QQOfficialAuthClient(
                app_id=settings.qq_bot_app_id,
                app_secret=settings.qq_bot_app_secret,
            )
        )
        await gateway.connect()
        gateway_url = gateway.gateway_info.url if gateway.gateway_info is not None else ""
        environment = "sandbox" if "sandbox" in gateway_url else "production"
        print("QQ Official authentication succeeded", flush=True)
        print(f"Gateway URL acquired ({environment})", flush=True)
        print("WebSocket connected", flush=True)
        print("HELLO received", flush=True)
        print("IDENTIFY sent", flush=True)
        print("QQ Official Gateway READY", flush=True)
        print("Waiting for events...", flush=True)
        while True:
            event = await gateway.next_event()
            print(f"EVENT: {event.event_type or 'UNKNOWN'}", flush=True)
    except (QQOfficialAuthError, QQOfficialGatewayError) as error:
        print(f"QQ Official Gateway failed: {type(error).__name__}")
        return 1
    finally:
        if gateway is not None:
            await gateway.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("QQ Official Gateway test stopped")
