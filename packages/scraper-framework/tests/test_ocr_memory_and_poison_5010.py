"""Regression tests for the #5010 live-worker OOM.

A live capture of a 25-page San Bernardino scan made the 512 MB live
ingestion worker OCR it.  OCR rendered every page before reading any, and
MuPDF kept every decoded page image in its store, so the worker was
OOM-killed.  The message was redelivered on every restart, which stalled
live ingestion.

- OCR renders one page at a time, only when a worker thread is free, and
  empties MuPDF's store after each page (``llm_extract._render_page``).
- A message the worker already died processing ``MAX_MESSAGE_CRASHES`` times
  is dead-lettered without being processed again. It is recorded
  ``dead_letter`` (#5008) so the reconciler retries it.
- Only the message in flight when the worker dies is charged (#5055): not
  the messages claimed with it, and not messages pending through an
  ``InfrastructureError`` crash loop (#1044).
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pymupdf
import pytest

from ingestion import llm_extract
from ingestion import worker as worker_mod
from ingestion.llm_providers import LLMResponse
from ingestion.worker import (
    MAX_MESSAGE_CRASHES,
    PROCESSING_CRASHES_KEY_PREFIX,
    PROCESSING_CRASHES_TTL_S,
    InfrastructureError,
    IngestionWorker,
)


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


class _Killed(BaseException):
    """Stands in for the OOM killer's SIGKILL: the process stops mid-call."""


