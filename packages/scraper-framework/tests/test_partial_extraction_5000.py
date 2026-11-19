"""Regression tests for #5000: a failed page or chunk with rows elsewhere is
not a complete split.

Found while verifying #4995: in a 51-page Orange PDF (document
``f2a60e90``) page 48 never parsed (``llm_extractor.page_parse_exhausted``),
the other pages returned rows, and the split was written as complete.  A
``strict`` extraction raised ``ExtractionIncompleteError`` only when no
rows came back at all, so every ruling on the failed page was lost until
someone reingested the key.  The same "incomplete result written as
complete" class as #4986 (OCR page cap) and #4995 (multimodal page cap).

Now a ``strict`` extraction with any failed page (multimodal) or chunk
(text, including one half of a retried chunk) raises
``PartialExtractionError``, carrying the rulings the other parts produced.
The worker refuses the split: a reingest leaves the key's rows alone and
reports the key (``SplitExtractionFailedError``, the failed manifest), a
live capture of a key with rows writes nothing, and a live capture of a new
key writes the partial rows (nothing to replace; a reingest replaces them
once every page answers).  No partial result is cached at document level,
and a failed page is never cached as an empty page, so the retry re-sends
only the failed page.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic
import pymupdf
import pytest

from framework import llm_extractor
from framework.llm_extractor import (
    ExtractionIncompleteError,
    ExtractionResult,
    LlmExtractor,
    PartialExtractionError,
    _LlmCache,
)
from framework.llm_schema import ExtractedRuling
from ingestion.worker import (
    SPLIT_EXTRACTION_FAILED_EVENT,
    IngestionWorker,
    SplitExtractionFailedError,
)

TOTAL_PAGES = 51
#: The page that fails (1-based), as page index 48 did on dev.
FAILED_PAGE = 49
#: Pages printing a ruling (1-based) -> its case number.
RULING_PAGES = {
    47: "2026-01400047",
    FAILED_PAGE: "2026-01400049",
    51: "2026-01400051",
}
UNPARSEABLE = 'Here are the rulings: [{"entry_number": 1, "case_info": "2026-0'


def _pdf(pages: int) -> bytes:
    """A *pages*-page PDF whose pages render to distinct images.  A ruling
    page's text layer prints its case number, the worker's evidence."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=300, height=200)
        page.insert_text((20, 100), f"Calendar page {n}")
        if n in RULING_PAGES:
            page.insert_text((20, 130), f"{RULING_PAGES[n]} Alder{n} v. Acme Corp.")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="module")
def pdf_51() -> bytes:
    return _pdf(TOTAL_PAGES)


def _page_json(page: int) -> str:
    if page not in RULING_PAGES:
        return json.dumps({"page_header": None, "rulings": []})
    return json.dumps(
        [
            {
                "entry_number": page,
                "case_info": f"{RULING_PAGES[page]} Alder{page} v. Acme Corp.",
                "ruling_text": f"The motion on page {page} is GRANTED.",
            }
        ]
    )


class _PagedLlm:
    """A stand-in for ``call_llm_with_images`` that answers by page image.

    The page images are learned from the PDF in render order; *failing*
    pages (1-based) answer with an unparseable response every time.
    """

    def __init__(self, pdf: bytes, failing: set[int]) -> None:
        self._page_of = {img: n for n, (img, _mt) in enumerate(_render(pdf), start=1)}
        self.failing = failing
        self.calls: list[int] = []

    def __call__(self, **kwargs: Any) -> object:
        from ingestion.llm_providers import LLMResponse

        page = self._page_of[kwargs["images"][0][0]]
        self.calls.append(page)
        text = UNPARSEABLE if page in self.failing else _page_json(page)
        return LLMResponse(text=text, input_tokens=10, output_tokens=5)


def _render(pdf: bytes) -> list[tuple[bytes, str]]:
    return llm_extractor._render_pdf_pages(pdf, TOTAL_PAGES)


