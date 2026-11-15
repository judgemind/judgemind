"""Unit tests for a scraper pre-split child's case-number evidence (#4976).

``IngestionWorker._with_pre_split_evidence`` reads the page a Contra Costa /
Los Angeles scraper archived and gives the child the same
``CASE_NUMBER_EVIDENCE_KEY`` the worker's own LLM split would.  The
end-to-end gate behaviour is pinned in ``test_split_write_pg.py``
(``-k presplit``) and the split harness.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from framework.models import ContentFormat
from ingestion.case_evidence import CASE_NUMBER_EVIDENCE_KEY
from ingestion.llm_extract import PdfText
from ingestion.worker import IngestionWorker, as_pre_split_child, event_content_format

PAGE = "Case No. 23STCV12345 Alder v. Acme Corp. " + "The motion is GRANTED. " * 20


def _worker(objects: dict[str, bytes]) -> IngestionWorker:
    s3 = MagicMock()
    s3.get_object.side_effect = lambda *, Bucket, Key: {"Body": io.BytesIO(objects[Key])}  # noqa: N803
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


@pytest.mark.parametrize("content_format", ["text", "txt"])
def test_text_page_is_the_evidence_under_either_spelling(content_format: str) -> None:
    """A live capture's ``.txt`` raw (``'text'``) and a pre-#5026 reingest's
    (``'txt'``) are both read as the evidence page (#5026)."""
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


def test_unreadable_or_textless_page_leaves_the_event() -> None:
    worker = _worker({"ca/x/raw/a.docx": b"PK..."})
    missing = _child(key="ca/x/raw/missing.html")
    assert worker._with_pre_split_evidence(missing) is missing
    no_key = _child(s3_key=None)
    assert worker._with_pre_split_evidence(no_key) is no_key
    docx = _child(key="ca/x/raw/a.docx", content_format="docx")
    assert worker._with_pre_split_evidence(docx) is docx


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
