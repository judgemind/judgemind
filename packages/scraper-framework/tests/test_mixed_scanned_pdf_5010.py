"""Regression tests for #5010: a text PDF with scanned pages.

``extract_pdf_text`` used to OCR a PDF only when *no* page had a text layer.
In a PDF that mixes text pages with scanned pages (a text calendar with a
scanned order appended) the scanned pages were silently dropped, and the
split read part of the document as if it were complete.  The scrapers'
private pdfplumber loops (Santa Clara, ``pdf_link_scraper``, Ventura) had
the same gap.

Now every page is judged on its own (``llm_extract._read_pdf_pages``):

- a scanned page is OCR'd and merged in page order; a failed or capped OCR
  of it leaves the result ``incomplete``, which the worker refuses to split;
- OCR text is never case-number evidence: the document as a whole has none,
  but a split child cut wholly from the text-layer pages keeps its own text
  (``case_evidence.text_from_ocr``);
- a scraper reads the text layer through the shared ``scraper_pdf_text``,
  which names the scanned pages in ``extra``; the worker reads the archived
  PDF again for such a live capture and OCRs them.

The fixtures are synthetic: text pages written with pymupdf (a text layer)
and an image-only page (a picture, no text layer), as on a real scan.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf
import pytest

from courts.ca import pdf_link_scraper as pls
from framework import CapturedDocument, ContentFormat
from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from ingestion import llm_extract
from ingestion.case_evidence import (
    CASE_NUMBER_EVIDENCE_KEY,
    TEXT_FROM_OCR_KEY,
    TEXT_LAYER_KEY,
    text_from_ocr,
)
from ingestion.llm_extract import (
    PAGE_SEPARATOR,
    SCANNED_PAGES_KEY,
    extract_pdf_text,
    extract_text_layer,
    scraper_pdf_text,
)
from ingestion.llm_providers import LLMResponse
from ingestion.worker import (
    OCR_INCOMPLETE,
    OCR_INCOMPLETE_KEY,
    IngestionWorker,
    NoExtractableTextError,
    SplitExtractionFailedError,
    _case_number_evidence,
    _pdf_text_marks,
    _read_text_evidence,
)

TEXT_CASE = "25CV100002"
SCANNED_CASE = "25CV400004"
SCANNED_RULING = (
    f"Case No.: {SCANNED_CASE} Birch v. Beta LLC\nThe demurrer is GRANTED with leave to amend."
)


def _text_page(page: int) -> str:
    case = TEXT_CASE if page == 2 else f"25CV10000{page}"
    return (
        f"SUPERIOR COURT OF CALIFORNIA, COUNTY OF SANTA CLARA\n"
        f"Department 9 tentative rulings, calendar page {page}\n"
        f"Case No.: {case} Alder v. Acme Corp. {page}\n"
        f"The motion to compel further responses is GRANTED. Moving party to give notice."
    )


def _picture(page: Any, rect: Any) -> None:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
    pix.clear_with(200)
    page.insert_image(rect, pixmap=pix)


def _mixed_pdf(*, scanned: tuple[int, ...] = (4,), pages: int = 4, stamp: str = "") -> bytes:
    """Text pages, and image-only pages at the 1-based *scanned* numbers
    (with *stamp* as their only text layer, as an e-filing stamp is)."""
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=612, height=792)
        if number in scanned:
            _picture(page, pymupdf.Rect(20, 20, 592, 772))
            if stamp:
                page.insert_text((30, 15), stamp, fontsize=8)
        else:
            page.insert_textbox(pymupdf.Rect(40, 40, 572, 752), _text_page(number), fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _page_number(text_message: str) -> int:
    match = re.search(r"\(page (\d+)\)", text_message)
    assert match, text_message
    return int(match.group(1))


def _ocr(fail_pages: frozenset[int] = frozenset(), text: str = SCANNED_RULING) -> MagicMock:
    """``call_llm_with_images``: *text* for each page, ``None`` (a failed
    call) for *fail_pages*."""

    def call(*, system_prompt: str, text_message: str, images: Any, **_: Any) -> Any:
        if _page_number(text_message) in fail_pages:
            return None
        return LLMResponse(text=text, input_tokens=10, output_tokens=10)

    return MagicMock(side_effect=call)


@pytest.fixture(scope="module")
def mixed() -> bytes:
    return _mixed_pdf()


# ---------------------------------------------------------------------------
# extract_pdf_text: the scanned page is OCR'd, in page order
# ---------------------------------------------------------------------------


def test_mixed_scanned_pdf_ocrs_page_4_only(mixed: bytes) -> None:
    ocr = _ocr()
    with patch("ingestion.llm_extract.call_llm_with_images", ocr):
        result = extract_pdf_text(mixed)

    assert result is not None
    assert ocr.call_count == 1
    assert _page_number(ocr.call_args.kwargs["text_message"]) == 4
    assert result.incomplete is None
    assert result.from_ocr
    assert result.page_count == 4
    pages = result.text.split(PAGE_SEPARATOR)
    assert len(pages) == 4
    assert TEXT_CASE in pages[1]
    assert pages[3] == SCANNED_RULING
    # The text-layer run around the OCR text: pages 1-3.
    assert result.text_layer == (PAGE_SEPARATOR.join(pages[:3]),)


def test_mixed_scanned_pdf_failed_ocr_is_incomplete(mixed: bytes) -> None:
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr(frozenset({4}))):
        result = extract_pdf_text(mixed)

    assert result is not None
    assert result.incomplete == "page(s) [4] failed"
    assert SCANNED_CASE not in result.text
    assert TEXT_CASE in result.text
    assert not result.from_ocr
    # Text missing page 4 is no evidence as a whole either; pages 1-3 are
    # its one text-layer run.
    assert result.text_layer == (result.text,)
    marks = _pdf_text_marks([result])
    assert marks[TEXT_FROM_OCR_KEY] is True
    assert marks[OCR_INCOMPLETE_KEY] == "page(s) [4] failed"
    event = {"document_id": "d", **marks}
    assert _case_number_evidence(event, result.text) == ()
    child = {**event, "_split_processed": True}
    page_2 = result.text.split(PAGE_SEPARATOR)[1]
    assert _case_number_evidence(child, page_2) == (page_2,)


def test_mixed_scanned_pdf_failed_middle_page_breaks_the_run() -> None:
    pdf = _mixed_pdf(scanned=(2,))
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr(frozenset({2}))):
        result = extract_pdf_text(pdf)
    assert result is not None
    pages = result.text.split(PAGE_SEPARATOR)
    assert result.text_layer == (pages[0], PAGE_SEPARATOR.join(pages[1:]))
    # A slice across the missing page is not source text.
    child = {"document_id": "d", "_split_processed": True, **_pdf_text_marks([result])}
    assert text_from_ocr(child, pages[0] + "\n" + pages[1])


def test_mixed_scanned_pdf_ocr_page_cap_is_incomplete() -> None:
    pdf = _mixed_pdf(scanned=(2, 3, 4))
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr:
        result = extract_pdf_text(pdf, ocr_max_pages=2)
    assert result is not None
    assert ocr.call_count == 2
    assert result.incomplete is not None
    assert "page cap 2 of 3 pages" in result.incomplete


def test_mixed_scanned_pdf_render_failure_is_incomplete(mixed: bytes) -> None:
    with patch.object(llm_extract, "_render_page", side_effect=RuntimeError("x")):
        result = extract_pdf_text(mixed)
    assert result is not None
    assert result.incomplete == "OCR of page(s) [4] failed"


def test_mixed_scanned_pdf_without_ocr_names_the_scanned_pages(mixed: bytes) -> None:
    ocr = _ocr()
    with patch("ingestion.llm_extract.call_llm_with_images", ocr):
        result = extract_pdf_text(mixed, ocr=False)
        layer = extract_text_layer(mixed)
    ocr.assert_not_called()
    assert result is not None
    assert result.incomplete == "page(s) [4] have no text layer"
    assert layer == result.text
    assert SCANNED_CASE not in result.text


def test_mixed_scanned_pdf_stamped_scan_page_is_still_scanned() -> None:
    """An e-filing stamp is a text layer of a few dozen characters on a
    page that is a picture: the page is scanned."""
    pdf = _mixed_pdf(stamp="ELECTRONICALLY FILED Superior Court of California")
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr:
        result = extract_pdf_text(pdf)
    assert ocr.call_count == 1
    assert result is not None
    assert result.text.split(PAGE_SEPARATOR)[3] == SCANNED_RULING


def test_mixed_scanned_pdf_blank_scan_page_keeps_the_text_layer(mixed: bytes) -> None:
    """OCR that finds nothing on the page adds no OCR text: the document
    stays all text layer (its evidence intact), and is complete."""
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr(text="  ")):
        result = extract_pdf_text(mixed)
    assert result is not None
    assert result.incomplete is None
    assert not result.from_ocr
    assert result.text_layer == ()
    assert len(result.text.split(PAGE_SEPARATOR)) == 3


def test_text_page_with_a_logo_is_not_scanned() -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    _picture(page, pymupdf.Rect(20, 20, 120, 120))
    page.insert_textbox(pymupdf.Rect(40, 140, 572, 752), _text_page(1), fontsize=11)
    pdf = doc.tobytes()
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr:
        result = extract_pdf_text(pdf)
    ocr.assert_not_called()
    assert result is not None
    assert result.incomplete is None
    assert not result.from_ocr


def test_image_share_of_a_degenerate_page_is_zero() -> None:
    page = MagicMock(width=0, height=0, images=[])
    assert llm_extract._image_share(page) == 0.0


# ---------------------------------------------------------------------------
# Evidence: only the OCR'd parts are excluded
# ---------------------------------------------------------------------------


def _mixed_event(text: str) -> dict[str, Any]:
    pages = text.split(PAGE_SEPARATOR)
    return {
        "document_id": "d",
        "extra": {"link_text": "Dept 9"},
        TEXT_FROM_OCR_KEY: True,
        TEXT_LAYER_KEY: [PAGE_SEPARATOR.join(pages[:3])],
    }


def test_mixed_scanned_pdf_evidence_excludes_the_ocr_page(mixed: bytes) -> None:
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()):
        result = extract_pdf_text(mixed)
    assert result is not None
    event = _mixed_event(result.text)

    # The document as a whole: no evidence (page 4 could print any number).
    assert _case_number_evidence(event, result.text) == ()
    assert _read_text_evidence(event, result.text) == ()
    # A deterministic splitter's child cut from page 2 (its whitespace
    # normalised, as ``_canonical_split_text`` does): its own text counts.
    page_2 = " ".join(result.text.split(PAGE_SEPARATOR)[1].split())
    child = {**event, "_split_processed": True}
    assert _case_number_evidence(child, page_2) == (page_2,)
    assert not text_from_ocr(child, page_2)
    # A child cut from page 4's OCR text has none.
    assert _case_number_evidence(child, SCANNED_RULING) == ()
    # Nor does one that reaches across from page 3 into page 4.
    pages = result.text.split(PAGE_SEPARATOR)
    assert text_from_ocr(child, pages[2] + "\n" + pages[3])
    # Text with no OCR in it is unaffected.
    assert not text_from_ocr({"document_id": "d"}, SCANNED_RULING)
    assert text_from_ocr(event, "")


# ---------------------------------------------------------------------------
# Worker: a text-first (Santa Clara) reingest of the mixed PDF
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
            extracted_case_number=SCANNED_CASE,
            extracted_case_title="Birch v. Beta LLC",
            ruling_text="The demurrer is GRANTED with leave to amend.",
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


def _sc_event(ruling_text: str, *, live: bool = False, extra: dict | None = None) -> dict:
    event: dict[str, Any] = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000005010",
        "scraper_id": "ca-santa-clara-tentatives" if live else "reingest-ca-santa_clara",
        "state": "CA",
        "county": "Santa Clara",
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "5010" * 16,
        "s3_key": "ca/santa_clara/superior_court/raw/5010.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": ruling_text,
        "capture_timestamp": "2026-11-01T18:00:00+00:00",
    }
    if not live:
        event["_replace_split_set"] = True
    if extra is not None:
        event["extra"] = extra
    return event


@patch("ingestion.worker.insert_validation_result")
def test_mixed_scanned_pdf_reingest_splits_page_4(mock_validation: MagicMock, mixed: bytes) -> None:
    worker, text_extractor = _sc_worker()
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
    ):
        worker.process_event(_sc_event(mixed.decode("latin-1")))

    # The text split saw page 4.
    text_extractor.extract.assert_called_once()
    assert SCANNED_RULING in text_extractor.extract.call_args.args[0]
    children = write_batch.call_args.args[3].children
    assert len(children) == 1
    child = children[0]
    assert child.case_number == SCANNED_CASE
    # The document holds OCR text: no evidence for the LLM split's child.
    assert child.event_data[TEXT_FROM_OCR_KEY] is True
    assert child.event_data[CASE_NUMBER_EVIDENCE_KEY] == ()


def test_mixed_scanned_pdf_reingest_with_failed_ocr_is_retryable(mixed: bytes) -> None:
    worker, text_extractor = _sc_worker()
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr(frozenset({4}))),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        worker.process_event(_sc_event(mixed.decode("latin-1")))

    assert not isinstance(raised.value, NoExtractableTextError)
    assert OCR_INCOMPLETE in str(raised.value)
    assert "page(s) [4] failed" in str(raised.value)
    text_extractor.extract.assert_not_called()
    write_batch.assert_not_called()


# ---------------------------------------------------------------------------
# Live capture: the scraper names the scanned pages, the worker OCRs them
# ---------------------------------------------------------------------------


def test_scraper_pdf_text_names_the_scanned_pages(mixed: bytes) -> None:
    extra: dict[str, Any] = {SCANNED_PAGES_KEY: [9]}
    text = scraper_pdf_text(mixed, extra=extra)
    assert extra[SCANNED_PAGES_KEY] == [4]
    assert TEXT_CASE in text
    assert "\f" not in text  # the scrapers' "\n" page join
    # A text PDF clears a stale mark.
    clean: dict[str, Any] = {SCANNED_PAGES_KEY: [4]}
    scraper_pdf_text(_mixed_pdf(scanned=()), extra=clean)
    assert SCANNED_PAGES_KEY not in clean
    # The audits' count uses the same rule (#5010 sizing).
    assert llm_extract.pdf_scanned_pages(mixed) == [4]
    assert llm_extract.pdf_scanned_pages(_mixed_pdf(scanned=())) == []


def test_scrapers_share_the_one_reader() -> None:
    from courts.ca import (
        cc_tentatives,
        fresno_tentatives,
        riverside_tentatives,
        sc_tentatives,
        ventura_tentatives,
    )

    for module in (pls, cc_tentatives, fresno_tentatives, riverside_tentatives, ventura_tentatives):
        assert module._extract_pdf_text is scraper_pdf_text
    assert sc_tentatives.extract_pdf_text is scraper_pdf_text


def test_pdf_link_parse_document_marks_scanned_pages(mixed: bytes) -> None:
    scraper = pls.PdfLinkScraper.__new__(pls.PdfLinkScraper)
    scraper._pdf_config = pls.PdfLinkConfig(
        index_url="https://example.test",
        pdf_base_url="https://example.test",
        link_text_re=re.compile(r"(?P<department>\d+)"),
        case_number_re=re.compile(r"25CV\d{6}"),
    )
    scraper._log = MagicMock()
    doc = CapturedDocument(
        scraper_id="ca-test",
        state="CA",
        county="Riverside",
        court="Superior Court",
        source_url="https://example.test/a.pdf",
        content_format=ContentFormat.PDF,
        raw_content=mixed,
        capture_timestamp=datetime(2026, 11, 1, tzinfo=UTC),
        content_hash="5010",
    )
    scraper.parse_document(doc)
    assert doc.extra[SCANNED_PAGES_KEY] == [4]
    assert doc.case_number == "25CV100001"


def _s3_returning(body: bytes | None) -> MagicMock:
    s3 = MagicMock()
    if body is None:
        s3.get_object.side_effect = RuntimeError("NoSuchKey")
    else:
        s3.get_object.side_effect = lambda **_k: {"Body": io.BytesIO(body)}
    return s3


@pytest.fixture()
def live_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live re-read on, whatever the production default
    (``LIVE_SCANNED_PAGE_REREAD``)."""
    from ingestion import worker as worker_mod

    monkeypatch.setattr(worker_mod, "LIVE_SCANNED_PAGE_REREAD", True)