def _extractor(cache: object | None = None) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic"):
        ext = LlmExtractor(api_key="test-key")
    ext._max_attempts = 1
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

    def keys(self, part: str) -> list[str]:
        return [k for k in self.objects if part in k]


def _cache() -> tuple[_LlmCache, _FakeS3]:
    s3 = _FakeS3()
    return _LlmCache(s3, "bucket", "anthropic", "test-model"), s3


def _cases(rulings: list[ExtractedRuling]) -> list[str | None]:
    return sorted(r.extracted_case_number or "" for r in rulings)


def _answered_cases() -> list[str]:
    return sorted(c for p, c in RULING_PAGES.items() if p != FAILED_PAGE)


# ---------------------------------------------------------------------------
# Multimodal: a failed page with rows on other pages
# ---------------------------------------------------------------------------


def test_failed_page_partial_raises_under_strict(pdf_51: bytes) -> None:
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        pytest.raises(PartialExtractionError) as raised,
    ):
        _extractor().extract_from_pdf(pdf_51, strict=True, document_id="f2a60e90")

    assert isinstance(raised.value, ExtractionIncompleteError)
    assert raised.value.failed_parts == [FAILED_PAGE - 1]
    # The rulings the other pages produced travel with the error.
    assert _cases(raised.value.rulings) == _answered_cases()
    assert f"of {TOTAL_PAGES}" in str(raised.value)


def test_failed_page_partial_applies_metadata_to_the_carried_rulings(pdf_51: bytes) -> None:
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        pytest.raises(PartialExtractionError) as raised,
    ):
        _extractor().extract_from_pdf(pdf_51, metadata={"department": "C15"}, strict=True)
    assert {r.department for r in raised.value.rulings} == {"C15"}


def test_failed_page_partial_without_strict_returns_rows_uncached(pdf_51: bytes) -> None:
    cache, _s3 = _cache()
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        patch.object(cache, "put", wraps=cache.put) as doc_put,
    ):
        rulings = _extractor(cache).extract_from_pdf(pdf_51)

    assert _cases(rulings) == _answered_cases()
    # No document-level entry: a later call must not be served the gap.
    doc_put.assert_not_called()


def test_failed_page_is_not_cached_and_retry_resends_only_it(pdf_51: bytes) -> None:
    """The per-page cache holds the 50 pages that answered, never the failed
    page as an empty page; the retry re-sends only the failed page and,
    complete, is cached at document level."""
    cache, s3 = _cache()
    ext = _extractor(cache)
    first = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=first),
        patch.object(cache, "put", wraps=cache.put) as doc_put,
        patch.object(cache, "put_page", wraps=cache.put_page) as page_put,
        pytest.raises(PartialExtractionError),
    ):
        ext.extract_from_pdf(pdf_51, strict=True)
    doc_put.assert_not_called()
    assert page_put.call_count == TOTAL_PAGES - 1
    # The failed page was asked twice (the stricter-JSON retry) and never cached.
    assert first.calls.count(FAILED_PAGE) == 2

    retry = _PagedLlm(pdf_51, failing=set())
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=retry),
        patch.object(cache, "put", wraps=cache.put) as doc_put,
    ):
        rulings = ext.extract_from_pdf(pdf_51, strict=True)
    assert retry.calls == [FAILED_PAGE]
    assert _cases(rulings) == sorted(RULING_PAGES.values())
    doc_put.assert_called_once()


def test_failed_page_with_no_rows_elsewhere_is_still_plain_incomplete(pdf_51: bytes) -> None:
    """No rows at all is the #4940 case: the multimodal call failed as a
    whole (the worker may still fall back to text)."""
    llm = _PagedLlm(pdf_51, failing=set(range(1, TOTAL_PAGES + 1)))
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        pytest.raises(ExtractionIncompleteError) as raised,
    ):
        _extractor().extract_from_pdf(pdf_51, strict=True)
    assert not isinstance(raised.value, PartialExtractionError)


