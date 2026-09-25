"""Regression tests for #4714: OC rulings dropped as ``ruling_empty_text_dropped``.

The dropped Orange County rows were not image-only PDFs.  Every affected PDF
has a full text layer.  The drops came from the multimodal fused-row split
(#2500).  OC prints the case number in the left column below the caption, and
the LLM's ``case_info`` often carries a second "v." caption after it: a repeat
of the row's caption, a cited case, or a role-literal placeholder.
``_split_fused_case_info`` gives the number to that second caption, so:

* the real ruling row keeps its text but loses its case number (stored under
  an ``UNKNOWN-<id>`` case), and
* a textless tail row gets the case number and is dropped with
  ``data_quality.ruling_empty_text_dropped`` on every run.

Fixture ``oc_mccartney_c61_fused_tail.pdf`` is a real OC Dept C61 PDF
(dev S3 ``ca/orange/superior_court/raw/1a16c171...pdf``, document
c29377b9-... in the issue).  ``oc_mccartney_c61_fused_tail_llm_rows.json`` is
the dev LLM-cache entry for it, with three fused tails: rows 4, 6 and 10.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pdfplumber
import pytest
import structlog

from framework.base import BaseScraper
from framework.llm_extractor import (
    LlmExtractor,
    _apply_pdf_post_join_filters,
    _reattach_fused_tail_case_numbers,
)
from framework.llm_schema import ExtractedRuling
from framework.models import CapturedDocument, ContentFormat, ScraperConfig
from ingestion.split_ids import make_split_document_id
from ingestion.worker import IngestionWorker

FIXTURES = Path(__file__).parent / "fixtures"
PDF_FIXTURE = FIXTURES / "oc_mccartney_c61_fused_tail.pdf"
ROWS_FIXTURE = FIXTURES / "oc_mccartney_c61_fused_tail_llm_rows.json"

# (entry_number, case number printed in that row's case-number column)
EXPECTED_REATTACHED = {
    4: "2026-01542409",  # Gelt Oasis Exchange, LLC vs. Monroe
    5: "2023-01367887",  # Flight Phase I Owner, LLC vs. Incipio, LLC
    8: "2026-01588949",  # 22751 EL Prado LLC vs. Triplett
}


def _fixture_rulings() -> list[ExtractedRuling]:
    return [ExtractedRuling(**row) for row in json.loads(ROWS_FIXTURE.read_text())]


def _ruling(
    *,
    text: str | None,
    case_number: str | None = None,
    entry: int | None = None,
    title: str | None = "Smith vs. Jones",
) -> ExtractedRuling:
    return ExtractedRuling(
        extracted_case_number=case_number,
        extracted_case_title=title,
        ruling_text=text,
        entry_number=entry,
    )


# ---------------------------------------------------------------------------
# The fixture PDF: a text layer, and the case numbers are printed in the
# ruling rows (ground truth for the re-attachment)
# ---------------------------------------------------------------------------


def test_fixture_pdf_has_text_layer_with_case_numbers_in_ruling_rows() -> None:
    with pdfplumber.open(PDF_FIXTURE) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Not image-only: thousands of characters of extractable text.
    assert len(text) > 10_000
    # Each number is printed at the head of its own entry row.
    assert "4 30-2026-01542409" in text
    assert "5 30-2023-01367887" in text
    assert "8 30-2026-01588949" in text


def test_cached_fixture_rows_reproduce_the_bug_shape() -> None:
    """The cached LLM rows show the real rows without a number and textless tails with one."""
    rows = _fixture_rulings()
    by_entry = {r.entry_number: r for r in rows if r.entry_number is not None}
    for entry, case_number in EXPECTED_REATTACHED.items():
        assert by_entry[entry].extracted_case_number is None
        assert by_entry[entry].ruling_text
        tails = [
            r for r in rows if r.extracted_case_number == case_number and r.entry_number is None
        ]
        assert len(tails) == 1
        assert not tails[0].ruling_text


# ---------------------------------------------------------------------------
# Cache-hit path through extract_from_pdf with the fixture PDF bytes
# ---------------------------------------------------------------------------


def _extractor_with_cache(cache: object) -> LlmExtractor:
    extractor = LlmExtractor.__new__(LlmExtractor)
    extractor._provider = "google"
    extractor._model = "stub"
    extractor._client = None
    extractor._max_retries = 0
    extractor._base_delay = 0.0
    extractor._max_delay = 0.0
    extractor._max_output_tokens = 32768
    extractor._max_chars_per_chunk = 80_000
    extractor._cache = cache
    extractor._bust_cache = False
    return extractor


def test_extract_from_pdf_cache_hit_reattaches_case_numbers() -> None:
    cache = MagicMock()
    cache.get.return_value = json.loads(ROWS_FIXTURE.read_text())
    extractor = _extractor_with_cache(cache)

    rulings = extractor.extract_from_pdf(PDF_FIXTURE.read_bytes())

    # Row count is unchanged, so split document IDs do not shift.
    assert len(rulings) == 12
    by_entry = {r.entry_number: r for r in rulings if r.entry_number is not None}
    for entry, case_number in EXPECTED_REATTACHED.items():
        assert by_entry[entry].extracted_case_number == case_number
        assert by_entry[entry].ruling_text
    # The dangling OC "30-" court prefix is trimmed from the caption.
    assert by_entry[4].extracted_case_title == "Gelt Oasis Exchange, LLC vs. Monroe"
    assert by_entry[5].extracted_case_title == "Flight Phase I Owner, LLC vs. Incipio, LLC"
    # Only rows with text hold these numbers (the worker skips textless tails).
    for case_number in EXPECTED_REATTACHED.values():
        holders = [r for r in rulings if r.extracted_case_number == case_number and r.ruling_text]
        assert len(holders) == 1


def test_reattach_is_idempotent_on_the_filter_tail() -> None:
    once = _apply_pdf_post_join_filters(_fixture_rulings())
    twice = _apply_pdf_post_join_filters([r.model_copy() for r in once])
    assert [r.model_dump() for r in once] == [r.model_dump() for r in twice]


# ---------------------------------------------------------------------------
# _reattach_fused_tail_case_numbers guard rails
# ---------------------------------------------------------------------------


def test_reattach_moves_single_tail_number_to_preceding_ruling() -> None:
    rows = [
        _ruling(text="The motion is GRANTED.", entry=4, title="Gelt vs. Monroe 30"),
        _ruling(text=None, case_number="2026-01542409", title="Gelt vs. Monroe"),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number == "2026-01542409"
    assert out[0].extracted_case_title == "Gelt vs. Monroe"
    assert out[1].ruling_text is None
    assert len(out) == 2
    # Idempotent: a second pass changes nothing.
    assert _reattach_fused_tail_case_numbers(out) == out


def test_reattach_keeps_existing_case_number() -> None:
    """A tail after a numbered row is a cited or sibling case, not this row's number."""
    rows = [
        _ruling(text="In re 206-208 W. Electric Ave.", case_number="2026-01574411", entry=10),
        _ruling(text=None, case_number="2026-01573506", title="Management Group v. Harrison"),
        _ruling(text="Kirk ruling body.", case_number="2026-01573506", entry=11),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number == "2026-01574411"
    assert out[1].extracted_case_number == "2026-01573506"


def test_reattach_skips_ambiguous_multi_number_run() -> None:
    """A consolidation ruling followed by two numbered tails is ambiguous, so skip it."""
    rows = [
        _ruling(text="Motion to consolidate is GRANTED.", entry=9, title="Simpson vs. Bendy"),
        _ruling(text=None, case_number="2026-01562015"),
        _ruling(text=None, case_number="2026-01572068"),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None
    assert [r.extracted_case_number for r in out[1:]] == ["2026-01562015", "2026-01572068"]


def test_reattach_skips_number_cited_in_ruling_body() -> None:
    rows = [
        _ruling(
            text="The related action, Ama Investors v. Pro Motorcars, Case No. 2023-01322592.",
            entry=10,
        ),
        _ruling(text=None, case_number="2023-01322592", title="Ama Investors v. Pro Motorcars"),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None
    assert out[1].extracted_case_number == "2023-01322592"


def test_reattach_skips_unhyphenated_number_cited_in_body() -> None:
    """Numbers with a short final segment ("N25-2112") are matched whole."""
    rows = [
        _ruling(text="See the related action, Case No. N25-2112.", entry=1),
        _ruling(text=None, case_number="N25-2112"),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None


def test_reattach_skips_number_already_held_by_another_ruling() -> None:
    rows = [
        _ruling(text="Body A.", entry=1),
        _ruling(text=None, case_number="2024-01111111"),
        _ruling(text="Body B.", case_number="2024-01111111", entry=2),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None


def test_reattach_ignores_textless_parent() -> None:
    rows = [
        _ruling(text=None, entry=2, title="National Funding vs. RM Realty"),
        _ruling(text=None, case_number="2024-01378889"),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None
    assert out[1].extracted_case_number == "2024-01378889"


# ---------------------------------------------------------------------------
# Worker: textless multimodal rows are skipped, not dropped as failures
# ---------------------------------------------------------------------------


def _make_worker() -> IngestionWorker:
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=MagicMock(),
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    worker._get_connection = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
    return worker


def _event() -> dict:
    return {
        "document_id": "11111111-2222-3333-4444-555555555555",
        "scraper_id": "ca-oc-tentatives-civil",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "content_format": "pdf",
        "s3_key": "ca/orange/superior_court/raw/1a16c171.pdf",
        "hearing_date": "2026-09-23",
    }


@patch("ingestion.worker.delete_stale_split_children", return_value=0)
def test_worker_skips_textless_multimodal_rows(
    _mock_delete: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = _apply_pdf_post_join_filters(_fixture_rulings())
    worker._multimodal_extractor = multimodal
    event = _event()

    with (
        patch.object(worker, "process_event") as dispatch,
        caplog.at_level(logging.INFO, logger="ingestion.worker"),
    ):
        handled = worker._llm_split_document(
            event, event["document_id"], "", "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )

    assert handled is True
    dispatched = [c.args[0] for c in dispatch.call_args_list]
    # 12 rows, 3 textless fused tails: only the 9 rulings with text are dispatched.
    assert len(dispatched) == 9
    assert all(e["ruling_text"] for e in dispatched)
    # Split IDs keep their original index, so existing children are not re-keyed.
    gelt = next(e for e in dispatched if e["case_number"] == "2026-01542409")
    assert gelt["_split_index"] == 3
    assert gelt["document_id"] == make_split_document_id(event["document_id"], 3)
    skipped = [r for r in caplog.records if r.getMessage() == "llm_split.textless_row_skipped"]
    assert len(skipped) == 3
    assert {r.row_class for r in skipped} == {"fused_tail"}
    assert not any("ruling_empty_text_dropped" in r.getMessage() for r in caplog.records)


@patch("ingestion.worker.delete_stale_split_children", return_value=0)
def test_worker_skips_textless_rows_without_emitting_drop_telemetry(
    _mock_delete: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """End to end through process_event: the fused tail never reaches deterministic validation."""
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = [
        _ruling(text=None, entry=2, title="National Funding vs. RM Realty"),
        _ruling(text=None, case_number="2024-01378889"),
    ]
    worker._multimodal_extractor = multimodal
    worker._fetch_raw_pdf_from_s3 = MagicMock(return_value=b"%PDF-1.7")  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="ingestion.worker"):
        worker.process_event({**_event(), "ruling_text": ""})

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("llm_split.textless_row_skipped") == 2
    assert not [
        r
        for r in caplog.records
        if getattr(r, "telemetry_event", None) == "data_quality.ruling_empty_text_dropped"
    ]


@patch("ingestion.worker.delete_stale_split_children", return_value=0)
def test_worker_skips_zero_ruling_pdf_with_no_text(
    _mock_delete: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A calendar-only PDF (zero rulings, no fallback text) is skipped cleanly."""
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = []
    worker._multimodal_extractor = multimodal
    event = _event()

    with (
        patch.object(worker, "process_event") as dispatch,
        caplog.at_level(logging.INFO, logger="ingestion.worker"),
    ):
        handled = worker._llm_split_document(
            event, event["document_id"], "", "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )

    assert handled is True
    dispatch.assert_not_called()
    messages = [r.getMessage() for r in caplog.records]
    assert "llm_split.zero_rulings_no_text_skipped" in messages


def test_worker_zero_ruling_pdf_with_text_still_falls_through() -> None:
    """When fallback text exists, keep the single-document path unchanged."""
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = []
    worker._multimodal_extractor = multimodal
    event = _event()

    handled = worker._llm_split_document(
        event,
        event["document_id"],
        "Case No. 2024-01234567 The motion is GRANTED.",
        "CA",
        "Orange",
        raw_pdf_bytes=b"%PDF-1.7",
    )

    assert handled is False


# ---------------------------------------------------------------------------
# Scraper: no capture-time "image-only" warning for deferred transcription
# ---------------------------------------------------------------------------


def _capture_warnings(defers: bool) -> list[dict]:
    config = ScraperConfig(
        scraper_id="test-oc",
        state="CA",
        county="Orange",
        court="Superior Court",
        target_urls=["https://example.com"],
    )
    doc = CapturedDocument(
        scraper_id=config.scraper_id,
        state=config.state,
        county=config.county,
        court=config.court,
        source_url="https://example.com/ruling.pdf",
        capture_timestamp=datetime.now(UTC),
        content_format=ContentFormat.PDF,
        raw_content=PDF_FIXTURE.read_bytes(),
        content_hash="",
    )

    class DeferringScraper(BaseScraper):
        defers_pdf_transcription = defers

        def fetch_documents(self) -> list[CapturedDocument]:
            return [doc]

        def parse_document(self, d: CapturedDocument) -> CapturedDocument:
            return d

    with structlog.testing.capture_logs() as cap_logs:
        DeferringScraper(config=config).run()
    return [e for e in cap_logs if "image-only PDF" in e.get("event", "")]


def test_deferring_scraper_does_not_warn_image_only() -> None:
    assert _capture_warnings(defers=True) == []


def test_non_deferring_scraper_still_warns_image_only() -> None:
    assert _capture_warnings(defers=False)


def test_oc_tentatives_scraper_defers_pdf_transcription() -> None:
    from courts.ca.oc_tentatives import OCTentativeRulingsScraper

    assert OCTentativeRulingsScraper.defers_pdf_transcription is True
