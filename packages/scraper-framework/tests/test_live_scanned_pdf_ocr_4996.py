"""Regression tests for #4996: a live capture of a fully scanned PDF in a
text-first county.

Before #5010 a scraper read a PDF with pdfplumber and sent the text as
``ruling_text``.  For a PDF with no text layer on any page that text was
empty, so the worker never saw PDF bytes, never OCR'd, ran no extractor
(``NO_EXTRACTABLE_TEXT``) and wrote one empty document that validation
dropped.  Only a reingest, which sends the raw bytes, recovered the rulings.

Now the scraper's shared ``scraper_pdf_text`` names every page of such a PDF
in ``extra[SCANNED_PAGES_KEY]`` (``_scanned_page_indexes``: no text layer on
any page means every page is scanned), and the live worker reads the archived
PDF again and OCRs it (``_with_scanned_pages_reread``,
``LIVE_SCANNED_PAGE_REREAD``).  These tests drive the whole live path, from
the scraper's ``parse_document`` through the emitted ``document.captured``
payload to the worker's split, for the fully scanned case: the #5010 tests
cover a text PDF with one scanned page.
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from courts.ca import pdf_link_scraper as pls
from framework import CapturedDocument, ContentFormat
from framework.events import EventBus
from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from ingestion.case_evidence import CASE_NUMBER_EVIDENCE_KEY, TEXT_FROM_OCR_KEY
from ingestion.llm_extract import SCANNED_PAGES_KEY
from ingestion.llm_providers import LLMResponse
from ingestion.worker import (
    INCOMPLETE_SINGLE_DOCUMENT_FALLBACK,
    NO_EXTRACTABLE_TEXT,
    OCR_INCOMPLETE,
    IngestionWorker,
)

PAGES = 3
CASES = {1: "CIVSB2500101", 2: "CIVSB2500202", 3: "CIVSB2500303"}
S3_KEY = "ca/san_bernardino/superior_court/raw/4996.pdf"


def _ocr_page_text(page: int) -> str:
    return (
        f"Case No.: {CASES[page]} Alder v. Acme Corp. {page}\n"
        "The motion to compel further responses is GRANTED."
    )


def _picture(page: Any) -> None:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(20, 20, 592, 772), pixmap=pix)


@pytest.fixture(scope="module")
def scanned_pdf() -> bytes:
    """A PDF with no text layer on any page: every page is a picture."""
    doc = pymupdf.open()
    for _ in range(PAGES):
        _picture(doc.new_page(width=612, height=792))
    data = doc.tobytes()
    doc.close()
    return data


def _page_number(text_message: str) -> int:
    match = re.search(r"\(page (\d+)\)", text_message)
    assert match, text_message
    return int(match.group(1))


def _ocr(fail_pages: frozenset[int] = frozenset()) -> MagicMock:
    """``call_llm_with_images``: each page's ruling, ``None`` for *fail_pages*."""

    def call(*, system_prompt: str, text_message: str, images: Any, **_: Any) -> Any:
        page = _page_number(text_message)
        if page in fail_pages:
            return None
        return LLMResponse(text=_ocr_page_text(page), input_tokens=10, output_tokens=10)

    return MagicMock(side_effect=call)


def _live_event(scanned_pdf: bytes) -> dict[str, Any]:
    """The ``document.captured`` payload a San Bernardino live capture of
    *scanned_pdf* emits: the scraper's own parse, serialised as the
    ``EventBus`` sends it."""
    scraper = pls.PdfLinkScraper.__new__(pls.PdfLinkScraper)
    scraper._pdf_config = pls.PdfLinkConfig(
        index_url="https://example.test",
        pdf_base_url="https://example.test",
        link_text_re=re.compile(r"(?P<department>S\d+)"),
        case_number_re=re.compile(r"CIVSB\d{7}"),
    )
    scraper._log = MagicMock()
    doc = CapturedDocument(
        scraper_id="ca-san-bernardino-tentatives",
        state="CA",
        county="San Bernardino",
        court="Superior Court",
        source_url="https://example.test/CVS24110125.pdf",
        content_format=ContentFormat.PDF,
        raw_content=scanned_pdf,
        capture_timestamp=datetime(2026, 11, 1, 18, tzinfo=UTC),
        content_hash="4996" * 16,
        s3_key=S3_KEY,
        s3_bucket="judgemind-document-archive-dev",
    )
    scraper.parse_document(doc)

    redis = MagicMock()
    redis.xlen.return_value = 0
    EventBus(redis, maxlen=10).emit_document_captured(doc, producer_id="test")
    payload: dict[str, Any] = json.loads(redis.xadd.call_args.args[1]["data"])
    return payload


