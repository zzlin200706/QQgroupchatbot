"""Preview or append new parser versions of stored QQ forwards, offline.

Run from the project root:
    .venv/bin/python -m scripts.reparse_qq_official_forwards
    .venv/bin/python -m scripts.reparse_qq_official_forwards --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings
from app.parsers import QQOfficialMessageParser
from app.services.forward_reparse import reparse_forwards
from app.storage.database import Database


async def run(*, apply: bool, raw_event_id: int | None) -> None:
    # An offline repair must not require working provider credentials.
    settings = Settings(
        summary_command_enabled=False, group_assistant_enabled=False,
        vision_enabled=False,
    )
    url = make_url(settings.database_url)
    if (
        url.drivername != "sqlite+aiosqlite"
        or not url.database
        or not Path(url.database).is_file()
    ):
        raise SystemExit("An existing local SQLite database is required")
    database = Database(settings.database_url)
    try:
        result = await reparse_forwards(
            database.session_factory,
            parser=QQOfficialMessageParser(
                max_forward_depth=settings.forward_max_depth,
                max_forward_nodes=settings.forward_max_nodes,
            ),
            apply=apply, raw_event_id=raw_event_id,
        )
        print(json.dumps({"mode": "apply" if apply else "preview", **asdict(result)}))
        if result.failed:
            raise SystemExit(1)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="append new normalized versions")
    parser.add_argument("--raw-event-id", type=int, help="limit to a single raw receipt")
    args = parser.parse_args()
    if args.raw_event_id is not None and args.raw_event_id < 1:
        parser.error("--raw-event-id must be positive")
    asyncio.run(run(apply=args.apply, raw_event_id=args.raw_event_id))


if __name__ == "__main__":
    main()
