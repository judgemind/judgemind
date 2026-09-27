"""Tests for the per-doc ``reingest_doc_timing`` instrumentation (#4116).

The ``DocTiming`` helper in ``ingestion.doc_timing`` is the shared
primitive used by both ``ingestion.worker.IngestionWorker.process_event``
(live ingestion) and, before #4845, ``scripts/reingest_from_s3.py``
to emit one structured timing line per processed document.

These tests verify:

* The log line fires exactly once per document.
* All four required phase keys are always present
  (``parse_document_ms``, ``regex_fallback_ms``, ``validation_ms``,
  ``db_write_ms``) — even when a phase was never entered.
* Re-entering the same phase accumulates time additively (split-doc
  semantics — see ``ingestion.doc_timing.DocTiming.add_ms``).
* ``time.perf_counter`` is mocked deterministically so the bucket
  values are exact integer ms.
* Calling ``phase()`` with an unknown name fails loudly.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ingestion.doc_timing import (
    EVENT_NAME,
    PHASE_NAMES,
    DocTiming,
    emit_via_stdlib_logger,
    emit_via_structlog_logger,
)


def _capture_emit() -> tuple[list[dict], callable]:
    """Build a list-backed emit_fn for assertions."""
    captured: list[dict] = []

    def _emit(payload: dict) -> None:
        captured.append(payload)

    return captured, _emit


def test_reingest_doc_timing_emits_all_required_keys() -> None:
    """All four phase buckets + total + extras + event name must appear.

    Acceptance criterion #3 (issue #4116): the log line carries all
    expected keys with deterministic values when ``time.perf_counter``
    is mocked.
    """
    captured, emit_fn = _capture_emit()

    # Mock time.perf_counter to return increasing ticks (one tick per
    # call, 0.1 seconds apart = 100 ms each).
    ticks = iter([0.0, 0.1, 0.2, 0.32, 0.5, 0.55])
    with patch("ingestion.doc_timing.time.perf_counter", side_effect=lambda: next(ticks)):
        with DocTiming(
            emit_fn,
            document_id="abc-123",
            county="Federal",
        ) as t:
            with t.phase("parse_document_ms"):  # 0.0 -> 0.1 = 100 ms
                pass
            with t.phase("regex_fallback_ms"):  # 0.2 -> 0.32 = 120 ms
                pass
            with t.phase("validation_ms"):  # 0.5 -> 0.55 = 50 ms
                pass

    assert len(captured) == 1
    payload = captured[0]

    # All four phase keys present + event + total_ms + extras.
    for key in PHASE_NAMES:
        assert key in payload, f"missing phase {key!r} in {payload}"

    assert payload["event"] == EVENT_NAME
    assert payload["document_id"] == "abc-123"
    assert payload["county"] == "Federal"

    # Deterministic mock values.
    assert payload["parse_document_ms"] == 100
    assert payload["regex_fallback_ms"] == 120
    assert payload["validation_ms"] == 50
    # Never entered → 0.
    assert payload["db_write_ms"] == 0

    # total_ms = sum of buckets.
    assert payload["total_ms"] == 270


def test_reingest_doc_timing_unentered_phases_report_zero() -> None:
    """The log line is rectangular: every bucket has a value (default 0)."""
    captured, emit_fn = _capture_emit()

    with DocTiming(emit_fn, document_id="d1") as t:
        # Don't enter any phase — should still emit with all four buckets.
        del t

    assert len(captured) == 1
    payload = captured[0]
    for key in PHASE_NAMES:
        assert payload[key] == 0, f"{key} should default to 0, got {payload[key]}"
    assert payload["total_ms"] == 0


def test_reingest_doc_timing_phase_re_entry_accumulates() -> None:
    """Re-entering the same phase (e.g. per-ruling regex on a split doc)
    must accumulate, not overwrite.
    """
    captured, emit_fn = _capture_emit()

    # 4 ticks: two phase blocks, each 50ms.
    ticks = iter([0.0, 0.05, 0.1, 0.15])
    with patch("ingestion.doc_timing.time.perf_counter", side_effect=lambda: next(ticks)):
        with DocTiming(emit_fn, document_id="d1") as t:
            with t.phase("regex_fallback_ms"):
                pass
            with t.phase("regex_fallback_ms"):
                pass

    assert captured[0]["regex_fallback_ms"] == 100  # 50 + 50, not 50


def test_reingest_doc_timing_add_ms_accumulates() -> None:
    """``add_ms`` writes directly to a bucket (used in worker.py + reingest
    where re-indenting under ``with timing.phase(...)`` is impractical).
    """
    captured, emit_fn = _capture_emit()

    with DocTiming(emit_fn, document_id="d1") as t:
        t.add_ms("db_write_ms", 42.0)
        t.add_ms("db_write_ms", 8.0)

    assert captured[0]["db_write_ms"] == 50


def test_reingest_doc_timing_unknown_phase_raises() -> None:
    """Typos in phase names fail loudly rather than silently dropping."""
    _, emit_fn = _capture_emit()
    with DocTiming(emit_fn, document_id="d1") as t:
        with pytest.raises(ValueError, match="Unknown phase"):
            with t.phase("nonexistent_phase_ms"):
                pass


def test_reingest_doc_timing_emits_on_exception() -> None:
    """Even when the body raises, the log line must still fire — that's
    the whole point of the helper for #4104-style perf bugs.
    """
    captured, emit_fn = _capture_emit()

    with pytest.raises(RuntimeError):
        with DocTiming(emit_fn, document_id="d1") as t:
            with t.phase("parse_document_ms"):
                raise RuntimeError("simulated parse failure")

    assert len(captured) == 1
    assert captured[0]["document_id"] == "d1"


def test_reingest_doc_timing_emit_is_idempotent() -> None:
    """Manual ``emit()`` followed by ``__exit__`` only logs once."""
    captured, emit_fn = _capture_emit()

    t = DocTiming(emit_fn, document_id="d1")
    t.emit()
    t.emit()  # no-op

    with t:
        pass  # __exit__ also calls emit() — still no double-log

    assert len(captured) == 1


def test_emit_via_stdlib_logger_routes_to_logging_extra() -> None:
    """``emit_via_stdlib_logger`` calls ``logger.info(event, extra=...)``."""
    import logging

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    log = logging.getLogger("test_doc_timing.stdlib")
    log.setLevel(logging.DEBUG)
    handler = _Capture()
    log.addHandler(handler)

    try:
        with DocTiming(emit_via_stdlib_logger(log), document_id="d1") as t:
            t.add_ms("parse_document_ms", 12.0)
    finally:
        log.removeHandler(handler)

    assert len(captured) == 1
    rec = captured[0]
    assert rec.getMessage() == EVENT_NAME
    # ``extra=`` kwargs land as record attributes.
    assert rec.parse_document_ms == 12  # type: ignore[attr-defined]
    assert rec.document_id == "d1"  # type: ignore[attr-defined]
    assert rec.telemetry_event == EVENT_NAME  # type: ignore[attr-defined]


def test_emit_via_structlog_logger_routes_to_kwargs() -> None:
    """``emit_via_structlog_logger`` invokes ``logger.info(event, **payload)``.

    Accepts a stand-in object with an ``info(event, **kwargs)`` shape so
    we don't need to depend on structlog being importable in the test
    environment.
    """
    captured: list[tuple[str, dict]] = []

    class _StructlogStub:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

    stub = _StructlogStub()

    with DocTiming(emit_via_structlog_logger(stub), document_id="d1") as t:
        t.add_ms("regex_fallback_ms", 25.0)

    assert len(captured) == 1
    event, kwargs = captured[0]
    assert event == EVENT_NAME
    assert kwargs["regex_fallback_ms"] == 25
    assert kwargs["document_id"] == "d1"
    # All four buckets always present.
    for key in PHASE_NAMES:
        assert key in kwargs
