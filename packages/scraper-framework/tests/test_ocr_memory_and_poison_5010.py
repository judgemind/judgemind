"""Regression tests for the #5010 live-worker OOM.

A live capture of a 25-page San Bernardino scan made the 512 MB live
ingestion worker OCR it.  OCR rendered every page before reading any, and
MuPDF kept every decoded page image in its store, so the worker was
OOM-killed.  The message was redelivered on every restart, which stalled
live ingestion.

- OCR renders one page at a time, only when a worker thread is free, and
  empties MuPDF's store after each page (``llm_extract._render_page``).
- A reclaimed message that has already been delivered more than
  ``MAX_MESSAGE_DELIVERIES`` times is dead-lettered without being processed
  again. It is recorded ``dead_letter`` (#5008) so the reconciler retries it.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf

from ingestion import llm_extract
from ingestion import worker as worker_mod
from ingestion.llm_providers import LLMResponse
from ingestion.worker import MAX_MESSAGE_DELIVERIES, IngestionWorker


def _scanned_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    for _ in range(pages):
        page = doc.new_page(width=200, height=200)
        page.insert_image(pymupdf.Rect(10, 10, 190, 190), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def test_ocr_renders_pages_one_at_a_time_as_workers_free_up() -> None:
    """At most *concurrency* page images exist at once: a page is rendered
    only when a worker thread is free, not the whole scan up front."""
    lock = threading.Lock()
    state = {"rendered": 0, "answered": 0, "max_alive": 0}
    real_render = llm_extract._render_page

    def render(doc: Any, index: int) -> bytes:
        with lock:
            state["rendered"] += 1
            alive = state["rendered"] - state["answered"]
            state["max_alive"] = max(state["max_alive"], alive)
        return real_render(doc, index)

    def call(**_kwargs: Any) -> LLMResponse:
        threading.Event().wait(0.02)
        with lock:
            state["answered"] += 1
        return LLMResponse(text="page text " * 5, input_tokens=1, output_tokens=1)

    with (
        patch.object(llm_extract, "_render_page", side_effect=render),
        patch.object(llm_extract, "call_llm_with_images", side_effect=call),
    ):
        result = llm_extract.ocr_pdf_document(_scanned_pdf(9), concurrency=2)

    assert result is not None
    assert result.incomplete is None
    assert state["rendered"] == 9
    assert state["max_alive"] <= 2


def test_render_page_empties_mupdf_store_after_each_page() -> None:
    with (
        patch.object(pymupdf.TOOLS, "store_shrink") as shrink,
        patch.object(
            llm_extract,
            "call_llm_with_images",
            return_value=LLMResponse(text="", input_tokens=1, output_tokens=0),
        ),
    ):
        llm_extract.ocr_pdf_document(_scanned_pdf(3), concurrency=1)
    assert shrink.call_count == 3
    shrink.assert_called_with(100)


def test_live_scanned_page_reread_is_on() -> None:
    assert worker_mod.LIVE_SCANNED_PAGE_REREAD is True


# ---------------------------------------------------------------------------
# Poison messages
# ---------------------------------------------------------------------------


def _worker() -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker.process_event = MagicMock()  # type: ignore[method-assign]
    worker._record_dead_letter = MagicMock()  # type: ignore[method-assign]
    return worker


def _claim(worker: IngestionWorker, deliveries: object) -> dict[str, Any]:
    event = {"document_id": "b924f0a8", "s3_key": "ca/san_bernardino/x.pdf"}
    worker._redis.xautoclaim.return_value = (
        b"0-0",
        [(b"7-0", {b"data": json.dumps(event).encode()})],
        [],
    )
    worker._redis.xpending_range.return_value = deliveries
    return event


def test_poison_message_is_dead_lettered_unprocessed() -> None:
    worker = _worker()
    event = _claim(worker, [{"message_id": b"7-0", "times_delivered": MAX_MESSAGE_DELIVERIES + 1}])

    assert worker._reclaim_pending_messages() == 0

    worker.process_event.assert_not_called()
    worker._record_dead_letter.assert_called_once()
    args, kwargs = worker._record_dead_letter.call_args
    assert args[0] == event
    assert kwargs["attempts"] == MAX_MESSAGE_DELIVERIES + 1
    worker._redis.xack.assert_called_once_with("document.captured", "ingestion-workers", b"7-0")
    worker._redis.xpending_range.assert_called_once_with(
        "document.captured", "ingestion-workers", min=b"7-0", max=b"7-0", count=1
    )


def test_redelivered_message_under_the_cap_is_processed() -> None:
    worker = _worker()
    _claim(worker, [{"message_id": b"7-0", "times_delivered": MAX_MESSAGE_DELIVERIES}])
    assert worker._reclaim_pending_messages() == 1
    worker.process_event.assert_called_once()
    worker._record_dead_letter.assert_not_called()


def test_unknown_delivery_count_is_processed() -> None:
    for deliveries in ([], None, [{"times_delivered": "x"}]):
        worker = _worker()
        _claim(worker, deliveries)
        assert worker._reclaim_pending_messages() == 1
    worker = _worker()
    _claim(worker, [])
    worker._redis.xpending_range.side_effect = RuntimeError("redis down")
    assert worker._reclaim_pending_messages() == 1


def test_malformed_poison_message_is_still_acked() -> None:
    worker = _worker()
    worker._redis.xautoclaim.return_value = (b"0-0", [(b"8-0", {b"data": b"not json"})], [])
    worker._redis.xpending_range.return_value = [{"times_delivered": 9}]
    assert worker._reclaim_pending_messages() == 0
    worker._redis.xack.assert_called_once()
    assert worker._record_dead_letter.call_args.args[0] is None
