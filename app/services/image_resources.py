"""Safe, bounded resolution of untrusted image attachment URLs."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from app.domain.images import ResolvedImage
from app.domain.messages import ImageSegment


_SUPPORTED_CONTENT_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png"}
)
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class ImageResolveError(Exception):
    """Base class for safe image resolution failures."""


class InvalidImageUrlError(ImageResolveError):
    """The attachment URL is absent or unsafe to fetch."""


class ImageTooLargeError(ImageResolveError):
    """The remote image exceeds the configured in-memory bound."""


class UnsupportedImageError(ImageResolveError):
    """The remote content is not a supported image type."""


class ImageResourceResolver(Protocol):
    async def resolve(self, image: ImageSegment) -> ResolvedImage:
        """Resolve one image without changing its source segment."""


AddressResolver = Callable[[str], Awaitable[Sequence[str]]]


class HttpImageResourceResolver:
    """Fetch public HTTP(S) images with redirect and size safeguards."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        client: httpx.AsyncClient | None = None,
        address_resolver: AddressResolver | None = None,
        max_redirects: int = 3,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least one")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._address_resolver = address_resolver or _resolve_hostname
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False)

    async def resolve(self, image: ImageSegment) -> ResolvedImage:
        current_url = image.url
        if not current_url:
            raise InvalidImageUrlError("image URL is unavailable")

        for redirect_count in range(self._max_redirects + 1):
            await self._validate_url(current_url)
            try:
                async with self._client.stream(
                    "GET",
                    current_url,
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_STATUS_CODES:
                        if redirect_count >= self._max_redirects:
                            raise ImageResolveError("image redirect limit exceeded")
                        location = response.headers.get("Location")
                        if not location:
                            raise ImageResolveError(
                                "image redirect response omitted Location"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ImageResolveError(
                            f"image download failed with HTTP {response.status_code}"
                        )

                    declared_size = _content_length(response.headers)
                    if declared_size is not None and declared_size > self._max_bytes:
                        raise ImageTooLargeError(
                            "image Content-Length exceeds configured maximum"
                        )
                    content_type = _content_type(
                        response.headers.get("Content-Type"),
                        fallback=image.content_type,
                    )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self._max_bytes:
                            raise ImageTooLargeError(
                                "streamed image exceeds configured maximum"
                            )
                        body.extend(chunk)
                    if not body:
                        raise ImageResolveError("image response body was empty")
                    return ResolvedImage(
                        data=bytes(body),
                        content_type=content_type,
                    )
            except httpx.TimeoutException as error:
                raise ImageResolveError("image download timed out") from error
            except httpx.RequestError as error:
                raise ImageResolveError("image download transport failed") from error

        raise AssertionError("bounded image redirect loop exited unexpectedly")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _validate_url(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise InvalidImageUrlError("image URL is malformed") from error
        if parsed.scheme.lower() not in {"http", "https"}:
            raise InvalidImageUrlError("image URL scheme is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidImageUrlError("image URL must not contain credentials")
        hostname = parsed.hostname
        if not hostname:
            raise InvalidImageUrlError("image URL hostname is unavailable")
        if port is not None and port < 1:
            raise InvalidImageUrlError("image URL port is invalid")
        normalized_host = hostname.rstrip(".").lower()
        if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
            raise InvalidImageUrlError("image URL hostname is not public")

        try:
            literal_address = ipaddress.ip_address(normalized_host)
        except ValueError:
            try:
                addresses = await asyncio.wait_for(
                    self._address_resolver(normalized_host),
                    timeout=self._timeout_seconds,
                )
            except (OSError, asyncio.TimeoutError) as error:
                raise InvalidImageUrlError(
                    "image URL hostname could not be resolved safely"
                ) from error
            if not addresses:
                raise InvalidImageUrlError("image URL hostname has no addresses")
            if any(not _public_address(address) for address in addresses):
                raise InvalidImageUrlError("image URL resolved to a non-public address")
        else:
            if not literal_address.is_global:
                raise InvalidImageUrlError("image URL address is not public")


def _content_length(headers: httpx.Headers) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _content_type(value: str | None, *, fallback: str | None) -> str:
    candidate = value.split(";", 1)[0].strip().lower() if value else fallback
    normalized = candidate.strip().lower() if candidate else None
    if normalized not in _SUPPORTED_CONTENT_TYPES:
        raise UnsupportedImageError("image content type is unsupported")
    return normalized


def _public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _resolve_hostname(hostname: str) -> Sequence[str]:
    def lookup() -> tuple[str, ...]:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(record[4][0] for record in records))

    return await asyncio.to_thread(lookup)
