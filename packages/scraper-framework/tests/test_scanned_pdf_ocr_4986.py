"""Regression tests for #4986: scanned PDFs in text-first counties.

A PDF with no text layer is OCR'd by the LLM (``llm_extract.ocr_pdf_document``,
#1332).  Two bugs:

1. OCR stopped at page 10 (``_OCR_MAX_PAGES = 10``) and nothing marked the
   extraction incomplete, so a 22-page Santa Clara scan
   (``ca/santa_clara/superior_court/raw/434fd3ec...pdf``) lost every ruling
   past page 10.  OCR now reads every page up to a cap of 100, four pages at
   a time; a PDF that hits the cap, the time budget or a failed page call is
   ``incomplete``, and a text split built on it is a failed extraction.
2. The OCR text, LLM output, counted as case-number evidence once it reached
   200 characters.  ``PdfText.from_ocr`` now marks it, the worker sets
   ``TEXT_FROM_OCR_KEY`` on the event, and the gate gets no evidence from it:
   the scanned-PDF rules apply (the extractors' number is kept).

The fixtures are synthetic image-only PDFs built with pymupdf: every page is
a picture, so pdfplumber finds no text layer, exactly as on the real scan.
"""

from __future__ import annotations

import re
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from ingestion import llm_extract
from ingestion.case_evidence import (
    CASE_NUMBER_EVIDENCE_KEY,
    TEXT_FROM_OCR_KEY,
    document_evidence,
    own_text_evidence,
)
from ingestion.llm_extract import (
    PdfText,
    extract_pdf_text,
    extract_text_from_pdf,
    extract_text_layer,
    ocr_pdf_document,
    pdf_page_count,
)
from ingestion.llm_providers import LLMResponse
from ingestion.worker import (
    OCR_INCOMPLETE,
    OCR_INCOMPLETE_KEY,
    IngestionWorker,
    NoExtractableTextError,
    SplitExtractionFailedError,
    _case_number_evidence,
    _read_text_evidence,
)

RULING_PAGE = 15
RULING_CASE = "25CV150015"
RULING = f"Case No.: {RULING_CASE} Alder v. Acme Corp.\nThe motion for summary judgment is GRANTED."
#: A number the scan does not print: with the OCR text as evidence the gate
#: would refute it; with no evidence it is kept.
LLM_CASE = "25CV999999"


def _scanned_pdf(pages: int) -> bytes:
    """An image-only PDF of *pages* pages: no text layer at all."""
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    for _ in range(pages):
        page = doc.new_page(width=200, height=200)
        page.insert_image(pymupdf.Rect(10, 10, 190, 190), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def _page_text(page: int) -> str:
    if page == RULING_PAGE:
        return RULING
    return f"SUPERIOR COURT OF CALIFORNIA, COUNTY OF SANTA CLARA. Calendar page {page}, no matters."


def _page_number(text_message: str) -> int:
    match = re.search(r"\(page (\d+)\)", text_message)
    assert match, text_message
    return int(match.group(1))


def _ocr(fail_pages: frozenset[int] = frozenset(), delay: float = 0.0) -> MagicMock:
    """A stand-in for ``call_llm_with_images``: each page's text, ``None``
    (a failed call) for *fail_pages*."""

    def call(*, system_prompt: str, text_message: str, images: Any, **_: Any) -> Any:
        page = _page_number(text_message)
        if delay:
            threading.Event().wait(delay)  # conftest patches time.sleep
        if page in fail_pages:
            return None
        return LLMResponse(text=_page_text(page), input_tokens=10, output_tokens=10)

    return MagicMock(side_effect=call)


@pytest.fixture(scope="module")
def scan_22() -> bytes:
    return _scanned_pdf(22)


# ---------------------------------------------------------------------------
# OCR reads every page, in order, and says when it did not
# ---------------------------------------------------------------------------


def test_22_page_scan_is_ocrd_to_the_last_page(scan_22: bytes) -> None:
    ocr = _ocr()
    with patch("ingestion.llm_extract.call_llm_with_images", ocr):
        result = extract_pdf_text(scan_22)

    assert result is not None
    assert result.from_ocr
    assert result.incomplete is None
    assert result.page_count == 22
    assert ocr.call_count == 22
    assert RULING in result.text
    # Pages joined in page order whatever order the concurrent calls end in.
    pages = result.text.split("\n\f\n")
    assert len(pages) == 22
    assert pages[RULING_PAGE - 1] == RULING
    assert pages[-1] == _page_text(22)
    # The plain-text helper returns the same text.
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()):
        assert extract_text_from_pdf(scan_22) == result.text