# ---------------------------------------------------------------------------
# Text: a failed chunk with rulings from other chunks
# ---------------------------------------------------------------------------


def _chunk(case_number: str) -> list[ExtractionResult]:
    return [ExtractionResult(rulings=[ExtractedRuling(extracted_case_number=case_number)])]


def test_failed_chunk_partial_raises_under_strict() -> None:
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    with (
        patch.object(ext, "_split_text", return_value=(["first", "second", "third"], True)),
        patch.object(
            ext,
            "_extract_chunk_with_retry",
            side_effect=[_chunk("2026-0001"), [], _chunk("2026-0003")],
        ),
        pytest.raises(PartialExtractionError) as raised,
    ):
        ext.extract("first second third", strict=True)

    assert raised.value.failed_parts == [1]
    assert _cases(raised.value.rulings) == ["2026-0001", "2026-0003"]
    cache.put.assert_not_called()


def test_failed_chunk_partial_without_strict_returns_rulings_uncached() -> None:
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    with (
        patch.object(ext, "_split_text", return_value=(["first", "second"], True)),
        patch.object(ext, "_extract_chunk_with_retry", side_effect=[_chunk("2026-0001"), []]),
    ):
        rulings = ext.extract("first second")
    assert _cases(rulings) == ["2026-0001"]
    cache.put.assert_not_called()


def _two_halves() -> str:
    return "\n\n".join(f"Case No. 2026-{i:04d} paragraph {'x' * 200}" for i in range(40))


def test_failed_chunk_partial_half_of_a_retried_chunk_raises() -> None:
    """A chunk whose response did not parse is retried as two halves; one
    half failing loses its rulings even though the other answered."""
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    answers = iter(
        [None, ExtractionResult(rulings=[ExtractedRuling(extracted_case_number="A")]), None]
    )
    with (
        patch.object(ext, "_extract_chunk", side_effect=lambda *a, **k: next(answers)),
        pytest.raises(PartialExtractionError) as raised,
    ):
        ext.extract(_two_halves(), strict=True)
    assert raised.value.failed_parts == [0]
    assert _cases(raised.value.rulings) == ["A"]
    cache.put.assert_not_called()


def test_failed_chunk_partial_half_in_a_multi_chunk_document_raises() -> None:
    ext = _extractor()
    half_failed = _chunk("2026-0002")

    def retry(chunk: str, **kwargs: Any) -> list[ExtractionResult]:
        if chunk == "second":
            kwargs["failed_parts"].append(11)  # the second half of chunk 1
            return half_failed
        return _chunk("2026-0001")

    with (
        patch.object(ext, "_split_text", return_value=(["first", "second"], True)),
        patch.object(ext, "_extract_chunk_with_retry", side_effect=retry),
        pytest.raises(PartialExtractionError) as raised,
    ):
        ext.extract("first second", strict=True)
    assert raised.value.failed_parts == [1]


def test_every_chunk_answered_is_complete_and_cached() -> None:
    cache = MagicMock()
    cache.get.return_value = None
    ext = _extractor(cache)
    with (
        patch.object(ext, "_split_text", return_value=(["first", "second"], True)),
        patch.object(
            ext, "_extract_chunk_with_retry", side_effect=[_chunk("2026-0001"), _chunk("2026-0002")]
        ),
    ):
        rulings = ext.extract("first second", strict=True)
    assert _cases(rulings) == ["2026-0001", "2026-0002"]
    cache.put.assert_called_once()


# ---------------------------------------------------------------------------
# Per-field extraction: a failed chunk's result is never cached
# ---------------------------------------------------------------------------


