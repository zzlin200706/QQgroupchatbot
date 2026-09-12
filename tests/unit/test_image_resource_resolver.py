from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from app.domain.messages import ImageSegment
from app.services.image_resources import (
    HttpImageResourceResolver,
    ImageResolveError,
    ImageTooLargeError,
    InvalidImageUrlError,
    UnsupportedImageError,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def image(url: str = "https://media.example.test/image") -> ImageSegment:
    return ImageSegment(
        position=0,
        raw_data={"url": "not-logged"},
        file="image.png",
        url=url,
        summary=None,
        sub_type=None,
        file_size=None,
        content_type="image/png",
    )


async def public_addresses(hostname: str) -> tuple[str, ...]:
    assert hostname == "media.example.test"
    return ("93.184.216.34",)


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body"),
    [("image/jpeg", b"jpeg-bytes"), ("image/png", b"png-bytes")],
)
async def test_resolves_supported_images(content_type: str, body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "media.example.test"
        return httpx.Response(200, headers={"Content-Type": content_type}, content=body)

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        resolved = await resolver.resolve(image())

    assert resolved.data == body
    assert resolved.content_type == content_type


@pytest.mark.asyncio
async def test_timeout_is_mapped_to_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail", request=request)

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(ImageResolveError, match="timed out") as caught:
            await resolver.resolve(image("https://media.example.test/image?secret=value"))

    assert "private" not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_http_failure_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"private response")

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(ImageResolveError, match="HTTP 404") as caught:
            await resolver.resolve(image())

    assert "private response" not in str(caught.value)


@pytest.mark.asyncio
async def test_content_length_rejects_before_reading_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png", "Content-Length": "101"},
            content=b"small",
        )

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(ImageTooLargeError, match="Content-Length"):
            await resolver.resolve(image())


@pytest.mark.asyncio
async def test_streamed_body_enforces_maximum_without_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png"},
            stream=ChunkStream(b"123", b"456"),
        )

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=5,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(ImageTooLargeError, match="streamed"):
            await resolver.resolve(image())


@pytest.mark.asyncio
async def test_empty_body_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"")

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(ImageResolveError, match="empty"):
            await resolver.resolve(image())


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///tmp/image.png", "ftp://example.test/x", "data:image/png;base64,eA=="])
async def test_non_http_schemes_are_rejected(url: str) -> None:
    resolver = HttpImageResourceResolver(timeout_seconds=5, max_bytes=100)
    try:
        with pytest.raises(InvalidImageUrlError, match="scheme"):
            await resolver.resolve(image(url))
    finally:
        await resolver.aclose()


@pytest.mark.asyncio
async def test_private_network_destination_is_rejected() -> None:
    async def private_addresses(hostname: str) -> tuple[str, ...]:
        return ("127.0.0.1",)

    resolver = HttpImageResourceResolver(
        timeout_seconds=5,
        max_bytes=100,
        address_resolver=private_addresses,
    )
    try:
        with pytest.raises(InvalidImageUrlError, match="non-public"):
            await resolver.resolve(image("https://internal.example.test/image"))
    finally:
        await resolver.aclose()


@pytest.mark.asyncio
async def test_unsupported_content_type_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"html")

    async with client(handler) as http_client:
        resolver = HttpImageResourceResolver(
            timeout_seconds=5,
            max_bytes=100,
            client=http_client,
            address_resolver=public_addresses,
        )
        with pytest.raises(UnsupportedImageError, match="unsupported"):
            await resolver.resolve(image())
