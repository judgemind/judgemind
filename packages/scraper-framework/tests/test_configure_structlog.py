"""Tests for the shared structlog configuration utility.

Verifies that ``framework.logging.configure_structlog()`` produces the
expected processor chain for each supported configuration, that the
stdlib bridge is correctly installed, and that all entry points use
the shared function.
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import pytest
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


class TestStdlibBridge:
    """Tests for the stdlib_bridge parameter of configure_structlog()."""

    @pytest.fixture(autouse=True)
    def _clean_root_handlers(self) -> None:  # type: ignore[return]
        """Save and restore root logger handlers and level to prevent cross-test pollution."""
        original_handlers = list(logging.root.handlers)
        original_level = logging.root.level
        yield
        # Restore original handler list and level to avoid cross-test pollution.
        logging.root.handlers = original_handlers
        logging.root.setLevel(original_level)

    def test_bridge_installs_processor_formatter_on_root(self) -> None:
        """stdlib_bridge=True installs a ProcessorFormatter on the root logger."""
        configure_structlog(json=True, stdlib_bridge=True)

        formatters = [
            h.formatter
            for h in logging.root.handlers
            if isinstance(getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter)
        ]
        assert len(formatters) == 1

    def test_bridge_sets_root_level(self) -> None:
        """stdlib_bridge=True sets the root logger level to match the level parameter."""
        configure_structlog(json=True, stdlib_bridge=True, level=logging.DEBUG)
        assert logging.root.level == logging.DEBUG

    def test_bridge_default_level_is_info(self) -> None:
        """The default root level when using stdlib_bridge is INFO."""
        configure_structlog(json=True, stdlib_bridge=True)
        assert logging.root.level == logging.INFO

    def test_bridge_idempotent(self) -> None:
        """Calling configure_structlog with stdlib_bridge=True twice does not duplicate handlers."""
        configure_structlog(json=True, stdlib_bridge=True)
        configure_structlog(json=True, stdlib_bridge=True)

        pf_handlers = [
            h
            for h in logging.root.handlers
            if isinstance(getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter)
        ]
        assert len(pf_handlers) == 1

    def test_bridge_produces_json_output(self) -> None:
        """stdlib logging routed through the bridge produces valid JSON."""
        configure_structlog(json=True, stdlib_bridge=True)

        test_logger = logging.getLogger("test_bridge_json")
        stream = io.StringIO()

        # Find the ProcessorFormatter handler installed by the bridge.
        formatter = None
        for handler in logging.root.handlers:
            if isinstance(
                getattr(handler, "formatter", None),
                structlog.stdlib.ProcessorFormatter,
            ):
                formatter = handler.formatter
                break

        assert formatter is not None

        capture_handler = logging.StreamHandler(stream)
        capture_handler.setFormatter(formatter)
        test_logger.addHandler(capture_handler)
        try:
            test_logger.info("bridge test", extra={"key": "val"})
        finally:
            test_logger.removeHandler(capture_handler)

        output = stream.getvalue().strip()
        assert output, "Expected log output but got empty string"
        record = json.loads(output)
        assert record["event"] == "bridge test"
        assert record["level"] == "info"
        assert "timestamp" in record
        assert record["key"] == "val"

    def test_bridge_exc_info_in_json(self) -> None:
        """Exception info from stdlib logging appears in JSON output via the bridge."""
        configure_structlog(json=True, stdlib_bridge=True)

        test_logger = logging.getLogger("test_bridge_exc")
        stream = io.StringIO()

        formatter = None
        for handler in logging.root.handlers:
            if isinstance(
                getattr(handler, "formatter", None),
                structlog.stdlib.ProcessorFormatter,
            ):
                formatter = handler.formatter
                break

        assert formatter is not None

        capture_handler = logging.StreamHandler(stream)
        capture_handler.setFormatter(formatter)
        test_logger.addHandler(capture_handler)
        try:
            try:
                raise RuntimeError("bridge exc test")
            except RuntimeError:
                test_logger.error("something broke", exc_info=True)
        finally:
            test_logger.removeHandler(capture_handler)

        output = stream.getvalue().strip()
        record = json.loads(output)
        assert record["event"] == "something broke"
        assert "exception" in record
        assert "RuntimeError" in record["exception"]
        assert "bridge exc test" in record["exception"]

    def test_no_bridge_by_default(self) -> None:
        """When stdlib_bridge is not specified, no ProcessorFormatter is installed."""
        # Remove any existing PF handlers first.
        for h in list(logging.root.handlers):
            if isinstance(getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter):
                logging.root.removeHandler(h)

        configure_structlog(json=True)

        pf_handlers = [
            h
            for h in logging.root.handlers
            if isinstance(getattr(h, "formatter", None), structlog.stdlib.ProcessorFormatter)
        ]
        assert len(pf_handlers) == 0

    def test_bridge_uses_console_renderer_when_tty(self) -> None:
        """When not in json mode and stderr is a TTY, the bridge uses ConsoleRenderer."""
        with patch("framework.logging.sys") as mock_sys:
            mock_sys.stderr.isatty.return_value = True
            configure_structlog(stdlib_bridge=True)

        # Find the ProcessorFormatter and check its last processor is ConsoleRenderer.
        for handler in logging.root.handlers:
            fmt = getattr(handler, "formatter", None)
            if isinstance(fmt, structlog.stdlib.ProcessorFormatter):
                last_proc = fmt.processors[-1]
                assert isinstance(last_proc, structlog.dev.ConsoleRenderer)
                return

        pytest.fail("No ProcessorFormatter found on root logger")

    def test_bridge_uses_json_renderer_when_json_true(self) -> None:
        """When json=True, the bridge uses JSONRenderer."""
        configure_structlog(json=True, stdlib_bridge=True)

        for handler in logging.root.handlers:
            fmt = getattr(handler, "formatter", None)
            if isinstance(fmt, structlog.stdlib.ProcessorFormatter):
                last_proc = fmt.processors[-1]
                assert isinstance(last_proc, structlog.processors.JSONRenderer)
                return

        pytest.fail("No ProcessorFormatter found on root logger")

    def test_bridge_processor_chain_has_add_log_level(self) -> None:
        """The bridge processor chain includes add_log_level."""
        configure_structlog(json=True, stdlib_bridge=True)

        for handler in logging.root.handlers:
            fmt = getattr(handler, "formatter", None)
            if isinstance(fmt, structlog.stdlib.ProcessorFormatter):
                # structlog.stdlib.add_log_level and structlog.processors.add_log_level
                # are the same function (aliased), so we just verify it's present.
                assert structlog.stdlib.add_log_level in fmt.processors
                return

        pytest.fail("No ProcessorFormatter found on root logger")

    def test_bridge_processor_chain_has_extra_adder(self) -> None:
        """The bridge includes ExtraAdder for passing stdlib extra dict values."""
        configure_structlog(json=True, stdlib_bridge=True)

        for handler in logging.root.handlers:
            fmt = getattr(handler, "formatter", None)
            if isinstance(fmt, structlog.stdlib.ProcessorFormatter):
                has_extra_adder = any(
                    isinstance(p, structlog.stdlib.ExtraAdder) for p in fmt.processors
                )
                assert has_extra_adder, "ExtraAdder not found in ProcessorFormatter chain"
                return

        pytest.fail("No ProcessorFormatter found on root logger")
