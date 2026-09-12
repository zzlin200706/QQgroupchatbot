from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.images import ImageDescription, ResolvedImage
from app.domain.messages import (
    ForwardNode,
    ForwardResolutionStatus,
    ForwardSegment,
    IdentityAvailability,
    IdentityRef,
    IdentitySource,
    ImageSegment,
    InternalMessage,
    MessageContext,
    MessageProvenance,
    ProvenanceSource,
    TextSegment,
)
from app.services.image_enrichment import (
    IMAGE_UNAVAILABLE_DESCRIPTION,
    ImageEnrichmentService,
)
from app.services.image_resources import ImageResolveError


NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def identity(
    name: str | None,
    user_id: str | None,
    *,
    source: IdentitySource = IdentitySource.EVENT,
) -> IdentityRef:
    return IdentityRef(
        platform="qq_official",
        user_id=user_id,
        display_name=name,
        card=None,
        source=source,
        availability=(
            IdentityAvailability.KNOWN
            if user_id is not None
            else IdentityAvailability.UNAVAILABLE
        ),
    )


ALICE = identity("Alice", "alice")


def image(position: int, name: str) -> ImageSegment:
    return ImageSegment(
        position=position,
        raw_data={"filename": name, "source": "qq"},
        file=name,
        url=f"https://media.example.test/{name}",
        summary=None,
        sub_type=None,
        file_size=10,
        content_type="image/png",
        width=100,
        height=50,
    )


def message(*segments, author: IdentityRef = ALICE) -> InternalMessage:
    return InternalMessage(
        platform="qq_official",
        source_raw_event_id=1,
        platform_message_id="message-1",
        context=MessageContext(
            message_type="0",
            sub_type="GROUP_MESSAGE_CREATE",
            group_id="group-1",
        ),
        actor=ALICE,
        author=author,
        timestamp=NOW,
        segments=tuple(segments),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.DIRECT_EVENT,
            raw_event_id=1,
        ),
    )


class FakeResolver:
    def __init__(self, *, failing_files: set[str] | None = None) -> None:
        self.failing_files = failing_files or set()
        self.calls: list[ImageSegment] = []

    async def resolve(self, value: ImageSegment) -> ResolvedImage:
        self.calls.append(value)
        if value.file in self.failing_files:
            raise ImageResolveError("safe test failure")
        return ResolvedImage(
            data=(value.file or "unknown").encode(),
            content_type="image/png",
        )


class FakeVisionProvider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[ResolvedImage, str]] = []

    @property
    def supports_vision(self) -> bool:
        return True

    async def describe_image(
        self,
        value: ResolvedImage,
        *,
        prompt: str,
    ) -> ImageDescription:
        self.calls.append((value, prompt))
        if self.fail:
            raise RuntimeError("provider failed")
        return ImageDescription(description=f"description:{value.data.decode()}")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_successfully_enriches_image_without_mutating_source_or_sender() -> None:
    source_image = image(1, "screen.png")
    source = message(
        TextSegment(position=0, raw_data="help", text="help"),
        source_image,
    )
    resolver = FakeResolver()
    provider = FakeVisionProvider()
    service = ImageEnrichmentService(
        enabled=True,
        resolver=resolver,
        provider=provider,
    )

    result = (await service.enrich_messages((source,)))[0]

    assert result is not source
    assert result.actor is source.actor
    assert result.author is source.author
    assert result.provenance is source.provenance
    assert result.segments[0] is source.segments[0]
    enriched_image = result.segments[1]
    assert isinstance(enriched_image, ImageSegment)
    assert enriched_image.description == "description:screen.png"
    assert enriched_image.raw_data == source_image.raw_data
    assert source_image.description is None
    assert len(resolver.calls) == len(provider.calls) == 1
    assert "sender" not in provider.calls[0][1].lower()


