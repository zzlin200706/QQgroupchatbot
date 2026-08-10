"""Capture one labeled, redacted QQ Official group-message Gateway sample."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.qq_official.auth import QQOfficialAuthClient, QQOfficialAuthError
from app.adapters.qq_official.gateway import QQGatewayDispatch, QQOfficialGatewayClient, QQOfficialGatewayError
from app.adapters.qq_official.redaction import (
    dispatch_summary,
    duplicate_key,
    is_duplicate_candidate,
    redact_gateway_dispatch,
)
from app.config import get_settings


_TARGET_EVENTS = frozenset({"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"})
_SAMPLES_DIRECTORY = Path("data/qq_official_samples")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Human test-case label, for example text")
    return parser.parse_args()


async def main(label: str) -> int:
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
        print(f"Waiting for test case: {label}", flush=True)
        while True:
            dispatch = await gateway.next_event()
            if dispatch.event_type in _TARGET_EVENTS:
                path, summary = save_sample(label, dispatch)
                print(f"Captured: {path.name}")
                print(f"event_type={summary['event_type']}")
                print(f"message_type={summary['message_type']}")
                print(f"author_present={summary['author_present']}")
                print(f"msg_elements={summary['msg_elements']}")
                print(f"max_nested_depth={summary['max_nested_depth']}")
                return 0
            print(f"Ignored EVENT: {dispatch.event_type or 'UNKNOWN'}", flush=True)
    except (QQOfficialAuthError, QQOfficialGatewayError) as error:
        print(f"QQ Official capture failed: {type(error).__name__}")
        return 1
    finally:
        if gateway is not None:
            await gateway.close()


def save_sample(label: str, dispatch: QQGatewayDispatch) -> tuple[Path, dict[str, object]]:
    """Persist one redacted capture and append local-only manifest metadata."""

    directory = _SAMPLES_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = _load_manifest(manifest_path)
    filename = f"{len(manifest) + 1:03d}_{_safe_label(label)}.json"
    captured_at = datetime.now(timezone.utc).isoformat()
    summary = dispatch_summary(dispatch)
    sequence, message_id = duplicate_key(dispatch)
    duplicate_candidate = is_duplicate_candidate(manifest, dispatch)
    sample_path = directory / filename
    sample_path.write_text(
        json.dumps(redact_gateway_dispatch(dispatch, captured_at=captured_at), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    manifest.append(
        {
            "label": label,
            "file": filename,
            "event_type": summary["event_type"],
            "message_type": summary["message_type"],
            "captured_at": captured_at,
            "gateway_sequence": sequence,
            "message_id": message_id,
            "duplicate_candidate": duplicate_candidate,
            "captured": True,
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sample_path, summary


def _load_manifest(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, list) else []


def _safe_label(label: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_")
    if not normalized:
        raise ValueError("label must contain at least one letter, number, underscore, or hyphen")
    return normalized


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parse_args().label)))
    except KeyboardInterrupt:
        print("QQ Official capture stopped")
