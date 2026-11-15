"""Unit tests for a scraper pre-split child's case-number evidence (#4976).

``IngestionWorker._with_pre_split_evidence`` reads the page a Contra Costa /
Los Angeles scraper archived and gives the child the same
``CASE_NUMBER_EVIDENCE_KEY`` the worker's own LLM split would.  A page that
cannot be read fails closed (#5012).  The end-to-end gate behaviour is
pinned in ``test_split_write_pg.py`` (``-k presplit``) and the split
harness.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from framework.models import ContentFormat
from ingestion.case_evidence import CASE_NUMBER_EVIDENCE_KEY
from ingestion.ingest_incomplete import INCOMPLETE_EVIDENCE_UNAVAILABLE
from ingestion.llm_extract import PdfText
from ingestion.worker import (
    REPLACE_SPLIT_SET_KEY,
    IngestionWorker,
    PreSplitEvidenceUnavailableError,
    _case_number_evidence,
    as_pre_split_child,
    event_content_format,
)

PAGE = "Case No. 23STCV12345 Alder v. Acme Corp. " + "The motion is GRANTED. " * 20


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


def _worker(objects: dict[str, bytes], *failures: Exception) -> IngestionWorker:
    """A worker whose S3 serves *objects*; its first ``get_object`` calls
    raise *failures*, in order.  A missing key raises ``NoSuchKey``."""
    pending = list(failures)

    def get_object(*, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 — boto3's names
        if pending:
            raise pending.pop(0)
        if Key not in objects:
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(objects[Key])}

    s3 = MagicMock()
    s3.get_object.side_effect = get_object
    return IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=s3,
        archive_bucket="test-bucket",
    )


def _child(key: str = "ca/x/raw/a.html", count: int = 2, **overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "content_hash": "ab" * 32,
        "content_format": "html",
        "s3_key": key,
        "s3_bucket": "bucket",
        "case_number": "2025-00912345",
        "ruling_text": "The LLM's transcription of the ruling.",
        "extra": {
            "_llm_extracted": True,
            "pre_split": True,
            "split_position": 0,
            "split_count": count,
            "link_text": "Dept 9 rulings",
        },
    }
    event.update(overrides)
    return as_pre_split_child(event)


def test_html_page_is_the_evidence() -> None:
    worker = _worker({"ca/x/raw/a.html": PAGE.encode()})
    out = worker._with_pre_split_evidence(_child())
    assert out[CASE_NUMBER_EVIDENCE_KEY] == (PAGE,)


@pytest.mark.parametrize("content_format", [ContentFormat.TEXT.value, "txt"])
def test_text_format_page_is_the_evidence_under_either_spelling(content_format: str) -> None:
    """A live capture's ``.txt`` raw (``'text'``, ``ContentFormat.TEXT``)
    and a pre-#5026 reingest's (``'txt'``) are both read as the evidence
    page (#5026, #5012)."""
    worker = _worker({"ca/x/raw/a.txt": PAGE.encode()})
    out = worker._with_pre_split_evidence(
        _child(key="ca/x/raw/a.txt", content_format=content_format)
    )
    assert out[CASE_NUMBER_EVIDENCE_KEY] == (PAGE,)


def test_split_into_one_ruling_adds_the_link_text() -> None:
    worker = _worker({"ca/x/raw/a.html": PAGE.encode()})
    out = worker._with_pre_split_evidence(_child(count=1))
    assert out[CASE_NUMBER_EVIDENCE_KEY] == (PAGE, "Dept 9 rulings")


def test_pdf_page_text_is_extracted() -> None:
    worker = _worker({"ca/x/raw/a.pdf": b"%PDF-1.4 bytes"})
    with patch("ingestion.worker.extract_pdf_text", return_value=PdfText(PAGE)) as extract:
        out = worker._with_pre_split_evidence(_child(key="ca/x/raw/a.pdf", content_format="pdf"))
    # The text layer only: OCR text is never evidence (#4986).
    extract.assert_called_once_with(b"%PDF-1.4 bytes", ocr=False)
    assert out[CASE_NUMBER_EVIDENCE_KEY] == (PAGE,)


def test_scanned_pdf_page_gives_empty_evidence() -> None:
    """A page with no text layer is a scan: no evidence, and no OCR run
    (#4986)."""
    worker = _worker({"ca/x/raw/a.pdf": b"%PDF-1.4 bytes"})
    with patch("ingestion.worker.extract_pdf_text", return_value=None):
        out = worker._with_pre_split_evidence(_child(key="ca/x/raw/a.pdf", content_format="pdf"))
    assert out[CASE_NUMBER_EVIDENCE_KEY] == ()


def test_short_page_gives_empty_evidence() -> None:
    worker = _worker({"ca/x/raw/a.html": b"Page 1"})
    out = worker._with_pre_split_evidence(_child())
    assert out[CASE_NUMBER_EVIDENCE_KEY] == ()


def test_textless_page_gives_empty_evidence() -> None:
    """No key, or a format with no text: the child gets empty evidence, so
    its LLM number is judged like a scanned page's, never trusted as a
    scraper's (#5012).  Nothing is read."""
    worker = _worker({"ca/x/raw/a.docx": b"PK..."})
    no_key = worker._with_pre_split_evidence(_child(s3_key=None))
    assert no_key[CASE_NUMBER_EVIDENCE_KEY] == ()
    docx = worker._with_pre_split_evidence(_child(key="ca/x/raw/a.docx", content_format="docx"))
    assert docx[CASE_NUMBER_EVIDENCE_KEY] == ()
    worker._s3_client.get_object.assert_not_called()


def test_corrupt_pdf_page_gives_empty_evidence() -> None:
    """A page read but whose text cannot be extracted gives none, as a scan
    does: the same bytes fail every time."""
    worker = _worker({"ca/x/raw/a.pdf": b"%PDF-1.4 bytes"})
    with patch("ingestion.worker.extract_pdf_text", side_effect=ValueError("bad xref")):
        out = worker._with_pre_split_evidence(_child(key="ca/x/raw/a.pdf", content_format="pdf"))
    assert out[CASE_NUMBER_EVIDENCE_KEY] == ()


# -- an unreadable page fails closed (#5012) --------------------------------


@pytest.fixture
def sleeps() -> Any:
    with patch("ingestion.worker.time.sleep") as sleep:
        yield sleep


@pytest.mark.parametrize(
    "error",
    [_client_error("NoSuchKey"), _client_error("AccessDenied"), _client_error("NoSuchBucket")],
    ids=["no_such_key", "access_denied", "no_such_bucket"],
)
def test_read_fails_permanently_refuses_and_records(error: Exception, sleeps: Any) -> None:
    """A read a retry cannot fix: no retry, the key is recorded
    ``evidence_unavailable`` for the reconciler, and the child is refused."""
    worker = _worker({}, error)
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    child = _child()
    with pytest.raises(PreSplitEvidenceUnavailableError) as raised:
        worker._with_pre_split_evidence(child)
    assert raised.value.permanent is True
    assert worker._s3_client.get_object.call_count == 1
    sleeps.assert_not_called()
    worker._record_incomplete_and_commit.assert_called_once()
    args, kwargs = worker._record_incomplete_and_commit.call_args
    assert args[0] is child
    assert args[1] == INCOMPLETE_EVIDENCE_UNAVAILABLE
    assert args[2]["transient"] is False
    assert kwargs == {"rollback_first": True}
    # A failed read is not cached: the next sibling reads the page again.
    assert worker._page_text_cache == {}


def test_read_fails_on_a_body_that_is_not_bytes_is_permanent(sleeps: Any) -> None:
    worker = _worker({})
    worker._s3_client.get_object.side_effect = lambda **_k: {"Body": io.StringIO("text")}
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(PreSplitEvidenceUnavailableError) as raised:
        worker._with_pre_split_evidence(_child())
    assert raised.value.permanent is True


@pytest.mark.parametrize(
    "error",
    [
        _client_error("SlowDown"),
        _client_error("ServiceUnavailable"),
        _client_error("InternalError"),
        EndpointConnectionError(endpoint_url="https://s3.us-west-2.amazonaws.com"),
        TimeoutError("read timed out"),
    ],
    ids=["throttled", "unavailable", "internal", "connection", "timeout"],
)
def test_read_fails_transiently_on_a_live_capture_retries_then_refuses(
    error: Exception, sleeps: Any
) -> None:
    """Transient errors are retried within the read; a read that keeps
    failing refuses the child without a record (the live worker retries
    the message, and a dead-letter records the key)."""
    worker = _worker({"ca/x/raw/a.html": PAGE.encode()}, *([error] * 4))
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(PreSplitEvidenceUnavailableError) as raised:
        worker._with_pre_split_evidence(_child())
    assert raised.value.permanent is False
    assert not isinstance(raised.value, OSError)  # not an infrastructure exit
    assert worker._s3_client.get_object.call_count == 4
    assert [c.args[0] for c in sleeps.call_args_list] == [0.5, 1.0, 2.0]
    worker._record_incomplete_and_commit.assert_not_called()


def test_read_fails_transiently_on_a_split_set_replacement_records(sleeps: Any) -> None:
    """A reingest / rebuild has no message retry: the key is recorded."""
    worker = _worker({}, *([_client_error("SlowDown")] * 4))
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(PreSplitEvidenceUnavailableError) as raised:
        worker._with_pre_split_evidence(_child(**{REPLACE_SPLIT_SET_KEY: True}))
    assert raised.value.permanent is False
    args, _kwargs = worker._record_incomplete_and_commit.call_args
    assert args[1] == INCOMPLETE_EVIDENCE_UNAVAILABLE
    assert args[2]["transient"] is True
    assert args[2]["replace_set"] is True


def test_transient_error_then_success_gives_normal_evidence(sleeps: Any) -> None:
    worker = _worker({"ca/x/raw/a.html": PAGE.encode()}, _client_error("SlowDown"))
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    out = worker._with_pre_split_evidence(_child())
    assert out[CASE_NUMBER_EVIDENCE_KEY] == (PAGE,)
    assert worker._s3_client.get_object.call_count == 2
    sleeps.assert_called_once_with(0.5)
    worker._record_incomplete_and_commit.assert_not_called()


def test_read_fails_never_leaves_the_llm_number_trusted(sleeps: Any) -> None:
    """The #5012 regression: an S3 error used to return the event without
    ``CASE_NUMBER_EVIDENCE_KEY``, so the gate trusted the LLM's number
    (``trusted = CASE_NUMBER_EVIDENCE_KEY not in event_data``).  Every read
    error now either refuses the child or, for a page with no text, marks
    the number as the LLM's with empty evidence."""
    for error in (_client_error("NoSuchKey"), _client_error("SlowDown")):
        worker = _worker({}, *([error] * 4))
        worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
        child = _child()
        try:
            out = worker._with_pre_split_evidence(child)
        except PreSplitEvidenceUnavailableError:
            continue
        assert CASE_NUMBER_EVIDENCE_KEY in out, "the LLM's number would be trusted"
        assert _case_number_evidence(out, out.get("ruling_text")) == ()


def _live_worker(error: Exception) -> IngestionWorker:
    worker = _worker({}, *([error] * 100))
    worker._ack = MagicMock()  # type: ignore[method-assign]
    worker._record_incomplete_and_commit = MagicMock()  # type: ignore[method-assign]
    worker.process_event = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda event: worker._with_pre_split_evidence(event)
    )
    return worker