@pytest.mark.asyncio
async def test_one_failed_image_does_not_destroy_other_images_or_text_order() -> None:
    source = message(
        TextSegment(position=0, raw_data="before", text="before"),
        image(1, "broken.png"),
        image(2, "working.png"),
        TextSegment(position=3, raw_data="after", text="after"),
    )
    service = ImageEnrichmentService(
        enabled=True,
        resolver=FakeResolver(failing_files={"broken.png"}),
        provider=FakeVisionProvider(),
    )

    result = (await service.enrich_messages((source,)))[0]

    assert [segment.position for segment in result.segments] == [0, 1, 2, 3]
    assert result.segments[0] is source.segments[0]
    assert result.segments[3] is source.segments[3]
    assert isinstance(result.segments[1], ImageSegment)
    assert result.segments[1].description == IMAGE_UNAVAILABLE_DESCRIPTION
    assert isinstance(result.segments[2], ImageSegment)
    assert result.segments[2].description == "description:working.png"


@pytest.mark.asyncio
async def test_provider_failure_degrades_only_the_image() -> None:
    source = message(image(0, "screen.png"))
    service = ImageEnrichmentService(
        enabled=True,
        resolver=FakeResolver(),
        provider=FakeVisionProvider(fail=True),
    )

    result = (await service.enrich_messages((source,)))[0]

    assert isinstance(result.segments[0], ImageSegment)
    assert result.segments[0].description == IMAGE_UNAVAILABLE_DESCRIPTION


@pytest.mark.asyncio
async def test_text_only_and_disabled_service_do_not_call_dependencies() -> None:
    text = message(TextSegment(position=0, raw_data="hello", text="hello"))
    image_message = message(image(0, "screen.png"))
    resolver = FakeResolver()
    provider = FakeVisionProvider()
    enabled = ImageEnrichmentService(
        enabled=True,
        resolver=resolver,
        provider=provider,
    )
    disabled = ImageEnrichmentService(enabled=False)

    enabled_result = await enabled.enrich_messages((text,))
    disabled_result = await disabled.enrich_messages((image_message,))

    assert enabled_result == (text,)
    assert disabled_result == (image_message,)
    assert resolver.calls == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_forward_and_nested_forward_identity_and_provenance_remain_layer_local() -> None:
    outer_sender = identity("Forwarder", "forwarder")
    node_sender = identity(
        "Node Author",
        "node-author",
        source=IdentitySource.FORWARD_NODE,
    )
    unavailable = identity(None, None, source=IdentitySource.UNKNOWN)
    nested_node = ForwardNode(
        sender=unavailable,
        timestamp=NOW,
        content=(image(0, "nested.png"),),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.NESTED_FORWARD_NODE,
            raw_event_id=1,
            forward_depth=2,
        ),
        raw_data={"nested": True},
    )
    nested_forward = ForwardSegment(
        position=1,
        raw_data={"forward": "nested"},
        reference_id="nested",
        resolved=True,
        resolution_status=ForwardResolutionStatus.EMBEDDED,
        content=(),
        nodes=(nested_node,),
    )
    outer_node = ForwardNode(
        sender=node_sender,
        timestamp=NOW,
        content=(image(0, "node.png"), nested_forward),
        provenance=MessageProvenance(
            source_type=ProvenanceSource.FORWARD_NODE,
            raw_event_id=1,
            forward_depth=1,
        ),
        raw_data={"node": True},
    )
    source = message(
        ForwardSegment(
            position=0,
            raw_data={"forward": "outer"},
            reference_id="outer",
            resolved=True,
            resolution_status=ForwardResolutionStatus.EMBEDDED,
            content=(),
            nodes=(outer_node,),
        ),
        author=outer_sender,
    )
    service = ImageEnrichmentService(
        enabled=True,
        resolver=FakeResolver(),
        provider=FakeVisionProvider(),
    )

    result = (await service.enrich_messages((source,)))[0]

    assert result.author == outer_sender
    outer = result.segments[0]
    assert isinstance(outer, ForwardSegment)
    assert outer.nodes[0].sender == node_sender
    assert outer.nodes[0].provenance is outer_node.provenance
    nested = outer.nodes[0].content[1]
    assert isinstance(nested, ForwardSegment)
    assert nested.nodes[0].sender == unavailable
    assert nested.nodes[0].sender.availability is IdentityAvailability.UNAVAILABLE
    assert nested.nodes[0].provenance is nested_node.provenance
    assert isinstance(nested.nodes[0].content[0], ImageSegment)
    assert nested.nodes[0].content[0].description == "description:nested.png"