def test_extract_fields_llm_partial_chunks_are_not_cached() -> None:
    from ingestion import llm_extract
    from ingestion.llm_providers import LLMResponse

    good = json.dumps(
        {
            "judge_name": "Good Judge",
            "hearing_date": "2026-03-09",
            "department": "1",
            "rulings": [
                {
                    "case_number": "001",
                    "case_title": "A v. B",
                    "outcome": "granted",
                    "motion_type": "msj",
                    "parties": [],
                }
            ],
        }
    )

    lock = threading.Lock()
    calls = [0]

    def answer(**kwargs: Any) -> LLMResponse | None:
        with lock:
            calls[0] += 1
            first = calls[0] == 1
        # One chunk answers, the other's call fails (chunks run concurrently).
        return LLMResponse(text=good, input_tokens=1, output_tokens=1) if first else None

    cache = MagicMock()
    cache.get.return_value = None
    with (
        patch.object(llm_extract, "_get_llm_cache", return_value=cache),
        patch.object(llm_extract, "call_llm", side_effect=answer),
    ):
        result = llm_extract.extract_fields_llm(
            "A" * 50_000 + "\f" + "B" * 50_000, "pdf", max_chars=60_000
        )
    # A field fill keeps the chunk that answered, but never caches it.
    assert result is not None and result.judge_name == "Good Judge"
    cache.put.assert_not_called()

    with (
        patch.object(llm_extract, "_get_llm_cache", return_value=cache),
        patch.object(llm_extract, "call_llm", return_value=LLMResponse(good, 1, 1)),
    ):
        llm_extract.extract_fields_llm("A" * 50_000 + "\f" + "C" * 50_000, "pdf", max_chars=60_000)
    cache.put.assert_called_once()


# ---------------------------------------------------------------------------
# Worker: a 51-page PDF with one failing page writes no split
# ---------------------------------------------------------------------------


def _oc_worker(multimodal: object, *, key_has_rows: bool = False) -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    text_extractor = MagicMock()
    text_extractor.extract.side_effect = AssertionError("no text fallback after a partial split")
    worker._get_framework_extractor = lambda: text_extractor  # type: ignore[method-assign]
    worker._get_county_extractor = lambda *a, **k: text_extractor  # type: ignore[method-assign]
    worker._get_multimodal_extractor = lambda **k: multimodal  # type: ignore[method-assign]
    worker._get_connection = MagicMock()  # type: ignore[method-assign]
    worker._key_has_rows = MagicMock(return_value=key_has_rows)  # type: ignore[method-assign]
    return worker


def _oc_event(pdf: bytes, *, reingest: bool) -> dict[str, Any]:
    event = {
        "document_id": "f2a60e90-cfdd-519a-b708-c32bbee55000",
        "scraper_id": "reingest-ca-orange" if reingest else "ca-orange-tentatives",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "5000" * 16,
        "s3_key": "ca/orange/superior_court/raw/5000.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": pdf.decode("latin-1"),
        "capture_timestamp": "2026-11-14T18:00:00+00:00",
    }
    if reingest:
        event["_replace_split_set"] = True
    return event


class _Events(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "telemetry_event", None) == SPLIT_EXTRACTION_FAILED_EVENT:
            self.records.append(record)


@pytest.fixture
def failure_events() -> Any:
    handler = _Events()
    log = logging.getLogger("ingestion.worker")
    log.addHandler(handler)
    try:
        yield handler.records
    finally:
        log.removeHandler(handler)


def test_failed_page_partial_reingest_writes_no_split_and_reports_the_key(
    pdf_51: bytes, failure_events: list[logging.LogRecord]
) -> None:
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        patch.object(IngestionWorker, "_write_one_ruling", autospec=True) as write_one,
        pytest.raises(SplitExtractionFailedError) as raised,
    ):
        _oc_worker(_extractor()).process_event(_oc_event(pdf_51, reingest=True))

    assert "PartialExtractionError" in str(raised.value)
    write_batch.assert_not_called()
    write_one.assert_not_called()
    assert [r.action for r in failure_events] == ["reported_failed"]  # type: ignore[attr-defined]


