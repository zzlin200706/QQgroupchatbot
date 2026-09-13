"""Tests for the global log secret-redaction filter."""

from __future__ import annotations

import logging

from app.log_redaction import SecretRedactionFilter, install_secret_redaction


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_filter_redacts_known_secret() -> None:
    filter_ = SecretRedactionFilter(secrets=("sk-supersecret",))
    record = _make_record("api_key=sk-supersecret failed")
    assert filter_.filter(record) is True
    assert "sk-supersecret" not in record.getMessage()
    assert "***" in record.getMessage()


def test_filter_passes_through_unknown_text() -> None:
    filter_ = SecretRedactionFilter(secrets=("sk-supersecret",))
    record = _make_record("harmless message")
    assert filter_.filter(record) is True
    assert record.getMessage() == "harmless message"


def test_filter_ignores_empty_secrets() -> None:
    filter_ = SecretRedactionFilter(secrets=("", "  "))
    record = _make_record("api_key=sk-supersecret")
    assert filter_.filter(record) is True
    assert record.getMessage() == "api_key=sk-supersecret"


def test_install_secret_redaction_attaches_to_root() -> None:
    # Use a private logger to avoid polluting real configuration.
    test_logger = logging.getLogger("test_redaction")
    test_logger.handlers = []
    test_logger.propagate = False
    install_secret_redaction(("hunter2",), logger_name="test_redaction")
    record = _make_record("password is hunter2 today")
    test_logger.handle(record)
    # The filter is attached and active.
    assert any(
        isinstance(f, SecretRedactionFilter) for f in test_logger.filters
    )
