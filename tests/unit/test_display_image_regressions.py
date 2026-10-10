from io import BytesIO

import pytest
from PIL import Image

from app.domain.images import ResolvedImage
from app.domain.messages.forward_display import explicit_image_url, image_resource_key
from app.services.image_enrichment import ImageEnrichmentService
from app.services.image_resources import (
    ImageResolveError, ImageTooLargeError, UnsupportedImageError, prepare_display_image,
)
from tests.unit.test_forward_display_images import (
    URL, PNG, Resolver, Vision, forward, image_bytes, line, message,
)


def test_valid_jpeg_with_24_byte_trailer_is_decoded_and_normalized():
    original = image_bytes("JPEG") + b"x" * 24
    result = prepare_display_image(ResolvedImage(data=original, content_type="image/jpeg"))
    assert result.data.endswith(b"\xff\xd9")
    assert result.data != original and original.endswith(b"x" * 24)
    with Image.open(BytesIO(result.data)) as decoded:
        decoded.load()
        assert decoded.size == (10, 20)


async def test_jpeg_trailer_reaches_vision_as_normalized_bytes_without_mutating_source():
    data = image_bytes("JPEG") + b"x" * 24

    class JpegResolver(Resolver):
        async def resolve(self, image):
            return ResolvedImage(data=data, content_type="image/jpeg")

    vision = Vision()
    source = message(forward(line()))
    result = (await ImageEnrichmentService(enabled=True, resolver=JpegResolver(),
                                          provider=vision).enrich_messages((source,)))[0]
    assert len(vision.calls) == 1 and vision.calls[0].data.endswith(b"\xff\xd9")
    assert "一张测试截图" in result.segments[0].content[0].text
    assert source.segments[0].content[0].text == line()
    assert data.endswith(b"x" * 24)


@pytest.mark.parametrize("data,mime", [
    (image_bytes("JPEG")[:-30], "image/jpeg"),
    (b"\xff\xd8\xffnot really jpeg\xff\xd9", "image/jpeg"),
    (PNG, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20, "image/png"),
    (b"<html>failure</html>", "image/png"),
])
def test_corrupt_truncated_fake_or_mislabeled_images_remain_rejected(data, mime):
    with pytest.raises(UnsupportedImageError):
        prepare_display_image(ResolvedImage(data=data, content_type=mime))


@pytest.mark.parametrize("format,mime", [
    ("JPEG", "image/jpeg"), ("PNG", "image/png"), ("GIF", "image/gif"), ("WEBP", "image/webp"),
])
def test_normal_supported_images_are_unchanged(format, mime):
    image = ResolvedImage(data=image_bytes(format), content_type=mime)
    assert prepare_display_image(image) is image


def test_pixel_and_normalized_output_limits():
    with pytest.raises(ImageTooLargeError, match="pixel"):
        prepare_display_image(ResolvedImage(data=PNG, content_type="image/png"), max_pixels=199)
    # A tiny encoded grayscale JPEG grows when normalized to RGB.
    output = BytesIO()
    with Image.new("L", (10, 20)) as im:
        im.save(output, format="JPEG")
    data = output.getvalue() + b"tail"
    with pytest.raises(ImageTooLargeError, match="normalized"):
        prepare_display_image(ResolvedImage(data=data, content_type="image/jpeg"), max_bytes=len(data))


def test_resource_key_only_ignores_signature_and_rejects_ambiguous_query():
    assert image_resource_key(URL) == image_resource_key(URL.replace("test-secret", "new-signature"))
    for other in (URL.replace("spec=0", "spec=1"), URL.replace("appid=1", "appid=2"),
                  URL.replace("fixture", "another"), URL + "&variant=x"):
        assert image_resource_key(URL) != image_resource_key(other)
    assert explicit_image_url(line(URL + "&fileid=another")) is None


async def test_17_links_for_six_resources_only_download_six_and_use_one_content_result():
    urls = [URL.replace("fixture", f"file-{i % 6}").replace("test-secret", f"signature-{i}")
            for i in range(17)]
    source = message(forward("\n".join(line(url) for url in urls)))
    resolver, vision = Resolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    result = (await service.enrich_messages((source,)))[0]
    text = result.segments[0].content[0].text
    assert "超过" not in text and text.count("一张测试截图") == 17
    assert len(resolver.calls) == 6 and len(vision.calls) == 1
    assert "signature-" not in text


async def test_renewed_link_after_failure_can_recover_without_extra_resource_slot():
    new_url = URL.replace("test-secret", "renewed")

    class ExpiredResolver(Resolver):
        async def resolve(self, image):
            if image.url == URL:
                self.calls.append(image.url)
                raise ImageResolveError("HTTP 403")
            return await super().resolve(image)

    resolver, vision = ExpiredResolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision,
                                     max_forward_display_images=1)
    source = message(forward("\n".join(line(url) for url in (URL, URL, new_url, URL))))
    text = (await service.enrich_messages((source,)))[0].segments[0].content[0].text
    assert text.count("图片下载失败") == 2
    assert text.count("一张测试截图") == 2
    assert resolver.calls == [URL, new_url] and len(vision.calls) == 1


async def test_failed_resource_has_bounded_alternate_attempts():
    urls = [URL.replace("test-secret", str(i)) for i in range(20)]
    resolver = Resolver(b"not image")
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=Vision())
    result = (await service.enrich_messages((message(forward("\n".join(map(line, urls)))),)))[0]
    assert len(resolver.calls) == 2
    assert "备用链接尝试已达上限" in result.segments[0].content[0].text


async def test_different_content_is_not_reused_and_cache_does_not_cross_calls():
    class DistinctResolver(Resolver):
        async def resolve(self, image):
            self.calls.append(image.url)
            return ResolvedImage(data=image_bytes(color="red" if "fixture" in image.url else "blue"),
                                 content_type="image/png")

    resolver, vision = DistinctResolver(), Vision()
    service = ImageEnrichmentService(enabled=True, resolver=resolver, provider=vision)
    source = message(forward(line() + "\n" + line(URL.replace("fixture", "other"))))
    await service.enrich_messages((source,))
    assert len(vision.calls) == 2
    await service.enrich_messages((source,))
    assert len(vision.calls) == 4


@pytest.mark.parametrize("failure,expected", [("download", "图片下载失败"),
                                               ("validation", "图片格式或解码校验失败"),
                                               ("vision", "视觉模型调用失败")])
async def test_safe_error_stage_and_attachment_location(failure, expected, caplog):
    class BrokenResolver(Resolver):
        async def resolve(self, image):
            if failure == "download":
                raise ImageResolveError(URL)
            return await super().resolve(image)

    service = ImageEnrichmentService(enabled=True,
        resolver=BrokenResolver(b"bad" if failure == "validation" else PNG),
        provider=Vision(fail=failure == "vision"))
    text = (await service.enrich_messages((message(forward("header\n" + line())),)))[0].segments[0].content[0].text
    assert expected in text
    assert "test-secret" not in caplog.text
    assert failure in caplog.text and "line=2" in caplog.text