def test_page_cap_marks_the_extraction_incomplete(scan_22: bytes) -> None:
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()):
        result = ocr_pdf_document(scan_22, max_pages=10)

    assert result is not None
    assert result.incomplete is not None
    assert "page cap 10 of 22 pages" in result.incomplete
    assert RULING not in result.text  # page 15 was never read


def test_default_cap_is_high_enough_for_real_calendars() -> None:
    assert llm_extract._OCR_MAX_PAGES >= 50


def test_failed_page_marks_the_extraction_incomplete(scan_22: bytes) -> None:
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr(frozenset({7}))):
        result = extract_pdf_text(scan_22)

    assert result is not None
    assert result.incomplete == "page(s) [7] failed"
    assert RULING in result.text


def test_blank_page_is_not_a_failure() -> None:
    blank = MagicMock(return_value=LLMResponse(text="  ", input_tokens=1, output_tokens=0))
    with patch("ingestion.llm_extract.call_llm_with_images", blank):
        result = ocr_pdf_document(_scanned_pdf(2))
    assert result == PdfText("", from_ocr=True, page_count=2, incomplete=None)
    # Nothing on any page: no text, as before.
    with patch("ingestion.llm_extract.call_llm_with_images", blank):
        assert extract_pdf_text(_scanned_pdf(2)) is None


def test_every_page_failing_is_reported_not_blank() -> None:
    """A transient outage must not look like a PDF with nothing on it (that
    is ``NO_EXTRACTABLE_TEXT``, never retried)."""
    with patch("ingestion.llm_extract.call_llm_with_images", MagicMock(return_value=None)):
        result = extract_pdf_text(_scanned_pdf(3))
    assert result is not None
    assert result.text == ""
    assert result.incomplete == "page(s) [1, 2, 3] failed"


def test_time_budget_marks_the_extraction_incomplete() -> None:
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr(delay=0.5)):
        result = ocr_pdf_document(_scanned_pdf(8), total_timeout=0.2, concurrency=2)
    assert result is not None
    assert result.incomplete is not None
    assert "unanswered after 0.2s" in result.incomplete


def test_total_budget_grows_with_the_page_count() -> None:
    assert llm_extract._ocr_total_timeout(4, 30.0, 4) == 120.0
    assert llm_extract._ocr_total_timeout(100, 30.0, 4) == 25 * 30.0


def test_text_layer_never_runs_ocr(scan_22: bytes) -> None:
    ocr = _ocr()
    with patch("ingestion.llm_extract.call_llm_with_images", ocr):
        assert extract_text_layer(scan_22) is None
        assert extract_pdf_text(scan_22, ocr=False) is None
    ocr.assert_not_called()
    assert pdf_page_count(scan_22) == 22
    assert pdf_page_count(b"not a pdf") == 0


# ---------------------------------------------------------------------------
# OCR text is never case-number evidence
# ---------------------------------------------------------------------------


def test_ocr_text_is_no_evidence_at_any_length() -> None:
    text = (RULING + " ") * 10
    assert document_evidence(text) == (text,)
    assert document_evidence(text, "link", from_ocr=True) == ()
    assert own_text_evidence(RULING) == (RULING,)
    assert own_text_evidence(RULING, from_ocr=True) == ()


def test_worker_evidence_ignores_ocr_text() -> None:
    text = (RULING + " ") * 10
    whole = {"document_id": "d", "extra": {"link_text": "Dept 9"}}
    assert _case_number_evidence(whole, text) == (text, "Dept 9")
    assert _read_text_evidence(whole, text) == (text, "Dept 9")
    ocr = {**whole, TEXT_FROM_OCR_KEY: True}
    assert _case_number_evidence(ocr, text) == ()
    assert _read_text_evidence(ocr, text) == ()
    # A deterministic splitter's child cut from OCR text inherits the mark.
    child = {**ocr, "_split_processed": True}
    assert _case_number_evidence(child, RULING) == ()


# ---------------------------------------------------------------------------
# Worker: a Santa Clara (text-first) reingest of a 22-page scan
# ---------------------------------------------------------------------------


