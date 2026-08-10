import copy
import logging

from app.adapters.qq_official.gateway import QQGatewayDispatch
from app.adapters.qq_official.redaction import (
    dispatch_summary,
    is_duplicate_candidate,
    redact_gateway_dispatch,
)


def test_redacts_auth_token_but_keeps_message_scene_indexes_without_mutating_input() -> None:
    data = {
        "message_scene": {"ext": "auth_token=actual-token&msg_idx=5&ref_msg_idx=3"},
        "msg_idx": 5,
        "ref_msg_idx": 3,
    }
    original = copy.deepcopy(data)

    captured = redact_gateway_dispatch(
        QQGatewayDispatch(sequence=7, event_type="GROUP_MESSAGE_CREATE", data=data),
        captured_at="2026-08-10T00:00:00+00:00",
    )

    assert data == original
    assert captured["gateway"] == {"op": 0, "s": 7, "t": "GROUP_MESSAGE_CREATE"}
    redacted_data = captured["data"]
    assert redacted_data["message_scene"]["ext"] == "auth_token=<REDACTED>&msg_idx=5&ref_msg_idx=3"
    assert redacted_data["msg_idx"] == 5
    assert redacted_data["ref_msg_idx"] == 3


def test_redacts_attachment_url_credentials_and_preserves_recursive_tree() -> None:
    data = {
        "attachments": [{"url": "https://cdn.example.invalid/download?appid=1&fileid=2&rkey=secret"}],
        "msg_elements": [
            {
                "author": {"member_openid": "author"},
                "children": [
                    {"msg_elements": [{"author": None, "message_type": 102}]}
                ],
            }
        ],
    }
    dispatch = QQGatewayDispatch(sequence=8, event_type="GROUP_MESSAGE_CREATE", data=data)

    captured = redact_gateway_dispatch(dispatch, captured_at="2026-08-10T00:00:00+00:00")
    summary = dispatch_summary(dispatch)

    assert captured["data"]["attachments"][0]["url"] == (
        "https://cdn.example.invalid/download?appid=%3CREDACTED%3E&fileid=%3CREDACTED%3E&rkey=%3CREDACTED%3E"
    )
    assert captured["data"]["msg_elements"][0]["children"][0]["msg_elements"][0]["author"] is None
    assert summary["max_nested_depth"] == 2
    assert summary["nested_author_missing"] == 1


def test_summary_logging_can_expose_no_secret_content(caplog) -> None:
    dispatch = QQGatewayDispatch(
        sequence=9,
        event_type="GROUP_MESSAGE_CREATE",
        data={"content": "private", "message_scene": {"ext": "auth_token=secret"}},
    )

    with caplog.at_level(logging.INFO):
        summary = dispatch_summary(dispatch)
        logging.getLogger(__name__).info("capture event_type=%s", summary["event_type"])

    assert "GROUP_MESSAGE_CREATE" in caplog.text
    assert "private" not in caplog.text
    assert "secret" not in caplog.text


def test_duplicate_candidate_does_not_compare_gateway_sequence_across_sessions() -> None:
    earlier_capture = {
        "event_type": "GROUP_MESSAGE_CREATE",
        "gateway_sequence": 2,
        "message_id": "earlier-message",
    }
    different_message_same_sequence = QQGatewayDispatch(
        sequence=2,
        event_type="GROUP_MESSAGE_CREATE",
        data={"id": "later-message"},
    )

    assert not is_duplicate_candidate([earlier_capture], different_message_same_sequence)
    same_message = QQGatewayDispatch(
        sequence=99,
        event_type="GROUP_MESSAGE_CREATE",
        data={"id": "earlier-message"},
    )
    assert is_duplicate_candidate([earlier_capture], same_message)
