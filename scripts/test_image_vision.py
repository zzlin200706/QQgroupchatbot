"""Describe one local image with the configured vision provider."""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
from pathlib import Path

from app.config import get_settings
from app.domain.images import ResolvedImage
from app.llm.providers import create_vision_provider
from app.services.image_enrichment import IMAGE_DESCRIPTION_PROMPT


_SUPPORTED_TYPES = frozenset({"image/gif", "image/jpeg", "image/png"})


async def _run(path: Path) -> None:
    settings = get_settings()
    provider = create_vision_provider(settings)
    if provider is None:
        raise SystemExit("VISION_ENABLED must be true")
    try:
        content_type, _ = mimetypes.guess_type(path.name)
        if content_type not in _SUPPORTED_TYPES:
            raise SystemExit("local image must be JPEG, PNG, or GIF")
        data = path.read_bytes()
        if not data:
            raise SystemExit("local image is empty")
        if len(data) > settings.image_max_bytes:
            raise SystemExit("local image exceeds IMAGE_MAX_BYTES")
        result = await provider.describe_image(
            ResolvedImage(data=data, content_type=content_type),
            prompt=IMAGE_DESCRIPTION_PROMPT,
        )
        print(result.description)
    finally:
        await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.image))


if __name__ == "__main__":
    main()