def _sc_worker() -> tuple[IngestionWorker, MagicMock]:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    text_extractor = MagicMock()
    text_extractor.extract.return_value = [
        ExtractedRuling(
            extracted_case_number=LLM_CASE,
            extracted_case_title="Alder v. Acme Corp.",
            ruling_text="The motion for summary judgment is GRANTED.",
            outcome=ExtractionOutcome.GRANTED,
        )
    ]
    multimodal = MagicMock()
    multimodal.extract_from_pdf.side_effect = AssertionError("Santa Clara is text-first")
    worker._get_framework_extractor = lambda: text_extractor  # type: ignore[method-assign]
    worker._get_county_extractor = lambda *a, **k: text_extractor  # type: ignore[method-assign]
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    worker._get_connection = MagicMock()  # type: ignore[method-assign]
    return worker, text_extractor


def _sc_reingest_event(pdf: bytes) -> dict[str, Any]:
    return {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004986",
        "scraper_id": "reingest-ca-santa_clara",
        "state": "CA",
        "county": "Santa Clara",
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "434fd3ec417fedf31a9f41244ca9e473b1e796f4253ae7de78868fd3705e5eff",
        "s3_key": "ca/santa_clara/superior_court/raw/434fd3ec.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": pdf.decode("latin-1"),
        "capture_timestamp": "2026-11-01T18:00:00+00:00",
        "_replace_split_set": True,
    }


@patch("ingestion.worker.insert_validation_result")
def test_reingest_reads_page_15_and_keeps_the_llm_number(
    mock_validation: MagicMock, scan_22: bytes
) -> None:
    worker, text_extractor = _sc_worker()
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
    ):
        worker.process_event(_sc_reingest_event(scan_22))

    # The text split saw page 15.
    text_extractor.extract.assert_called_once()
    assert RULING in text_extractor.extract.call_args.args[0]
    # The child carries no evidence: its number is the extractor's, kept
    # under the scanned-PDF rules instead of being refuted by OCR text.
    write_batch.assert_called_once()
    children = write_batch.call_args.args[3].children
    assert len(children) == 1
    child = children[0]
    assert child.event_data[CASE_NUMBER_EVIDENCE_KEY] == ()
    assert child.event_data[TEXT_FROM_OCR_KEY] is True
    assert child.case_number_evidence == ()
    assert child.case_number == LLM_CASE


def test_reingest_of_incomplete_ocr_is_a_retryable_failure(scan_22: bytes) -> None:
    worker, text_extractor = _sc_worker()
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()),
        patch("ingestion.llm_extract._OCR_MAX_PAGES", 10),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        worker.process_event(_sc_reingest_event(scan_22))

    # Retryable: not the "nothing could be read" subclass.
    assert not isinstance(raised.value, NoExtractableTextError)
    assert OCR_INCOMPLETE in str(raised.value)
    assert "page cap 10 of 22 pages" in str(raised.value)
    text_extractor.extract.assert_not_called()
    write_batch.assert_not_called()


