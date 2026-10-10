"""Tests for the global log secret-redaction filter."""

from __future__ import annotations

import logging
from io import StringIO

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


def test_root_handler_redacts_propagated_application_logs() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    root = logging.getLogger()
    root.addHandler(handler)
    filter_ = install_secret_redaction(("fake-provider-key",))
    try:
        logging.getLogger("app.redaction_test").warning(
            "request failed api_key=%s", "fake-provider-key"
        )
        assert output.getvalue() == "request failed api_key=***\n"
    finally:
        root.removeHandler(handler)
        root.removeFilter(filter_)
        for existing_handler in root.handlers:
            existing_handler.removeFilter(filter_)
        handler.close()


def test_propagated_exception_and_cause_are_redacted() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    parent = logging.getLogger("redaction_exception_test")
    parent.addHandler(handler)
    parent.propagate = False
    filter_ = install_secret_redaction(
        ("fake-provider-key",), logger_name=parent.name
    )
    child = logging.getLogger(f"{parent.name}.child")
    try:
        try:
            try:
                raise ValueError("upstream rejected fake-provider-key")
            except ValueError as cause:
                raise RuntimeError("request could not complete") from cause
        except RuntimeError:
            child.exception("provider request failed")
        rendered = output.getvalue()
        assert "fake-provider-key" not in rendered
        assert "ValueError: upstream rejected ***" in rendered
        assert "RuntimeError: request could not complete" in rendered
        assert "Traceback" in rendered
    finally:
        parent.removeFilter(filter_)
        parent.removeHandler(handler)
        parent.propagate = True
        handler.close()


def test_filter_redacts_cached_exception_and_stack_details() -> None:
    filter_ = SecretRedactionFilter(("fake-provider-key",))
    record = _make_record("safe message")
    record.exc_text = "ValueError: fake-provider-key"
    record.stack_info = "Stack details contain fake-provider-key"

    assert filter_.filter(record) is True

    rendered = logging.Formatter().format(record)
    assert "fake-provider-key" not in rendered
    assert "ValueError: ***" in rendered
    assert "Stack details contain ***" in rendered


def test_filter_preserves_exception_without_credentials() -> None:
    filter_ = SecretRedactionFilter(("fake-provider-key",))
    record = _make_record("safe message")
    record.exc_text = "ValueError: ordinary failure"
    assert filter_.filter(record) is True
    assert record.exc_text == "ValueError: ordinary failure"
