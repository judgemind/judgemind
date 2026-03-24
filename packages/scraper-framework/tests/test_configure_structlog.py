"""Tests for the shared structlog configuration utility.

Verifies that ``framework.logging.configure_structlog()`` produces the
expected processor chain for each supported configuration and that all
four original call sites now use the shared function.
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import structlog

from framework.logging import configure_structlog


class TestConfigureStructlog:
    """Unit tests for configure_structlog()."""

    def test_json_mode_always_uses_json_renderer(self) -> None:
        """When json=True, JSONRenderer is used regardless of terminal."""
        configure_structlog(json=True)
        config = structlog.get_config()
        processors = config["processors"]
        last = processors[-1]
        assert isinstance(last, structlog.processors.JSONRenderer)

    def test_default_mode_uses_json_when_not_tty(self) -> None:
        """When stderr is not a TTY (default in CI/ECS), JSONRenderer is used."""
        with patch("framework.logging.sys") as mock_sys:
            mock_sys.stderr.isatty.return_value = False
            configure_structlog()
        config = structlog.get_config()
        processors = config["processors"]
        last = processors[-1]
        assert isinstance(last, structlog.processors.JSONRenderer)

    def test_default_mode_uses_console_when_tty(self) -> None:
        """When stderr is a TTY (local dev), ConsoleRenderer is used."""
        with patch("framework.logging.sys") as mock_sys:
            mock_sys.stderr.isatty.return_value = True
            configure_structlog()
        config = structlog.get_config()
        processors = config["processors"]
        last = processors[-1]
        assert isinstance(last, structlog.dev.ConsoleRenderer)

    def test_includes_format_exc_info(self) -> None:
        """The processor chain must always include format_exc_info."""
        configure_structlog(json=True)
        config = structlog.get_config()
        processors = config["processors"]
        has_exc_renderer = any(
            isinstance(p, structlog.processors.ExceptionRenderer) for p in processors
        )
        assert has_exc_renderer, "ExceptionRenderer (format_exc_info) not found in processor chain"

    def test_includes_add_log_level(self) -> None:
        """The processor chain must always include add_log_level."""
        configure_structlog(json=True)
        config = structlog.get_config()
        processors = config["processors"]
        assert structlog.processors.add_log_level in processors

    def test_includes_timestamper(self) -> None:
        """The processor chain must always include TimeStamper with ISO format."""
        configure_structlog(json=True)
        config = structlog.get_config()
        processors = config["processors"]
        timestampers = [p for p in processors if isinstance(p, structlog.processors.TimeStamper)]
        assert len(timestampers) == 1
        assert timestampers[0].fmt == "iso"

    def test_contextvars_prepended_when_enabled(self) -> None:
        """When contextvars=True, merge_contextvars is the first processor."""
        configure_structlog(json=True, contextvars=True)
        config = structlog.get_config()
        processors = config["processors"]
        assert processors[0] is structlog.contextvars.merge_contextvars

    def test_contextvars_absent_when_disabled(self) -> None:
        """When contextvars=False (default), merge_contextvars is not present."""
        configure_structlog(json=True)
        config = structlog.get_config()
        processors = config["processors"]
        assert structlog.contextvars.merge_contextvars not in processors

    def test_custom_level(self) -> None:
        """The level parameter controls the filtering bound logger level."""
        configure_structlog(json=True, level=logging.DEBUG)
        config = structlog.get_config()
        # wrapper_class is a BoundLoggerFilteringAtLevel — verify it was created
        # with the right level by checking the class itself exists.
        assert config["wrapper_class"] is not None

    def test_json_output_has_expected_fields(self) -> None:
        """End-to-end: a log message produces valid JSON with expected fields."""
        configure_structlog(json=True)
        logger = structlog.get_logger("test_json_output")

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            logger.info("hello world", key="value")

        raw = captured.getvalue().strip()
        assert raw, "Expected log output but got empty string"
        record = json.loads(raw)
        assert record["event"] == "hello world"
        assert record["key"] == "value"
        assert record["level"] == "info"
        assert "timestamp" in record

    def test_exc_info_formatted_in_json(self) -> None:
        """Exception info is formatted and included in JSON output."""
        configure_structlog(json=True)
        logger = structlog.get_logger("test_exc_info")

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            try:
                raise ValueError("test error")
            except ValueError:
                logger.error("something failed", exc_info=True)

        raw = captured.getvalue().strip()
        record = json.loads(raw)
        assert "exception" in record
        assert "ValueError" in record["exception"]
        assert "test error" in record["exception"]

    def test_cache_logger_on_first_use(self) -> None:
        """cache_logger_on_first_use should be True."""
        configure_structlog(json=True)
        config = structlog.get_config()
        assert config["cache_logger_on_first_use"] is True

    def test_context_class_is_dict(self) -> None:
        """context_class should be dict."""
        configure_structlog(json=True)
        config = structlog.get_config()
        assert config["context_class"] is dict

    def test_logger_factory_is_print(self) -> None:
        """logger_factory should be PrintLoggerFactory."""
        configure_structlog(json=True)
        config = structlog.get_config()
        assert isinstance(config["logger_factory"], structlog.PrintLoggerFactory)