class _FakeRedis:
    """The stream calls the poison handling makes, against one consumer group
    whose pending list holds *events* (left by a consumer that died).

    ``kill()`` makes the process "dead": every later call is ignored, the way
    a SIGKILLed worker runs no cleanup.  ``restart()`` revives it, as ECS
    starting a fresh task against the same Redis."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.entries = {
            f"{i + 1}-0".encode(): {b"data": json.dumps(event).encode()}
            for i, event in enumerate(events)
        }
        self.pending = list(self.entries)
        self.kv: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.alive = True

    def kill(self) -> None:
        self.alive = False

    def restart(self) -> None:
        self.alive = True

    def xautoclaim(self, stream: str, group: str, consumer: str, **kw: Any) -> Any:
        claimed = self.pending[: kw["count"]]
        return (b"0-0", [(msg_id, self.entries[msg_id]) for msg_id in claimed], [])

    def xack(self, stream: str, group: str, msg_id: bytes) -> int:
        if not self.alive:
            return 0
        self.pending.remove(msg_id)
        return 1

    def incr(self, key: str) -> int | None:
        if not self.alive:
            return None
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    def decr(self, key: str) -> int | None:
        if not self.alive:
            return None
        self.kv[key] = self.kv.get(key, 0) - 1
        return self.kv[key]

    def expire(self, key: str, seconds: int) -> bool:
        if self.alive:
            self.ttls[key] = seconds
        return self.alive

    def delete(self, key: str) -> int:
        if not self.alive:
            return 0
        return 1 if self.kv.pop(key, None) is not None else 0


def _worker(redis_client: Any = None) -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=redis_client if redis_client is not None else MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker.process_event = MagicMock()  # type: ignore[method-assign]
    worker._record_dead_letter = MagicMock()  # type: ignore[method-assign]
    return worker


def _events(n: int) -> list[dict[str, Any]]:
    return [{"document_id": f"doc-{i}", "s3_key": f"ca/x/{i}.pdf"} for i in range(1, n + 1)]


def _key(msg_id: str) -> str:
    return f"{PROCESSING_CRASHES_KEY_PREFIX}{msg_id}"


def _killer(redis: _FakeRedis, document_id: str, processed: list[str]) -> Any:
    def process(event: dict[str, Any]) -> None:
        if event["document_id"] == document_id:
            redis.kill()
            raise _Killed
        processed.append(event["document_id"])

    return process


def test_poison_and_batch_mates_only_the_killer_is_dead_lettered() -> None:
    """3 restarts with a queue of 5 where message 1 kills the worker: on the
    4th restart only message 1 is dead-lettered and messages 2-5, claimed
    with it on every restart but never started, are processed (#5055)."""
    redis = _FakeRedis(_events(5))
    processed: list[str] = []

    for _ in range(MAX_MESSAGE_CRASHES):
        worker = _worker(redis)
        worker.process_event.side_effect = _killer(redis, "doc-1", processed)
        try:
            worker._reclaim_pending_messages()
        except _Killed:
            pass
        else:  # pragma: no cover - the killer always kills
            raise AssertionError("message 1 should have killed the worker")
        redis.restart()

    assert redis.kv == {_key("1-0"): MAX_MESSAGE_CRASHES}
    assert processed == []

    worker = _worker(redis)
    worker.process_event.side_effect = _killer(redis, "doc-1", processed)
    assert worker._reclaim_pending_messages() == 4

    assert processed == ["doc-2", "doc-3", "doc-4", "doc-5"]
    worker._record_dead_letter.assert_called_once()
    args, kwargs = worker._record_dead_letter.call_args
    assert args[0]["document_id"] == "doc-1"
    assert kwargs["attempts"] == MAX_MESSAGE_CRASHES
    assert redis.pending == []
    assert redis.kv == {}


def test_poison_in_mid_batch_charges_only_the_killer() -> None:
    """Messages before the killer complete on the first restart; the ones
    after it are never charged."""
    redis = _FakeRedis(_events(5))
    processed: list[str] = []
    for _ in range(MAX_MESSAGE_CRASHES):
        worker = _worker(redis)
        worker.process_event.side_effect = _killer(redis, "doc-3", processed)
        try:
            worker._reclaim_pending_messages()
        except _Killed:
            pass
        redis.restart()
    assert processed == ["doc-1", "doc-2"]
    assert redis.kv == {_key("3-0"): MAX_MESSAGE_CRASHES}

    worker = _worker(redis)
    worker.process_event.side_effect = _killer(redis, "doc-3", processed)
    assert worker._reclaim_pending_messages() == 2
    assert processed == ["doc-1", "doc-2", "doc-4", "doc-5"]
    assert worker._record_dead_letter.call_args.args[0]["document_id"] == "doc-3"


def test_infrastructure_error_restarts_never_dead_letter() -> None:
    """Pending messages left by repeated ``InfrastructureError`` exits are
    processed once the infrastructure recovers (#1044, #5055)."""
    redis = _FakeRedis(_events(5))
    for _ in range(MAX_MESSAGE_CRASHES + 3):
        worker = _worker(redis)
        worker.process_event.side_effect = psycopg.OperationalError("connection refused")
        with pytest.raises(InfrastructureError):
            worker._reclaim_pending_messages()
        assert redis.kv == {_key("1-0"): 0}

    worker = _worker(redis)
    assert worker._reclaim_pending_messages() == 5
    assert worker.process_event.call_count == 5
    worker._record_dead_letter.assert_not_called()
    assert redis.pending == []
    assert redis.kv == {}


def test_restart_loop_with_no_message_in_flight_charges_nothing() -> None:
    """DB down at startup: the worker exits before it starts any message, so
    no message is charged and none is dead-lettered."""
    redis = _FakeRedis(_events(5))
    for _ in range(MAX_MESSAGE_CRASHES + 3):
        worker = _worker(redis)
        worker.health_check = MagicMock(  # type: ignore[method-assign]
            side_effect=InfrastructureError(psycopg.OperationalError("db down"))
        )
        with pytest.raises(InfrastructureError):
            worker.run()
    assert redis.kv == {}

    worker = _worker(redis)
    assert worker._reclaim_pending_messages() == 5
    worker._record_dead_letter.assert_not_called()


def test_normal_processing_clears_the_marker() -> None:
    redis = _FakeRedis(_events(1))
    worker = _worker(redis)
    assert worker._process_message(b"1-0", redis.entries[b"1-0"]) is True
    worker.process_event.assert_called_once()
    assert redis.ttls == {_key("1-0"): PROCESSING_CRASHES_TTL_S}
    assert redis.kv == {}
    assert redis.pending == []


def test_message_level_dead_letter_clears_the_marker() -> None:
    redis = _FakeRedis(_events(1))
    worker = _worker(redis)
    worker.process_event.side_effect = ValueError("bad data")
    worker._process_message(b"1-0", redis.entries[b"1-0"])
    worker._record_dead_letter.assert_called_once()
    assert redis.kv == {}
    assert redis.pending == []


def test_keyboard_interrupt_mid_message_takes_the_charge_back() -> None:
    """A Python-level exit (Ctrl-C's KeyboardInterrupt) is not a crash on
    the message: the charge is taken back and the message stays pending."""
    redis = _FakeRedis(_events(1))
    worker = _worker(redis)
    worker.process_event.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        worker._process_message(b"1-0", redis.entries[b"1-0"])
    assert redis.kv == {_key("1-0"): 0}
    assert redis.pending == [b"1-0"]


def test_death_between_mark_and_start_counts_once() -> None:
    """Dying right after the mark, before processing starts, charges the
    message once, not twice."""
    redis = _FakeRedis(_events(1))
    worker = _worker(redis)

    def die_on_expire(key: str, seconds: int) -> bool:
        redis.kill()
        raise _Killed

    with patch.object(redis, "expire", side_effect=die_on_expire):
        with pytest.raises(_Killed):
            worker._reclaim_pending_messages()
    worker.process_event.assert_not_called()
    redis.restart()
    assert redis.kv == {_key("1-0"): 1}

    worker = _worker(redis)
    assert worker._reclaim_pending_messages() == 1
    worker.process_event.assert_called_once()
    assert redis.kv == {}


def test_poison_message_is_dead_lettered_unprocessed() -> None:
    redis = _FakeRedis(_events(1))
    redis.kv[_key("1-0")] = MAX_MESSAGE_CRASHES
    worker = _worker(redis)

    assert worker._reclaim_pending_messages() == 0

    worker.process_event.assert_not_called()
    worker._record_dead_letter.assert_called_once()
    args, kwargs = worker._record_dead_letter.call_args
    assert args[0] == _events(1)[0]
    assert kwargs["attempts"] == MAX_MESSAGE_CRASHES
    assert redis.pending == []
    assert redis.kv == {}


def test_message_under_the_cap_is_processed() -> None:
    redis = _FakeRedis(_events(1))
    redis.kv[_key("1-0")] = MAX_MESSAGE_CRASHES - 1
    worker = _worker(redis)
    assert worker._reclaim_pending_messages() == 1
    worker.process_event.assert_called_once()
    worker._record_dead_letter.assert_not_called()


def test_xautoclaim_delivery_count_is_ignored() -> None:
    """XPENDING's times_delivered (raised by every claim) plays no part."""
    worker = _worker()
    worker._redis.xautoclaim.return_value = (
        b"0-0",
        [(b"7-0", {b"data": json.dumps(_events(1)[0]).encode()})],
        [],
    )
    worker._redis.xpending_range.return_value = [{"times_delivered": 99}]
    worker._redis.incr.return_value = 1
    assert worker._reclaim_pending_messages() == 1
    worker.process_event.assert_called_once()
    worker._redis.xpending_range.assert_not_called()


def test_unknown_crash_count_is_processed() -> None:
    """A Redis failure on the mark, or a reply that is not a count, never
    dead-letters: the message is processed."""
    for incr in (RuntimeError("redis down"), None, b"x"):
        worker = _worker()
        worker._redis.xautoclaim.return_value = (
            b"0-0",
            [(b"7-0", {b"data": json.dumps(_events(1)[0]).encode()})],
            [],
        )
        if isinstance(incr, Exception):
            worker._redis.incr.side_effect = incr
        else:
            worker._redis.incr.return_value = incr
        assert worker._reclaim_pending_messages() == 1
        worker.process_event.assert_called_once()


def test_redis_failure_clearing_the_marker_is_logged_not_raised() -> None:
    worker = _worker()
    worker._redis.incr.return_value = 1
    worker._redis.delete.side_effect = RuntimeError("redis down")
    data = {b"data": json.dumps(_events(1)[0]).encode()}
    assert worker._process_message(b"7-0", data) is True
    worker._redis.xack.assert_called_once()

    worker = _worker()
    worker._redis.incr.return_value = 1
    worker._redis.decr.side_effect = RuntimeError("redis down")
    worker.process_event.side_effect = psycopg.OperationalError("db down")
    with pytest.raises(InfrastructureError):
        worker._process_message(b"7-0", data)


def test_malformed_message_is_acked_without_a_mark() -> None:
    redis = _FakeRedis([])
    redis.entries[b"8-0"] = {b"data": b"not json"}
    redis.pending.append(b"8-0")
    worker = _worker(redis)
    assert worker._reclaim_pending_messages() == 0
    assert redis.pending == []
    assert redis.kv == {}
    worker._record_dead_letter.assert_not_called()
