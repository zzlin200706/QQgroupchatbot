import logging
from io import BytesIO
from PIL import Image

import httpx
import pytest

from app.domain.images import ImageDescription, ResolvedImage
from app.domain.messages import ForwardSegment, ForwardResolutionStatus, TextSegment, ReplySegment
from app.domain.messages.segments import ResolvedMessageReference
from app.domain.messages.forward_display import explicit_image_url, redact_media_urls
from app.log_redaction import SecretRedactionFilter
from app.rendering.message_renderer import MessageRenderer
from app.services.image_enrichment import ImageEnrichmentService
from app.services.image_resources import HttpImageResourceResolver
from tests.unit.test_image_enrichment_service import message, ALICE


URL = "https://multimedia.nt.qq.com.cn/download?appid=1&fileid=fixture&rkey=test-secret&spec=0"
def image_bytes(format="PNG", color="red"):
    output = BytesIO()
    with Image.new("RGB", (10, 20), color) as image:
        image.save(output, format=format)
    return output.getvalue()


PNG = image_bytes()


def line(url=URL):
    return f"[附件1] 类型:图片 文件名:test.png 尺寸:10x20 大小:1.2KB URL:{url}"


def forward(text):
    return ForwardSegment(position=0, raw_data={"original": text}, reference_id=None,
                          resolved=False, resolution_status=ForwardResolutionStatus.UNRESOLVED,
                          content=(TextSegment(position=0, raw_data=text, text=text),), nodes=())


class Resolver:
    def __init__(self, data=PNG):
        self.calls = []
        self.data = data

    async def resolve(self, image):
        self.calls.append(image.url)
        return ResolvedImage(data=self.data, content_type="image/png")


class Vision:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def describe_image(self, image, *, prompt):
        self.calls.append(image)
        if self.fail:
            raise RuntimeError("failure")
        return ImageDescription(description="一张测试截图")


@pytest.mark.parametrize("url", [
    "https://example.com/image.png", "https://127.0.0.1/a.png",
    URL.replace("https:", "http:"), URL.replace(".cn/", ".cn.evil.test/"),
    URL.replace("https://", "https://user@"), URL.replace("/download?", "/other?"),
    URL + "#fragment", URL.replace(".cn/", ".cn:444/"),
    "https://multimedia.nt.qq.com.cn/download?fileid=1",
])
def test_only_observed_public_qq_image_endpoint_is_eligible(url):
    assert explicit_image_url(line(url)) is None


@pytest.mark.parametrize("text", [URL, "普通聊天 " + URL, line().replace("类型:图片", "类型:文件"),
                                  "[消息内容] " + line(), line() + " extra text"])
def test_arbitrary_urls_and_non_image_fields_are_not_candidates(text):
    assert explicit_image_url(text) is None


async def test_display_images_are_local_to_attachment_preserve_source_and_deduplicate():
    original = "[群聊的聊天记录]\n=== 消息 1 ===\n" + line() + "\n=== 消息 2 ===\n" + line()
    source = message(forward(original))
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    result = (await service.enrich_messages((source, source)))[0]
    text = result.segments[0].content[0].text
    assert text.index("一张测试截图") < text.index("=== 消息 2 ===")
    assert text.count("一张测试截图") == 2
    assert "test-secret" not in text
    assert source.segments[0].content[0].text == original
    assert result.segments[0].raw_data == source.segments[0].raw_data
    assert result.author is source.author
    assert not result.segments[0].resolved and not result.segments[0].nodes
    assert resolver.calls == [URL] and len(vision.calls) == 1
    assert (await service.enrich_messages((result,)))[0] is result
    assert len(resolver.calls) == 1


async def test_actual_quoted_display_shape_and_ordinary_text_do_not_share_permissions():
    text = "=== 消息 1 ===\n[消息内容] [群聊的聊天记录]\n[消息类型] 引用消息\n    " + line()
    segment = TextSegment(position=0, raw_data=text, text=text)
    reply = ReplySegment(position=0, raw_data={}, referenced_message_id=None,
                         resolved_message=ResolvedMessageReference(None, ALICE, None, (segment,), {}))
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    unchanged = message(segment)
    assert (await service.enrich_messages((unchanged,)))[0] is unchanged
    result = (await service.enrich_messages((message(reply),)))[0]
    assert "一张测试截图" in result.segments[0].resolved_message.segments[0].text
    assert resolver.calls == [URL]
    assert "\n    [该附件的图片描述" in result.segments[0].resolved_message.segments[0].text