def test_failed_page_partial_live_capture_of_a_key_with_rows_writes_nothing(
    pdf_51: bytes, failure_events: list[logging.LogRecord]
) -> None:
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        patch.object(IngestionWorker, "_write_one_ruling", autospec=True) as write_one,
    ):
        _oc_worker(_extractor(), key_has_rows=True).process_event(_oc_event(pdf_51, reingest=False))

    write_batch.assert_not_called()
    write_one.assert_not_called()
    assert [r.action for r in failure_events] == ["left_key_alone"]  # type: ignore[attr-defined]


@patch("ingestion.worker.insert_validation_result")
def test_failed_page_partial_live_capture_of_a_new_key_writes_the_answered_rows(
    _validation: MagicMock, pdf_51: bytes, failure_events: list[logging.LogRecord]
) -> None:
    llm = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=llm),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        patch.object(IngestionWorker, "_write_one_ruling", autospec=True) as write_one,
    ):
        _oc_worker(_extractor()).process_event(_oc_event(pdf_51, reingest=False))

    write_one.assert_not_called()
    write_batch.assert_called_once()
    children = write_batch.call_args.args[3].children
    assert sorted(c.case_number for c in children) == _answered_cases()
    (event,) = failure_events
    assert event.action == "partial_split_written"  # type: ignore[attr-defined]
    assert event.failed_parts == [FAILED_PAGE - 1]  # type: ignore[attr-defined]


@patch("ingestion.worker.insert_validation_result")
def test_failed_page_partial_retry_succeeds_and_sends_only_the_failed_page(
    _validation: MagicMock, pdf_51: bytes
) -> None:
    cache, _s3 = _cache()
    multimodal = _extractor(cache)
    first = _PagedLlm(pdf_51, failing={FAILED_PAGE})
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=first),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        pytest.raises(SplitExtractionFailedError),
    ):
        _oc_worker(multimodal).process_event(_oc_event(pdf_51, reingest=True))
    write_batch.assert_not_called()

    retry = _PagedLlm(pdf_51, failing=set())
    with (
        patch("ingestion.llm_providers.call_llm_with_images", side_effect=retry),
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
    ):
        _oc_worker(multimodal).process_event(_oc_event(pdf_51, reingest=True))
    assert retry.calls == [FAILED_PAGE]
    write_batch.assert_called_once()
    children = write_batch.call_args.args[3].children
    assert sorted(c.case_number for c in children) == sorted(RULING_PAGES.values())


def test_failed_chunk_partial_text_county_reingest_reports_the_key(
    failure_events: list[logging.LogRecord],
) -> None:
    """A text-first county (Ventura's config): one chunk failed."""
    worker = _oc_worker(MagicMock())
    partial = PartialExtractionError(
        "text extraction failed on chunks [1] of 2",
        rulings=[ExtractedRuling(extracted_case_number="2026-0001", ruling_text="GRANTED.")],
        failed_parts=[1],
    )
    county = MagicMock()
    county.extract.side_effect = partial
    worker._get_county_extractor = lambda *a, **k: county  # type: ignore[method-assign]
    event = {
        **_oc_event(b"Calendar text", reingest=True),
        "county": "Ventura",
        "scraper_id": "reingest-ca-ventura",
        "s3_key": "ca/ventura/superior_court/raw/5000.html",
        "content_format": "html",
        "ruling_text": "Case No. 2026-0001 The motion is GRANTED. " * 20,
    }
    with (
        patch.object(IngestionWorker, "_write_split_batch", autospec=True) as write_batch,
        pytest.raises(SplitExtractionFailedError, match="PartialExtractionError"),
    ):
        worker.process_event(event)
    write_batch.assert_not_called()
    assert [r.action for r in failure_events] == ["reported_failed"]  # type: ignore[attr-defined]
