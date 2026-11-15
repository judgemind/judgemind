"""Regression tests for #4995: no silent page cap on the multimodal path.

``LlmExtractor.extract_from_pdf`` (Orange and every unconfigured county)
stopped reading at page 50 (``max_pages: int = 50``) and logged it only at
info, so a longer PDF lost every ruling past page 50 while its split passed
as complete.  The same bug class as the OCR fallback's 10-page cap (#4986).

Now the cap is ``_MULTIMODAL_MAX_PAGES`` (500), a bound for runaway
documents, and a PDF over it is never truncated silently: it logs
``llm_extractor.page_limit_reached`` with ``total_pages`` and raises
``PageCapExceededError`` under ``strict``, which the worker turns into a
failed extraction (rows untouched, a reingest reports the key for a retry).
Document-level cache entries written under the old cap for PDFs over 50
pages hold only the first 50 pages' rulings, so those PDFs use a separate
document key.  The text extractor's ``_MAX_CHUNKS`` truncation is flagged
the same way.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import pymupdf
import pytest

from framework import llm_extractor
from framework.llm_extractor import (
    PDF_PER_PAGE_PROMPT,
    ExtractionIncompleteError,
    LlmExtractor,
    PageCapExceededError,
    _content_hash_for_cache,
    _LlmCache,
)
from framework.llm_schema import ExtractedRuling
from ingestion.worker import IngestionWorker, SplitExtractionFailedError

RULING_PAGE = 55
RULING_CASE = "2026-01455555"


def _pdf(pages: int) -> bytes:
    """A *pages*-page PDF whose pages render to distinct images.  Page 55's
    text layer prints the ruling's case number, the worker's evidence."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=300, height=200)
        page.insert_text((20, 100), f"Calendar page {n}")
        if n == RULING_PAGE:
            page.insert_text((20, 130), f"{RULING_CASE} Alder v. Acme Corp.")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="module")
def pdf_60() -> bytes:
    return _pdf(60)


def _page_json(page: int) -> str:
    if page != RULING_PAGE:
        return "[]"
    return json.dumps(
        [
            {
                "entry_number": 7,
                "case_info": f"{RULING_CASE} Alder v. Acme Corp.",
                "ruling_text": "The motion for summary judgment is GRANTED.",
            }
        ]
    )


