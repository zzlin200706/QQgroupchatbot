"""Manually verify local QQ Official Bot credentials without exposing tokens."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.qq_official.auth import QQOfficialAuthClient, QQOfficialAuthError
from app.config import get_settings


async def main() -> int:
    settings = get_settings()
    try:
        client = QQOfficialAuthClient(
            app_id=settings.qq_bot_app_id,
            app_secret=settings.qq_bot_app_secret,
        )
        access_token = await client.fetch_access_token()
    except QQOfficialAuthError as error:
        print(f"QQ Official authentication failed: {type(error).__name__}")
        return 1

    print("QQ Official authentication succeeded")
    print(f"expires_in={access_token.expires_in}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
