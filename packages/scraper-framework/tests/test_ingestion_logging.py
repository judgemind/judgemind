"""Tests for ingestion logging configuration.

Verifies that standard-library ``logging.getLogger()`` messages are routed
through structlog and rendered as JSON — ensuring CloudWatch visibility for
all ingestion modules (worker.py, db.py, etc.).
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import structlog


def test_stdlib_logging_routes_through_structlog() -> None:
    """Standard-library log records must be formatted as JSON via structlog.

    The ingestion entrypoint (``__main__.py``) configures a
    ``structlog.stdlib.ProcessorFormatter`` on the root logger so that
    modules using ``logging.getLogger(__name__)`` produce the same
    JSON output as modules using ``structlog.get_logger()``.

    This test imports the entrypoint module (which runs the configuration
    at import time), then emits a stdlib log record and checks that the
    output is valid JSON with the expected fields.
    """
    # Import the module to trigger the logging configuration side-effect.
    # The configuration runs at module level in __main__.py.
    import ingestion.__main__  # noqa: F401

    # Create a stdlib logger (same pattern as worker.py / db.py).
    test_logger = logging.getLogger("ingestion.test_logging_route")

    # Capture output by temporarily adding a handler with a StringIO stream.
    stream = io.StringIO()
    formatter = None
    # Find the ProcessorFormatter that __main__.py installed on root.
    for handler in logging.root.handlers:
        if isinstance(
            getattr(handler, "formatter", None),
            structlog.stdlib.ProcessorFormatter,
        ):
            formatter = handler.formatter
            break

    assert formatter is not None, (
        "No structlog.stdlib.ProcessorFormatter found on the root logger. "
        "The ingestion __main__.py logging configuration is missing or broken."
    )

    # Create a temporary handler using the same formatter.
    capture_handler = logging.StreamHandler(stream)
    capture_handler.setFormatter(formatter)
    test_logger.addHandler(capture_handler)
    try:
        test_logger.info("test message", extra={"doc_id": "abc123"})
    finally:
        test_logger.removeHandler(capture_handler)

    output = stream.getvalue().strip()
    assert output, "Expected log output but got empty string"

    record = json.loads(output)
    assert record["event"] == "test message"
    assert record["level"] == "info"
    # Verify the timestamp processor ran.
    assert "timestamp" in record
    # Verify that extra dict values are passed through (ExtraAdder).
    assert record["doc_id"] == "abc123"


def test_stdlib_logging_level_filtering() -> None:
    """DEBUG messages should be filtered out (root level is INFO)."""
    import ingestion.__main__  # noqa: F401

    test_logger = logging.getLogger("ingestion.test_level_filter")

    formatter = None
    for handler in logging.root.handlers:
        if isinstance(
            getattr(handler, "formatter", None),
            structlog.stdlib.ProcessorFormatter,
        ):
            formatter = handler.formatter
            break

    assert formatter is not None

    stream = io.StringIO()
    capture_handler = logging.StreamHandler(stream)
    capture_handler.setFormatter(formatter)
    # Match the root logger level (INFO) to verify filtering.
    capture_handler.setLevel(logging.DEBUG)
    test_logger.addHandler(capture_handler)
    try:
        test_logger.debug("should be filtered")
    finally:
        test_logger.removeHandler(capture_handler)

    output = stream.getvalue().strip()
    # DEBUG is below INFO — the root logger should not propagate it.
    assert output == "", f"Expected no output for DEBUG but got: {output}"


def test_structlog_exc_info_appears_in_json() -> None:
    """When exc_info=True is passed to a structlog logger, the formatted
    traceback must appear in the JSON output as an ``exception`` field.

    This tests the *actual* structlog configuration from ``__main__.py``
    by capturing stdout (where ``PrintLogger`` writes) and verifying that
    the JSON output contains the formatted traceback.
    """
    import ingestion.__main__  # noqa: F401

    # Use the actual configured logger — structlog.get_logger() uses the
    # processor chain from structlog.configure() in __main__.py.
    logger = structlog.get_logger("test_native_exc_info")

    captured = io.StringIO()
    with patch("sys.stdout", captured):
        try:
            raise ValueError("test FK violation")
        except ValueError:
            logger.error("DB write failed", exc_info=True)

    raw = captured.getvalue().strip()
    assert raw, "Expected log output but got empty string"

    record = json.loads(raw)
    assert record["event"] == "DB write failed"
    assert "exception" in record, (
        "The 'exception' field is missing from JSON output. "
        "structlog.processors.format_exc_info must be in the processor chain."
    )
    # The traceback should contain the original exception message and type.
    assert "ValueError" in record["exception"]
    assert "test FK violation" in record["exception"]
    assert "Traceback" in record["exception"]


def test_structlog_json_config_includes_format_exc_info() -> None:
    """Verify that the actual structlog.configure() call in __main__.py
    includes format_exc_info in its processor chain.

    This is a structural check — it reads the configured processors directly
    rather than testing through log output.  ``structlog.processors.format_exc_info``
    is an instance of ``structlog.processors.ExceptionRenderer``, so we check
    for the presence of that class in the chain.
    """
    import ingestion.__main__  # noqa: F401

    config = structlog.get_config()
    processors = config.get("processors", [])

    has_exc_renderer = any(
        isinstance(p, structlog.processors.ExceptionRenderer) for p in processors
    )
    assert has_exc_renderer, (
        f"ExceptionRenderer (format_exc_info) not found in structlog processor chain: "
        f"{processors}. Without this processor, exc_info=True tracebacks are silently "
        "dropped from JSON output."
    )


def test_stdlib_exc_info_appears_in_json() -> None:
    """When exc_info=True is passed to a stdlib logger routed through structlog,
    the formatted traceback must appear in the JSON output.

    This verifies that the ``ProcessorFormatter`` processor chain in
    ``__main__.py`` includes ``format_exc_info``.
    """
    import ingestion.__main__  # noqa: F401

    test_logger = logging.getLogger("ingestion.test_exc_info_stdlib")

    # Find the ProcessorFormatter installed by __main__.py.
    formatter = None
    for handler in logging.root.handlers:
        if isinstance(
            getattr(handler, "formatter", None),
            structlog.stdlib.ProcessorFormatter,
        ):
            formatter = handler.formatter
            break

    assert formatter is not None, "No structlog.stdlib.ProcessorFormatter found on the root logger."

    stream = io.StringIO()
    capture_handler = logging.StreamHandler(stream)
    capture_handler.setFormatter(formatter)
    test_logger.addHandler(capture_handler)
    try:
        try:
            raise RuntimeError("test enum cast error")
        except RuntimeError:
            test_logger.error("Ingestion failed", exc_info=True)
    finally:
        test_logger.removeHandler(capture_handler)

    raw = stream.getvalue().strip()
    assert raw, "Expected log output but got empty string"

    record = json.loads(raw)
    assert record["event"] == "Ingestion failed"
    assert "exception" in record, (
        "The 'exception' field is missing from stdlib-routed JSON output. "
        "The ProcessorFormatter chain must include format_exc_info."
    )
    assert "RuntimeError" in record["exception"]
    assert "test enum cast error" in record["exception"]
    assert "Traceback" in record["exception"]
