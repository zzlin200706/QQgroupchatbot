"""Tests for the structured event log helper."""

from __future__ import annotations

import logging

import pytest

from app.observability import event


def test_event_emits_at_named_logger(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.events")
    event("bot.started", app_env="test", count=3)
    record = caplog.records[0]
    assert record.name == "app.events"
    assert record.levelno == logging.INFO
    assert "event=bot.started" in record.getMessage()
    assert "app_env=test" in record.getMessage()
    assert "count=3" in record.getMessage()


def test_event_renders_boolean_and_none(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.events")
    event("x", ready=True, missing=None, label="hi there")
    rendered = caplog.records[0].getMessage()
    assert "ready=true" in rendered
    assert "missing=null" in rendered
    assert "label=hi there" in rendered


def test_event_honours_level_argument(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.events")
    event("x", level="warning")
    assert caplog.records[0].levelno == logging.WARNING


def test_unknown_level_falls_back_to_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.events")
    event("x", level="not-a-level")
    assert caplog.records[0].levelno == logging.INFO