def _llm() -> MagicMock:
    """A stand-in for ``call_llm_with_images``: pages are sent in order, so
    call *n* is page *n*."""
    from ingestion.llm_providers import LLMResponse

    calls = {"n": 0}

    def call(**kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        return LLMResponse(text=_page_json(calls["n"]), input_tokens=10, output_tokens=5)

    return MagicMock(side_effect=call)


def _extractor(cache: object | None = None) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._max_retries = 1
    ext._cache = cache
    return ext


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:  # noqa: N803
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> dict:  # noqa: N803
        self.objects[Key] = Body
        return {}


def _cases(rulings: list[ExtractedRuling]) -> list[str | None]:
    return [r.extracted_case_number for r in rulings]


def _warnings(logger: MagicMock, event: str) -> list[dict[str, Any]]:
    """The keyword fields of each ``logger.warning(event, ...)`` call.  The
    module logger is patched: structlog's cached loggers can escape
    ``capture_logs`` once another test configured structlog."""
    return [c.kwargs for c in logger.warning.call_args_list if c.args == (event,)]


# ---------------------------------------------------------------------------
# Every page of a PDF past the old 50-page cap is read
# ---------------------------------------------------------------------------


def test_multimodal_page_cap_60_page_pdf_extracts_ruling_on_page_55(pdf_60: bytes) -> None:
    llm = _llm()
    with patch("ingestion.llm_providers.call_llm_with_images", llm):
        rulings = _extractor().extract_from_pdf(pdf_60, strict=True)

    assert llm.call_count == 60
    assert _cases(rulings) == [RULING_CASE]
    assert "GRANTED" in rulings[0].ruling_text


def test_multimodal_page_cap_default_is_far_above_real_calendars() -> None:
    assert llm_extractor._MULTIMODAL_MAX_PAGES >= 500


# ---------------------------------------------------------------------------
# A PDF over the cap is flagged, never silently truncated
# ---------------------------------------------------------------------------


def test_multimodal_page_cap_hit_raises_under_strict_before_any_llm_call(
    pdf_60: bytes,
) -> None:
    llm = _llm()
    with (
        patch("ingestion.llm_providers.call_llm_with_images", llm),
        patch.object(llm_extractor, "_MULTIMODAL_MAX_PAGES", 50),
        patch.object(llm_extractor, "logger") as logger,
        pytest.raises(PageCapExceededError) as raised,
    ):
        _extractor(MagicMock()).extract_from_pdf(pdf_60, strict=True, document_id="doc-1")

    assert isinstance(raised.value, ExtractionIncompleteError)
    assert "60 pages" in str(raised.value)
    llm.assert_not_called()
    assert _warnings(logger, "llm_extractor.page_limit_reached") == [
        {"max_pages": 50, "total_pages": 60, "document_id": "doc-1"}
    ]


def test_multimodal_page_cap_hit_without_strict_returns_partial_uncached(
    pdf_60: bytes,
) -> None:
    cache = MagicMock()
    cache.get.return_value = None
    cache.get_page.return_value = None
    llm = _llm()
    with (
        patch("ingestion.llm_providers.call_llm_with_images", llm),
        patch.object(llm_extractor, "logger") as logger,
    ):
        rulings = _extractor(cache).extract_from_pdf(pdf_60, max_pages=50)

    assert llm.call_count == 50
    assert rulings == []  # the ruling is on page 55
    cache.get.assert_not_called()  # nothing complete can be cached
    cache.put.assert_not_called()
    assert _warnings(logger, "llm_extractor.page_limit_reached")


# ---------------------------------------------------------------------------
# A document entry cached under the old 50-page cap is not served
# ---------------------------------------------------------------------------


def test_multimodal_page_cap_stale_truncated_cache_entry_is_not_served(pdf_60: bytes) -> None:
    s3 = _FakeS3()
    cache = _LlmCache(s3, "bucket", "anthropic", "test-model")
    # What the old extractor cached for this PDF: pages 1-50 only, no rulings
    # from page 55.  Both the bytes-only key and the pre-#4815 metadata key.
    stale = [ExtractedRuling(extracted_case_number="30-2026-00000001").model_dump(mode="json")]
    metadata = {"judge_name": "Hon. Alder"}
    cache.put(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(pdf_60), stale)
    cache.put(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(pdf_60, metadata), stale)
    ext = _extractor(cache)

    llm = _llm()
    with patch("ingestion.llm_providers.call_llm_with_images", llm):
        first = ext.extract_from_pdf(pdf_60, metadata=metadata, strict=True)
    assert llm.call_count == 60
    assert _cases(first) == [RULING_CASE]

    # The complete result is cached under the all-pages key and served next.
    with patch("ingestion.llm_providers.call_llm_with_images") as again:
        second = ext.extract_from_pdf(pdf_60, strict=True)
    again.assert_not_called()
    assert _cases(second) == [RULING_CASE]


def test_multimodal_page_cap_short_pdf_keeps_its_cache_key() -> None:
    """A PDF within the old cap was read in full: its entry stays valid."""
    pdf = _pdf(3)
    s3 = _FakeS3()
    cache = _LlmCache(s3, "bucket", "anthropic", "test-model")
    cached = [ExtractedRuling(extracted_case_number=RULING_CASE).model_dump(mode="json")]
    cache.put(PDF_PER_PAGE_PROMPT, _content_hash_for_cache(pdf), cached)
    with patch("ingestion.llm_providers.call_llm_with_images") as llm:
        rulings = _extractor(cache).extract_from_pdf(pdf, strict=True)
    llm.assert_not_called()
    assert _cases(rulings) == [RULING_CASE]


def test_pdf_page_count_unreadable_is_zero() -> None:
    assert llm_extractor._pdf_page_count(b"not a pdf") == 0
    assert llm_extractor._pdf_page_count(_pdf(4)) == 4


# ---------------------------------------------------------------------------
# Worker: a PDF over the cap is a retryable failed extraction
# ---------------------------------------------------------------------------


def _oc_worker(multimodal: MagicMock) -> tuple[IngestionWorker, MagicMock]:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    text_extractor = MagicMock()
    text_extractor.extract.side_effect = AssertionError("no text fallback past the page cap")
    worker._get_framework_extractor = lambda: text_extractor  # type: ignore[method-assign]
    worker._get_county_extractor = lambda *a, **k: text_extractor  # type: ignore[method-assign]
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    worker._get_connection = MagicMock()  # type: ignore[method-assign]
    return worker, text_extractor


def _oc_reingest_event(pdf: bytes) -> dict[str, Any]:
    return {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004995",
        "scraper_id": "reingest-ca-orange",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "4995" * 16,
        "s3_key": "ca/orange/superior_court/raw/4995.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": pdf.decode("latin-1"),
        "capture_timestamp": "2026-11-14T18:00:00+00:00",
        "_replace_split_set": True,
    }


def test_multimodal_page_cap_reingest_is_a_retryable_failure(pdf_60: bytes) -> None:
    multimodal = _extractor()
    llm = _llm()
    with (
        patch("ingestion.llm_providers.call_llm_with_images", llm),
        patch.object(llm_extractor, "_MULTIMODAL_MAX_PAGES", 50),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        _oc_worker(multimodal)[0].process_event(_oc_reingest_event(pdf_60))

    assert "PageCapExceededError" in str(raised.value)
    assert "60 pages" in str(raised.value)
    llm.assert_not_called()
    write_batch.assert_not_called()


@patch("ingestion.worker.insert_validation_result")
def test_multimodal_page_cap_reingest_splits_page_55(
    mock_validation: MagicMock, pdf_60: bytes
) -> None:
    multimodal = _extractor()
    with (
        patch("ingestion.llm_providers.call_llm_with_images", _llm()),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
    ):
        _oc_worker(multimodal)[0].process_event(_oc_reingest_event(pdf_60))

    write_batch.assert_called_once()
    (child,) = write_batch.call_args.args[3].children
    assert child.case_number == RULING_CASE


# ---------------------------------------------------------------------------
# Text extraction: ``_MAX_CHUNKS`` chunks that miss the tail are flagged
# ---------------------------------------------------------------------------


def _chunk_result() -> list[Any]:
    from framework.llm_extractor import ExtractionResult

    return [ExtractionResult(rulings=[ExtractedRuling(extracted_case_number=RULING_CASE)])]


def test_chunk_cap_hit_raises_under_strict() -> None:
    ext = _extractor()
    ext._max_chars_per_chunk = 1_000
    text = "x" * 20_000  # no boundaries: force-split, 10 chunks cover ~10K
    with (
        patch.object(ext, "_extract_chunk_with_retry", return_value=_chunk_result()) as call,
        patch.object(llm_extractor, "logger") as logger,
        pytest.raises(ExtractionIncompleteError, match="exceeds 10 chunks"),
    ):
        ext.extract(text, strict=True)
    call.assert_not_called()
    (warning,) = _warnings(logger, "llm_extractor.chunk_cap_reached")
    assert warning["total_chars"] == 20_000


def test_chunk_cap_hit_without_strict_is_not_cached() -> None:
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    ext._max_chars_per_chunk = 1_000
    with patch.object(ext, "_extract_chunk_with_retry", return_value=_chunk_result()):
        rulings = ext.extract("y" * 20_000)
    assert rulings
    cache.put.assert_not_called()


def test_chunks_that_cover_the_text_are_not_flagged() -> None:
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    ext._max_chars_per_chunk = 1_000
    with patch.object(ext, "_extract_chunk_with_retry", return_value=_chunk_result()) as call:
        ext.extract("z" * 5_000, strict=True)
    assert call.call_count > 1
    cache.put.assert_called_once()