def test_reingest_of_failed_ocr_is_retryable_not_unextractable() -> None:
    worker, text_extractor = _sc_worker()
    with (
        patch("ingestion.llm_extract.call_llm_with_images", MagicMock(return_value=None)),
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        worker.process_event(_sc_reingest_event(_scanned_pdf(2)))
    assert not isinstance(raised.value, NoExtractableTextError)
    assert OCR_INCOMPLETE in str(raised.value)
    text_extractor.extract.assert_not_called()


@pytest.mark.parametrize(
    ("county", "scraper_id", "pages"),
    [
        pytest.param("Santa Clara", "reingest-ca-santa_clara", None, id="text-first-every-page"),
        pytest.param("Orange", "ca-oc-tentatives", 10, id="multimodal-fallback-only"),
    ],
)
@patch.object(IngestionWorker, "_llm_split_document", return_value=True)
@patch("ingestion.worker.psycopg")
def test_ocr_page_budget_follows_the_extraction_strategy(
    mock_psycopg: MagicMock,
    mock_split: MagicMock,
    county: str,
    scraper_id: str,
    pages: int | None,
) -> None:
    """A text-first county's split reads the OCR text, so every page is
    OCR'd; a multimodal county reads the page images itself, so OCR stays
    a 10-page fallback (and is flagged incomplete past it)."""
    worker, _ = _sc_worker()
    event = {**_sc_reingest_event(b"%PDF-1.4 bytes"), "county": county, "scraper_id": scraper_id}
    ocr_text = PdfText("x" * 300, from_ocr=True, page_count=22, incomplete=None)
    with patch("ingestion.worker.extract_pdf_text", return_value=ocr_text) as extract:
        worker.process_event(event)
    extract.assert_called_once()
    assert extract.call_args.kwargs["ocr_max_pages"] == pages
    split_event = mock_split.call_args.args[0]
    assert split_event[TEXT_FROM_OCR_KEY] is True
    assert split_event.get(OCR_INCOMPLETE_KEY) is None


# ---------------------------------------------------------------------------
# Worker: a multimodal county reads page images; only its text fallback
# depends on the OCR
# ---------------------------------------------------------------------------


def _oc_worker(multimodal_result: Any) -> tuple[IngestionWorker, MagicMock, MagicMock]:
    worker, text_extractor = _sc_worker()
    multimodal = MagicMock()
    if isinstance(multimodal_result, Exception):
        multimodal.extract_from_pdf.side_effect = multimodal_result
    else:
        multimodal.extract_from_pdf.return_value = multimodal_result
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    return worker, text_extractor, multimodal


def _oc_reingest_event(pdf: bytes) -> dict[str, Any]:
    return {
        **_sc_reingest_event(pdf),
        "county": "Orange",
        "scraper_id": "reingest-ca-orange",
        "s3_key": "ca/orange/superior_court/raw/abc.pdf",
    }


@patch("ingestion.worker.insert_validation_result")
def test_multimodal_split_ignores_capped_ocr(mock_validation: MagicMock, scan_22: bytes) -> None:
    """Orange reads all 22 page images itself: the 10-page OCR fallback
    being incomplete does not fail the split."""
    worker, text_extractor, multimodal = _oc_worker(
        [
            ExtractedRuling(
                extracted_case_number="30-2026-01234567",
                extracted_case_title="Alder v. Acme Corp.",
                ruling_text="The motion is GRANTED.",
            )
        ]
    )
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
    ):
        worker.process_event(_oc_reingest_event(scan_22))
    multimodal.extract_from_pdf.assert_called_once()
    text_extractor.extract.assert_not_called()
    write_batch.assert_called_once()
    (child,) = write_batch.call_args.args[3].children
    assert child.event_data[OCR_INCOMPLETE_KEY] == "page cap 10 of 22 pages"
    assert child.case_number_evidence == ()


def test_multimodal_failure_does_not_fall_back_to_capped_ocr(scan_22: bytes) -> None:
    worker, text_extractor, _ = _oc_worker(RuntimeError("page 3 failed"))
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()),
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        worker.process_event(_oc_reingest_event(scan_22))
    assert "multimodal: RuntimeError: page 3 failed" in str(raised.value)
    assert OCR_INCOMPLETE in str(raised.value)
    text_extractor.extract.assert_not_called()


# ---------------------------------------------------------------------------
# Worker: a CC portal PDF-only ruling cut from OCR text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pdf_text", "ruling", "from_ocr"),
    [
        pytest.param(PdfText(RULING, from_ocr=True), RULING, True, id="ocr"),
        pytest.param(PdfText(RULING), RULING, False, id="text-layer"),
        pytest.param(None, None, False, id="unreadable"),
    ],
)
def test_cc_portal_transcription_carries_the_ocr_mark(
    pdf_text: PdfText | None, ruling: str | None, from_ocr: bool
) -> None:
    from courts.ca import cc_tentatives_portal as portal

    worker, _ = _sc_worker()
    event = {
        "document_id": "d",
        "content_format": "text",
        "ruling_text": '{"row": {}}',
    }

    def transcribe(envelope: Any, extract: Any) -> Any:
        return portal.EnvelopePdfTranscription(extract(b"%PDF-1.4"), "transcribed")

    with (
        patch.object(portal, "load_envelope", return_value={"row": {}}),
        patch.object(portal, "envelope_fields", return_value={"ruling_text": None}),
        patch.object(portal, "transcribe_envelope_pdf", side_effect=transcribe),
        patch("ingestion.worker.extract_pdf_text", return_value=pdf_text),
    ):
        out = worker._unwrap_cc_portal_envelope(event)
    assert (out.get("ruling_text") or None) == ruling
    assert out.get(TEXT_FROM_OCR_KEY, False) is from_ocr