async def test_budget_and_bad_bytes_never_reach_vision():
    source = message(forward(line() + "\n" + line(URL.replace("fixture", "second"))))
    resolver, vision = Resolver(b"<html>not an image</html>"), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision,
                                     max_forward_display_images=1)
    result = (await service.enrich_messages((source,)))[0]
    text = result.segments[0].content[0].text
    assert "无法解析" in text and "超过本次" in text
    assert len(resolver.calls) == 1 and not vision.calls


async def test_disabled_and_provider_failure_are_safe():
    source = message(forward(line()))
    assert (await ImageEnrichmentService(enabled=False).enrich_messages((source,)))[0] is source
    result = (await ImageEnrichmentService(enabled=True, resolver=Resolver(),
                                          provider=Vision(fail=True)).enrich_messages((source,)))[0]
    assert "无法解析" in result.segments[0].content[0].text


@pytest.mark.parametrize("mode", ["private", "redirect", "html", "oversize", "ok"])
async def test_display_uses_bounded_http_resolver(mode):
    requests = []

    async def addresses(host):
        return ["127.0.0.1"] if mode == "private" else ["93.184.216.34"]

    def handler(request):
        requests.append(request)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        body = b"<html>" if mode == "html" else PNG
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = HttpImageResourceResolver(timeout_seconds=1, max_bytes=1 if mode == "oversize" else 100,
                                             client=client, address_resolver=addresses)
        vision = Vision()
        result = (await ImageEnrichmentService(enabled=True, resolver=resolver,
                    provider=vision).enrich_messages((message(forward(line())),)))[0]
    assert len(vision.calls) == (1 if mode == "ok" else 0)
    assert len(requests) == (0 if mode == "private" else 1)
    assert "test-secret" not in result.segments[0].content[0].text


def test_log_redaction_without_configured_secrets():
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "GET %s", (URL,), None)
    SecretRedactionFilter(()).filter(record)
    assert "test-secret" not in record.getMessage()
    assert "test-secret" not in redact_media_urls(URL)


def test_renderer_hides_signed_links_even_without_vision():
    source = message(TextSegment(position=0, raw_data=URL, text=URL))
    assert "test-secret" not in MessageRenderer().render_message(source)
    assert source.segments[0].text == URL


async def test_quoted_forward_later_than_first_three_lines_keeps_deep_text_and_images():
    text = ("=== 消息 1 ===\n[消息内容] 外层普通说明\n[消息类型] 引用消息\n"
            "[关联消息]\n--- 第1条 ---\n    [消息内容] [群聊的聊天记录]\n"
            "    [消息类型] 合并转发消息\n    [关联消息]\n    --- 第1条 ---\n"
            "        [消息内容] 内层预约时间是14点\n        " + line())
    segment = TextSegment(position=0, raw_data=text, text=text)
    reply = ReplySegment(position=0, raw_data={}, referenced_message_id=None,
        resolved_message=ResolvedMessageReference(None, ALICE, None, (segment,), {}))
    resolver, vision = Resolver(), Vision()
    result = (await ImageEnrichmentService(enabled=True, resolver=resolver,
                 provider=vision).enrich_messages((message(reply),)))[0]
    enriched = result.segments[0].resolved_message.segments[0].text
    assert "        [该附件的图片描述" in enriched
    assert "内层预约时间是14点" in enriched
    assert len(vision.calls) == 1
    assert segment.text == text


async def test_ordinary_quote_with_an_image_url_is_not_a_forward_display():
    text = "普通聊天\n这只是链接\n" + line()
    segment = TextSegment(position=0, raw_data=text, text=text)
    reply = ReplySegment(position=0, raw_data={}, referenced_message_id=None,
        resolved_message=ResolvedMessageReference(None, ALICE, None, (segment,), {}))
    resolver, vision = Resolver(), Vision()
    await ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision).enrich_messages((message(reply),))
    assert not resolver.calls and not vision.calls