def test_live_capture_scanned_pages_deferred_without_reading_the_archive(
    mixed: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off: no S3 read, no OCR in the live worker; the event is marked
    incomplete so no split is built on its partial text."""
    from ingestion import worker as worker_mod

    monkeypatch.setattr(worker_mod, "LIVE_SCANNED_PAGE_REREAD", False)
    worker, _ = _sc_worker()
    worker._s3_client = _s3_returning(mixed)
    extra: dict[str, Any] = {}
    layer = scraper_pdf_text(mixed, extra=extra)
    with patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr:
        event, text = worker._with_scanned_pages_reread(
            _sc_event(layer, live=True, extra=extra), layer
        )
    assert text == layer
    assert "page(s) [4] are scanned" in event[OCR_INCOMPLETE_KEY]
    worker._s3_client.get_object.assert_not_called()
    ocr.assert_not_called()


@pytest.mark.usefixtures("live_reread")
@patch("ingestion.worker.insert_validation_result")
def test_live_capture_with_scanned_pages_reads_the_archived_pdf(
    mock_validation: MagicMock, mixed: bytes
) -> None:
    worker, text_extractor = _sc_worker()
    worker._s3_client = _s3_returning(mixed)
    extra: dict[str, Any] = {}
    layer = scraper_pdf_text(mixed, extra=extra)
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr,
        patch.object(IngestionWorker, "_write_split_batch", autospec=True),
    ):
        worker.process_event(_sc_event(layer, live=True, extra=extra))

    ocr.assert_called_once()
    assert SCANNED_RULING in text_extractor.extract.call_args.args[0]


@pytest.mark.usefixtures("live_reread")
@patch("ingestion.worker.insert_validation_result")
def test_live_capture_read_again_once_for_a_whole_document(
    mock_validation: MagicMock, mixed: bytes
) -> None:
    """No rulings found: the document is written whole, from the text the
    split judged.  The whole-document child does not read the archive and
    OCR the page a second time."""
    worker, text_extractor = _sc_worker()
    text_extractor.extract.return_value = []
    worker._s3_client = _s3_returning(mixed)
    extra: dict[str, Any] = {}
    layer = scraper_pdf_text(mixed, extra=extra)
    with (
        patch("ingestion.llm_extract.call_llm_with_images", _ocr()) as ocr,
        patch.object(IngestionWorker, "_write_split_batch", autospec=True),
    ):
        worker.process_event(_sc_event(layer, live=True, extra=extra))
    assert ocr.call_count == 1
    assert worker._s3_client.get_object.call_count == 1
    event, _ = worker._with_scanned_pages_reread(_sc_event(layer, live=True, extra=extra), layer)
    assert SCANNED_PAGES_KEY not in event["extra"]


@pytest.mark.usefixtures("live_reread")
def test_live_capture_with_unreadable_archive_refuses_the_split(mixed: bytes) -> None:
    worker, text_extractor = _sc_worker()
    worker._s3_client = _s3_returning(None)
    extra: dict[str, Any] = {}
    layer = scraper_pdf_text(mixed, extra=extra)
    event, text = worker._with_scanned_pages_reread(_sc_event(layer, live=True, extra=extra), layer)
    assert text == layer
    assert "page(s) [4] have no text layer" in event[OCR_INCOMPLETE_KEY]


@pytest.mark.usefixtures("live_reread")
def test_reread_leaves_other_events_alone(mixed: bytes) -> None:
    worker, _ = _sc_worker()
    worker._s3_client = _s3_returning(mixed)
    plain = _sc_event("text", live=True, extra={})
    assert worker._with_scanned_pages_reread(plain, "text") == (plain, "text")
    child = {**_sc_event("t", live=True, extra={SCANNED_PAGES_KEY: [4]}), "_split_processed": True}
    assert worker._with_scanned_pages_reread(child, "t") == (child, "t")
    raw = mixed.decode("latin-1")
    already = _sc_event(raw, extra={SCANNED_PAGES_KEY: [4]})
    assert worker._with_scanned_pages_reread(already, raw) == (already, raw)
    worker._s3_client.get_object.assert_not_called()
