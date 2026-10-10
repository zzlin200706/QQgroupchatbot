import json

import pytest

from scripts.audit_napcat_forward import audit_response


def message(*segments):
    return {"sender": {"user_id": "private-id", "nickname": "private-name"}, "message": list(segments)}


def response(*messages):
    return {"status": "ok", "retcode": 0, "data": {"messages": list(messages)}}


def text():
    return {"type": "text", "data": {"text": "private chat text"}}


def image(url="https://example.test/image?rkey=private-signature"):
    return {"type": "image", "data": {"url": url}}


def forward(*children):
    return {"type": "forward", "data": {"id": "private-forward-id", "content": list(children)}}


def test_three_level_text_images_and_secrets_are_not_printed():
    payload = response(message(text(), forward(message(text(), image(), forward(message(text(), image()))))))
    result = audit_response(payload)
    assert result["has_nested_text"] and result["has_nested_image_url_candidate"]
    assert result["levels"]["3"]["images"] == 1
    assert not result["requires_review"]
    assert not result["live_fetch_verified"] and not result["vision_verified"]
    assert "private" not in json.dumps(result)


def test_inner_id_only_is_not_reported_as_readable():
    result = audit_response(response(message({"type": "forward", "data": {"id": "only-id"}})))
    assert result["findings"]["unexpanded_forwards"] == 1
    assert result["requires_review"] and not result["has_nested_text"]


@pytest.mark.parametrize("url", [None, "", "/tmp/local-file.jpg", "file:///tmp/local.jpg", "https://user:secret@example.test/image"])
def test_local_paths_or_missing_urls_do_not_count_as_download_candidates(url):
    result = audit_response(response(message(forward(message(image(url))))))
    assert result["requires_review"] and not result["has_nested_image_url_candidate"]


@pytest.mark.parametrize("payload", [None, [], {}, {"status": "failed", "retcode": 1},
                                     {"status": "ok", "retcode": False, "data": {"messages": []}}])
def test_bad_envelopes(payload):
    assert audit_response(payload)["findings"]["invalid_response"] == 1


def test_limits_and_cycles_cannot_masquerade_as_complete_expansion():
    payload = response(message(forward(message(text(), image()))))
    assert audit_response(payload, max_depth=1)["findings"]["depth_limit"] == 1
    assert audit_response(payload, max_items=1)["findings"]["item_limit"] == 1
    payload["data"]["messages"][0]["message"][0]["data"]["content"] = payload["data"]["messages"]
    assert audit_response(payload)["findings"]["cycles"] == 1


def test_standard_onebot_shape_is_not_silently_assumed_to_be_napcat_shape():
    result = audit_response({"status": "ok", "retcode": 0, "data": {"message": []}})
    assert result["requires_review"]


def test_empty_response_is_not_an_acceptance_success():
    result = audit_response(response())
    assert result["requires_review"] and result["findings"]["no_messages"] == 1
