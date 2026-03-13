"""Tests for ingestion logging configuration.

Verifies that standard-library ``logging.getLogger()`` messages are routed
through structlog and rendered as JSON — ensuring CloudWatch visibility for
all ingestion modules (worker.py, db.py, etc.).
"""

from __future__ import annotations

import io
import json
import logging

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
