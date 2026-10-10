"""Offline feasibility probe for NapCat get_forward_msg exports; NOT a runtime adapter.

Reads one local JSON response, reports structural counts only, never connects,
downloads images, logs text/identities/URLs, or writes to the bot database.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


MAX_INPUT_BYTES = 4 * 1024 * 1024


def audit_response(payload: object, *, max_depth: int = 10, max_items: int = 500) -> dict:
    if max_depth < 1 or max_items < 1:
        raise ValueError("limits must be positive")
    counts: Counter = Counter()
    levels: dict[int, Counter] = {}
    remaining = max_items
    active: set[int] = set()

    def visit_messages(messages: object, depth: int) -> None:
        nonlocal remaining
        if depth > max_depth:
            counts["depth_limit"] += 1
            return
        if not isinstance(messages, list):
            counts["invalid_structure"] += 1
            return
        if id(messages) in active:
            counts["cycles"] += 1
            return
        active.add(id(messages))
        try:
            level = levels.setdefault(depth, Counter())
            for message in messages:
                if remaining == 0:
                    counts["item_limit"] += 1
                    return
                remaining -= 1
                if not isinstance(message, dict) or not isinstance(message.get("message"), list):
                    counts["invalid_structure"] += 1
                    continue
                level["messages"] += 1
                for segment in message["message"]:
                    if remaining == 0:
                        counts["item_limit"] += 1
                        return
                    remaining -= 1
                    if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
                        counts["invalid_structure"] += 1
                        continue
                    kind, data = segment.get("type"), segment["data"]
                    if kind == "text":
                        if isinstance(data.get("text"), str) and data["text"].strip():
                            level["text_segments"] += 1
                        else:
                            counts["invalid_structure"] += 1
                    elif kind == "image":
                        level["images"] += 1
                        url = data.get("url")
                        try:
                            parsed = urlsplit(url) if isinstance(url, str) else None
                            candidate = (parsed is not None and parsed.scheme in ("http", "https")
                                         and bool(parsed.hostname) and parsed.username is None)
                        except ValueError:
                            candidate = False
                        level["image_url_candidates" if candidate else "images_without_http_url"] += 1
                    elif kind == "forward":
                        level["forwards"] += 1
                        content = data.get("content")
                        if isinstance(content, list) and content:
                            visit_messages(content, depth + 1)
                        else:
                            counts["unexpanded_forwards"] += 1
                    else:
                        counts["other_segments"] += 1
        finally:
            active.remove(id(messages))

    if (not isinstance(payload, dict) or payload.get("status") != "ok"
            or type(payload.get("retcode")) is not int or payload["retcode"] != 0
            or not isinstance(payload.get("data"), dict)):
        counts["invalid_response"] += 1
    else:
        visit_messages(payload["data"].get("messages"), 1)
    if not any(value["messages"] for value in levels.values()):
        counts["no_messages"] += 1
    return {
        "scope": "offline_response_structure_only",
        "levels": {str(depth): dict(value) for depth, value in sorted(levels.items())},
        "findings": dict(counts),
        "has_nested_text": any(depth >= 2 and value["text_segments"] for depth, value in levels.items()),
        "has_nested_image_url_candidate": any(
            depth >= 2 and value["image_url_candidates"] for depth, value in levels.items()),
        "requires_review": bool(counts) or any(value["images_without_http_url"] for value in levels.values()),
        "live_fetch_verified": False,
        "image_download_verified": False,
        "vision_verified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("response", type=Path, help="local exported get_forward_msg JSON response")
    args = parser.parse_args()
    try:
        with args.response.open("rb") as source:
            body = source.read(MAX_INPUT_BYTES + 1)
        if len(body) > MAX_INPUT_BYTES:
            raise ValueError("input too large")
        report = audit_response(json.loads(body))
    except (OSError, ValueError, RecursionError):
        # Never echo a private path, JSON fragment, token, or exception body.
        parser.exit(2, "Input unavailable, oversized, or invalid JSON.\n")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if report["requires_review"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
