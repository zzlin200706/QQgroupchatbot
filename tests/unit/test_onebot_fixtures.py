import json
from pathlib import Path


def test_real_group_text_fixture_is_sanitized_and_structural() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "onebot"
        / "real_group_text_sanitized.json"
    )
    event = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert event["post_type"] == "message"
    assert event["message_type"] == "group"
    assert event["group_id"] == 200000001
    assert event["group_name"] == "[REDACTED]"
    assert event["sender"] == {
        "card": "[REDACTED]",
        "nickname": "[REDACTED]",
        "role": "owner",
        "user_id": 100000001,
    }
    assert event["message"] == [{"type": "text", "data": {"text": "[REDACTED]"}}]
