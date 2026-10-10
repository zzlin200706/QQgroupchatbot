import asyncio
from dataclasses import replace

import pytest

from app.services.image_enrichment import ImageEnrichmentService
from tests.unit.test_forward_display_images import URL, Resolver, Vision, forward, line, message


def source(url=URL, group="group-1"):
    original = message(forward(line(url)))
    return replace(original, context=replace(original.context, group_id=group))


async def test_quote_and_history_share_success_and_budget_within_request_only():
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision,
                                     max_forward_display_images=1)
    with service.request_scope(platform="qq_official", group_id="group-1"):
        await service.enrich_messages((source(),))
        result = await service.enrich_messages((source(URL.replace("test-secret", "new")),
                                               source(URL.replace("fixture", "other"))))
    assert "一张测试截图" in result[0].segments[0].content[0].text
    assert "超过本次" in result[1].segments[0].content[0].text
    assert len(resolver.calls) == len(vision.calls) == 1
    await service.enrich_messages((source(),))
    assert len(resolver.calls) == len(vision.calls) == 2


async def test_mixed_group_batch_never_reuses_results():
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    with service.request_scope(platform="qq_official", group_id="group-1"):
        await service.enrich_messages((source(), source(group="group-2")))
    assert len(resolver.calls) == len(vision.calls) == 2
    await service.enrich_messages((source(), source(group="group-2")))
    assert len(resolver.calls) == len(vision.calls) == 4


async def test_child_tasks_cannot_inherit_parent_cache_or_share_with_siblings():
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    with service.request_scope(platform="qq_official", group_id="group-1"):
        await service.enrich_messages((source(),))
        await asyncio.gather(service.enrich_messages((source(),)), service.enrich_messages((source(),)))
        await service.enrich_messages((source(),))
    assert len(resolver.calls) == len(vision.calls) == 3


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_scope_cleanup_on_failure_or_cancellation(error):
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    with pytest.raises(error):
        with service.request_scope(platform="qq_official", group_id="group-1"):
            await service.enrich_messages((source(),))
            raise error()
    await service.enrich_messages((source(),))
    assert len(vision.calls) == 2


async def test_nested_scope_restores_outer_cache():
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    with service.request_scope(platform="qq_official", group_id="group-1"):
        await service.enrich_messages((source(),))
        with service.request_scope(platform="qq_official", group_id="group-1"):
            await service.enrich_messages((source(),))
        await service.enrich_messages((source(),))
    assert len(vision.calls) == 2