def test_read_fails_permanently_live_message_is_acked_without_retry(sleeps: Any) -> None:
    """The ``evidence_unavailable`` record stands: no retry, and no
    dead-letter record to overwrite it."""
    worker = _live_worker(_client_error("NoSuchKey"))
    worker._process_event_data(b"1-0", _child())
    assert worker.process_event.call_count == 1
    worker._ack.assert_called_once_with(b"1-0")
    reasons = [c.args[1] for c in worker._record_incomplete_and_commit.call_args_list]
    assert reasons == [INCOMPLETE_EVIDENCE_UNAVAILABLE]


def test_read_fails_transiently_live_message_is_retried_then_dead_lettered(sleeps: Any) -> None:
    worker = _live_worker(_client_error("ServiceUnavailable"))
    worker._process_event_data(b"1-0", _child())
    assert worker.process_event.call_count == worker._max_retries
    worker._ack.assert_called_once_with(b"1-0")
    reasons = [c.args[1] for c in worker._record_incomplete_and_commit.call_args_list]
    assert reasons == ["dead_letter"]


def test_other_events_are_unchanged() -> None:
    worker = _worker({"ca/x/raw/a.html": PAGE.encode()})
    whole = {"document_id": "d", "s3_key": "ca/x/raw/a.html", "content_format": "html"}
    assert worker._with_pre_split_evidence(whole) is whole
    not_llm = _child()
    not_llm["extra"] = {**not_llm["extra"], "_llm_extracted": False}
    assert worker._with_pre_split_evidence(not_llm) is not_llm
    carried = {**_child(), CASE_NUMBER_EVIDENCE_KEY: ("x",)}
    assert worker._with_pre_split_evidence(carried) is carried
    worker._s3_client.get_object.assert_not_called()


def test_page_is_read_once_per_key_and_the_cache_is_bounded() -> None:
    keys = [f"ca/x/raw/{i}.html" for i in range(10)]
    worker = _worker({k: PAGE.encode() for k in keys})
    for _ in range(3):
        worker._with_pre_split_evidence(_child(key=keys[0]))
    assert worker._s3_client.get_object.call_count == 1
    for k in keys:
        worker._with_pre_split_evidence(_child(key=k))
    assert len(worker._page_text_cache) == 8
    assert ("bucket", keys[0]) not in worker._page_text_cache


def test_event_content_format_accepts_both_text_spellings() -> None:
    """The worker's single parse of an event's format (#5026)."""
    assert event_content_format({"content_format": "text"}) is ContentFormat.TEXT
    assert event_content_format({"content_format": "txt"}) is ContentFormat.TEXT
    assert event_content_format({"content_format": "pdf"}) is ContentFormat.PDF
    assert event_content_format({"content_format": "rtf"}) is None
    assert event_content_format({}) is None