def _worker(scanned_pdf: bytes) -> tuple[IngestionWorker, MagicMock]:
    s3 = MagicMock()
    s3.get_object.side_effect = lambda **_k: {"Body": io.BytesIO(scanned_pdf)}
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=s3,
        archive_bucket="judgemind-document-archive-dev",
    )
    worker._enrichment_client = None
    text_extractor = MagicMock()
    text_extractor.extract.return_value = [
        ExtractedRuling(
            extracted_case_number=CASES[page],
            extracted_case_title=f"Alder v. Acme Corp. {page}",
            ruling_text="The motion to compel further responses is GRANTED.",
            outcome=ExtractionOutcome.GRANTED,
        )
        for page in sorted(CASES)
    ]
    multimodal = MagicMock()
    multimodal.extract_from_pdf.side_effect = AssertionError("San Bernardino is text-first")
    worker._get_framework_extractor = lambda: text_extractor  # type: ignore[method-assign]
    worker._get_county_extractor = lambda *a, **k: text_extractor  # type: ignore[method-assign]
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    worker._get_connection = MagicMock()  # type: ignore[method-assign]
    return worker, text_extractor


def test_live_scanned_pdf_ocr_scraper_marks_every_page(scanned_pdf: bytes) -> None:
    """The live event carries no text and names every page as scanned."""
    event = _live_event(scanned_pdf)
    assert event["ruling_text"] == ""
    assert event["content_format"] == "pdf"
    assert event["s3_key"] == S3_KEY
    assert event["extra"][SCANNED_PAGES_KEY] == list(range(1, PAGES + 1))
    assert event.get("_replace_split_set") is None  # a live capture


@patch("ingestion.worker.insert_validation_result")
def test_live_scanned_pdf_ocr_is_split_like_a_reingest(
    mock_validation: MagicMock, scanned_pdf: bytes
) -> None:
    worker, text_extractor = _worker(scanned_pdf)
    event = _live_event(scanned_pdf)
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr,
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        patch.object(IngestionWorker, "_leave_key_after_failed_extraction") as failed,
    ):
        worker.process_event(event)

    # The archived PDF was read once and every page OCR'd.
    worker._s3_client.get_object.assert_called_once()
    assert worker._s3_client.get_object.call_args.kwargs["Key"] == S3_KEY
    assert sorted(_page_number(c.kwargs["text_message"]) for c in ocr.call_args_list) == [1, 2, 3]
    # The text-first split read the OCR text (never NO_EXTRACTABLE_TEXT).
    failed.assert_not_called()
    text_extractor.extract.assert_called_once()
    split_text = text_extractor.extract.call_args.args[0]
    for page in CASES:
        assert _ocr_page_text(page) in split_text
    # Written as the split's children, not as one whole document.
    write_batch.assert_called_once()
    children = write_batch.call_args.args[3].children
    assert sorted(c.case_number for c in children) == sorted(CASES.values())
    for child in children:
        # OCR text is LLM output: never case-number evidence (#4986).
        assert child.event_data[TEXT_FROM_OCR_KEY] is True
        assert child.event_data[CASE_NUMBER_EVIDENCE_KEY] == ()


@patch("ingestion.worker.insert_validation_result")
def test_live_scanned_pdf_ocr_incomplete_is_recorded_for_retry(
    mock_validation: MagicMock, scanned_pdf: bytes
) -> None:
    """A page OCR could not read: no split is built on the partial text, and
    the key is recorded in ``telemetry.ingest_incomplete`` so the raw-key
    reconciler re-ingests it (#5008)."""
    worker, text_extractor = _worker(scanned_pdf)
    event = _live_event(scanned_pdf)
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr(frozenset({2}))),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        patch.object(IngestionWorker, "_key_has_rows", return_value=False),
        patch.object(IngestionWorker, "_record_incomplete_and_commit") as record,
    ):
        worker.process_event(event)

    text_extractor.extract.assert_not_called()
    write_batch.assert_not_called()
    record.assert_called_once()
    recorded_event, reason, detail = record.call_args.args
    assert recorded_event["s3_key"] == S3_KEY
    assert reason == INCOMPLETE_SINGLE_DOCUMENT_FALLBACK
    assert OCR_INCOMPLETE in detail["error"]
    assert "page(s) [2] failed" in detail["error"]
    assert NO_EXTRACTABLE_TEXT not in detail["error"]
