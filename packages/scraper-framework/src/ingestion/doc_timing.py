"""Per-document phase timing helper for the ingestion pipeline (#4116).

Both ``packages/scraper-framework/src/ingestion/worker.py`` (live ingestion)
and ``scripts/reingest_from_s3.py`` (offline reingest) process documents
through the same logical phases:

  1. ``parse_document_ms`` — extract text from the raw S3 content via
     pdfplumber/HTML and run the scraper's ``parse_document()``.
  2. ``regex_fallback_ms`` — apply deterministic regex fallbacks to fill
     fields that scraper + LLM extraction left missing.
  3. ``validation_ms`` — run the deterministic validator (#2424) against
     the extracted ruling.
  4. ``db_write_ms`` — upsert case + insert document/ruling + parties.

When one of these phases regresses (e.g. #4104's quadratic regex on long
opinion text), the bottleneck used to be invisible without external
profiling.  This helper emits a single structured ``reingest_doc_timing``
log line per document so the next perf bug in this pipeline is
self-diagnosing.

Usage::

    from ingestion.doc_timing import DocTiming

    with DocTiming(emit_fn, document_id="abc-123", county="Federal") as t:
        with t.phase("parse_document_ms"):
            text = parse_pdf(raw_content)
            extracted = scraper.parse_document(...)

        # ... LLM extraction ...

        with t.phase("regex_fallback_ms"):
            apply_regex_fallbacks(extracted, text)

        with t.phase("validation_ms"):
            det_result = run_deterministic_rules(...)

        with t.phase("db_write_ms"):
            upsert_case(...)
            insert_document_and_ruling(...)

On context-manager exit, ``DocTiming`` calls ``emit_fn(payload)`` once
with a dict shaped like::

    {
      "event": "reingest_doc_timing",
      "document_id": "abc-123",
      "county": "Federal",
      "parse_document_ms": 12,
      "regex_fallback_ms": 2419,
      "validation_ms": 8,
      "db_write_ms": 47,
      "total_ms": 2486,
    }

Two ready-made emitters are provided so the worker (stdlib logging) and
the reingest script (structlog) can share the timing primitive without
each call site having to know how to format the dict:

  * :func:`emit_via_stdlib_logger` — for ``logging.Logger``.
  * :func:`emit_via_structlog_logger` — for ``structlog.BoundLogger``.

Phases that were never entered are reported as ``0``.  Calling
``timing.phase(name)`` for an unknown phase raises ``ValueError`` so a
typo in the call site fails loudly rather than dropping a measurement
silently.

Re-entering the same phase accumulates time (e.g. ``regex_fallback_ms``
fires once per ruling for split documents).  This matches operator
intuition: if a 5-ruling document spends 100ms in regex per ruling, the
log line shows 500ms total time spent in that phase.

Timer source: ``time.perf_counter()`` — high-resolution, monotonic, not
affected by wall-clock adjustments.  Tests can mock this to assert
specific bucket values deterministically.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

# Single source of truth for the bucket names.  Worker and reingest
# import this so adding a new bucket only updates one place.
PHASE_NAMES: tuple[str, ...] = (
    "parse_document_ms",
    "regex_fallback_ms",
    "validation_ms",
    "db_write_ms",
)

#: Stable event name used in both stdlib + structlog emitters.  Tests
#: and CloudWatch Logs Insights queries pivot on this string, so it is
#: defined once here.
EVENT_NAME: str = "reingest_doc_timing"

EmitFn = Callable[[dict[str, Any]], None]


def emit_via_stdlib_logger(logger: logging.Logger) -> EmitFn:
    """Build an emit function that writes via the stdlib ``logging`` API.

    Used by ``ingestion.worker.IngestionWorker.process_event``.  The
    framework's structlog stdlib bridge (``configure_structlog(
    stdlib_bridge=True)``, used by the worker entrypoint) picks up the
    ``extra=`` kwargs and renders them as JSON fields.
    """

    def _emit(payload: dict[str, Any]) -> None:
        # Pop ``event`` so it doesn't clash with logging's positional
        # message; pass it as the message instead so it appears under
        # the standard "message"/"event" key in the structured output.
        event = payload.pop("event", EVENT_NAME)
        logger.info(event, extra={"telemetry_event": event, **payload})

    return _emit


def emit_via_structlog_logger(logger: Any) -> EmitFn:
    """Build an emit function that writes via a ``structlog.BoundLogger``.

    Used by ``scripts/reingest_from_s3.py`` which calls
    ``structlog.get_logger()``.  structlog's API takes the event as
    the first positional arg and **kwargs for context fields.
    """

    def _emit(payload: dict[str, Any]) -> None:
        event = payload.pop("event", EVENT_NAME)
        logger.info(event, **payload)

    return _emit


class DocTiming:
    """Per-document phase timer.

    The timer accumulates time across multiple calls to :meth:`phase`
    for the same phase name (re-entry is supported and additive —
    useful when a single document produces multiple split rulings that
    each go through validation + db_write).

    Attributes are intentionally minimal so the helper stays cheap in
    the hot path.  Callers that don't want a log line (e.g. unit tests
    on unrelated code paths) should not instantiate ``DocTiming`` at
    all — this class is the explicit instrumentation hook, not a
    background metric.
    """

    __slots__ = ("_buckets_ms", "_extra", "_emit_fn", "_emitted")

    def __init__(self, emit_fn: EmitFn, **extra: Any) -> None:
        """Create a timer.

        Args:
            emit_fn: Callable invoked once with the final payload dict
                on context-manager exit (or on the first manual
                :meth:`emit`).  Use :func:`emit_via_stdlib_logger` or
                :func:`emit_via_structlog_logger` to bind it to a
                logger.
            **extra: Free-form fields merged into the payload (e.g.
                ``document_id``, ``county``, ``scraper_id``).  All
                four phase buckets initialize to ``0`` so the log
                line is rectangular even for documents that exited
                early (e.g. deterministic-validation FAIL with no DB
                write).
        """
        self._buckets_ms: dict[str, float] = dict.fromkeys(PHASE_NAMES, 0.0)
        self._extra = dict(extra)
        self._emit_fn = emit_fn
        self._emitted = False

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a phase.  Re-entering the same name accumulates."""
        self._validate_phase(name)
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._buckets_ms[name] += elapsed_ms

    def add_ms(self, name: str, elapsed_ms: float) -> None:
        """Add ``elapsed_ms`` to a phase bucket without using a context manager.

        Useful when re-indenting a long block under ``with timing.phase(...)``
        would create a 100+-line diff or fight an existing ``try/except`` —
        the caller takes a ``time.perf_counter()`` reading at the boundaries
        and hands the delta to this helper.  The bucket-name validation and
        accumulation semantics are identical to :meth:`phase`.
        """
        self._validate_phase(name)
        self._buckets_ms[name] += elapsed_ms

    def _validate_phase(self, name: str) -> None:
        if name not in self._buckets_ms:
            raise ValueError(f"Unknown phase {name!r}. Valid phases: {PHASE_NAMES}")

    def emit(self) -> None:
        """Emit the structured log line.

        Idempotent — calling more than once is a no-op so the
        ``__exit__`` path can rely on ``emit()`` even when callers
        also invoke it manually for early-return paths (e.g.
        deterministic-validation FAIL skips the DB write but still
        wants the partial timing record).
        """
        if self._emitted:
            return
        self._emitted = True
        rounded = {name: round(self._buckets_ms[name]) for name in PHASE_NAMES}
        total_ms = sum(rounded.values())
        payload: dict[str, Any] = {
            "event": EVENT_NAME,
            **self._extra,
            **rounded,
            "total_ms": total_ms,
        }
        self._emit_fn(payload)

    def __enter__(self) -> DocTiming:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.emit()
