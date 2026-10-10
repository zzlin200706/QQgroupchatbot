"""Conservative recognition of observed QQ forward display attachment lines."""

import re
from urllib.parse import parse_qs, urlsplit


IMAGE_LINE = re.compile(
    r"^[ \t]*\[附件[1-9][0-9]*\][ \t]+类型:图片[ \t]+"
    r"文件名:[^\r\n]+?[ \t]+尺寸:[0-9]+x[0-9]+[ \t]+"
    r"大小:[0-9.]+(?:B|KB|MB)[ \t]+URL:(?P<url>https://[^\s<>]+)[ \t]*$"
)
_SIGNED_MEDIA_URL = re.compile(
    r"https?://multimedia\.nt\.qq\.com\.cn(?::443)?/download\?[^\s<>\"']+",
    re.IGNORECASE,
)


def is_forward_display(text: str) -> bool:
    lines = text.splitlines()
    headers = {
        "[群聊的聊天记录]", "[消息内容] [群聊的聊天记录]"
    }
    if any(line.strip() in headers for line in lines[:3]):
        return True
    # A quoted wrapper may start with ordinary text, while a later child holds
    # the forward. Recognize the observed display labels, not arbitrary URLs.
    stripped = [line.strip() for line in lines]
    return (any(line in headers for line in stripped)
            and "[消息类型] 合并转发消息" in stripped
            and "[关联消息]" in stripped)


def explicit_image_url(line: str) -> str | None:
    match = IMAGE_LINE.fullmatch(line.rstrip("\r\n"))
    if not match:
        return None
    url = match.group("url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "multimedia.nt.qq.com.cn"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment or parsed.path != "/download"):
            return None
        query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        return None
    if (not {"appid", "fileid", "rkey"}.issubset(query)
            or any(len(values) != 1 for values in query.values())
            or any(not query[key][0] for key in ("appid", "fileid", "rkey"))):
        return None
    return url


def image_resource_key(url: str) -> tuple[tuple[str, str], ...]:
    """For already validated QQ media URLs only; retain variant/unknown query fields.

    Real samples keep appid/fileid/spec stable while rkey changes across quotes.
    This is a batch-local reuse key, never an authorization or identity assertion.
    """
    query = parse_qs(urlsplit(url).query, strict_parsing=True, keep_blank_values=True)
    return tuple(sorted((key, value) for key, values in query.items()
                        if key != "rkey" for value in values))


def redact_media_urls(text: str) -> str:
    return _SIGNED_MEDIA_URL.sub("[图片链接已隐藏]", text)
