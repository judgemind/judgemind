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
from ingestion.worker import (
    REPLACE_SPLIT_SET_KEY,
    SPLIT_EXTRACTION_FAILED_EVENT,
    WHOLE_DOCUMENT_KEY,
    ExtractorUnavailableError,
    IngestionWorker,
    NoExtractableTextError,
    SplitExtractionFailedError,
    is_infrastructure_error,
)

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


@pytest.mark.parametrize("case_number", ["24-12345", "N25-2112", "26CV484550", "CVRI2401570"])
def test_reattach_only_for_oc_shaped_numbers(case_number: str) -> None:
    """Other counties' fused rows may really mean "the number belongs to the second caption"."""
    rows = [
        _ruling(text="The motion is GRANTED.", entry=1),
        _ruling(text=None, case_number=case_number),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number is None


@pytest.mark.parametrize("case_number", ["2026-01542409", "24-1394670", "2024-1432624"])
def test_reattach_accepts_oc_number_shapes(case_number: str) -> None:
    rows = [
        _ruling(text="The motion is GRANTED.", entry=1),
        _ruling(text=None, case_number=case_number),
    ]
    out = _reattach_fused_tail_case_numbers(rows)
    assert out[0].extracted_case_number == case_number


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


@pytest.mark.parametrize("replace_set", [True, False], ids=["reingest", "live"])
@patch("ingestion.worker.record_ingest_skip", return_value=True)
@patch("ingestion.worker.delete_stale_split_children", return_value=0)
@patch("ingestion.worker.write_split_rulings", return_value={})
def test_all_rows_textless_skip_is_recorded(
    mock_write: MagicMock,
    _mock_delete: MagicMock,
    mock_record: MagicMock,
    replace_set: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OC calendar whose every row has an empty tentative cell writes no
    ruling and records ``no_ruling_text`` in ``telemetry.ingest_skips``, in
    the empty split set's transaction, so the raw-key reconciler counts the
    key as skipped, not lost (#5015, #5032).  The shape of dev keys
    ``dbdf0d10``, ``15eb8f02`` and ``9d74992b``: numbered entries
    (``row_class=empty_cell``) with a caption and no ruling body."""
    worker = _make_worker()
    worker._indexer = MagicMock()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = [
        _ruling(text="", entry=n, case_number=f"2026-0150000{n}", title=f"Party {n} vs. Other")
        for n in (1, 2, 3)
    ]
    worker._multimodal_extractor = multimodal
    worker._fetch_raw_pdf_from_s3 = MagicMock(return_value=b"%PDF-1.7")  # type: ignore[method-assign]
    event = {**_event(), "ruling_text": ""}
    if replace_set:
        event[REPLACE_SPLIT_SET_KEY] = True

    with caplog.at_level(logging.INFO, logger="ingestion.worker"):
        worker.process_event(event)

    skipped = [r for r in caplog.records if r.getMessage() == "llm_split.textless_row_skipped"]
    assert [r.row_class for r in skipped] == ["empty_cell"] * 3
    # The key's split set is replaced by the empty one: no ruling is written.
    mock_write.assert_called_once()
    assert mock_write.call_args.kwargs["children"] == []
    mock_record.assert_called_once()
    kwargs = mock_record.call_args.kwargs
    assert kwargs["s3_key"] == event["s3_key"]
    assert kwargs["reason"] == "no_ruling_text"
    assert kwargs["detail"]["textless_rows"] == 3
    assert mock_record.call_args.args[0] is worker._get_connection.return_value
    worker._get_connection.return_value.commit.assert_called()


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


def test_worker_zero_ruling_pdf_with_text_is_the_split_s_one_child() -> None:
    """When fallback text exists, the document is written as one ruling: the
    one child of the key's split set (#4940), so the split-set write replaces
    the key's earlier children instead of leaving them next to it."""
    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.return_value = []
    worker._multimodal_extractor = multimodal
    event = _event()

    with (
        patch.object(worker, "_dispatch_split_child") as dispatch,
        patch.object(worker, "_write_split_batch"),
    ):
        handled = worker._llm_split_document(
            event,
            event["document_id"],
            "Case No. 2024-01234567 The motion is GRANTED.",
            "CA",
            "Orange",
            raw_pdf_bytes=b"%PDF-1.7",
        )

    assert handled is True
    assert multimodal.extract_from_pdf.call_args.kwargs["strict"] is True
    batch, child = dispatch.call_args.args
    assert child[WHOLE_DOCUMENT_KEY] is True
    assert child["document_id"] == event["document_id"]
    assert batch.whole_document is True


def _failing_extractors(worker: IngestionWorker) -> MagicMock:
    """Make every extraction call fail: the multimodal page calls and the
    text fallback both raise ``ExtractionIncompleteError``."""
    from framework.llm_extractor import ExtractionIncompleteError

    multimodal = MagicMock()
    multimodal.extract_from_pdf.side_effect = ExtractionIncompleteError("PDF pages [0] failed")
    worker._multimodal_extractor = multimodal
    text = MagicMock()
    text.extract.side_effect = ExtractionIncompleteError("text extraction failed")
    return text


_DOC_TEXT = "Case No. 2024-01234567 The motion is GRANTED."


def test_worker_failed_pdf_extraction_on_reingest_writes_nothing() -> None:
    """A reingest whose extraction failed (every page's API call failed, then
    the text fallback's) is not a verdict on the document: no ruling write
    happens, neither a split-set write nor the whole document as a ruling of
    its own, and the key is reported as failed (#4948).  Before, the worker
    wrote the whole document on the parent row, next to the key's split
    children."""
    worker = _make_worker()
    text = _failing_extractors(worker)
    event = {**_event(), "ruling_text": _DOC_TEXT, REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
        patch.object(worker, "_fetch_raw_pdf_from_s3", return_value=b"%PDF-1.7"),
        patch.object(worker, "_dispatch_split_child") as dispatch,
        patch.object(worker, "_write_split_batch") as split_write,
        patch.object(worker, "_write_one_ruling") as single_write,
        pytest.raises(SplitExtractionFailedError, match="PDF pages.*text extraction failed"),
    ):
        worker.process_event(event)

    assert worker._multimodal_extractor.extract_from_pdf.call_args.kwargs["strict"] is True
    assert text.extract.call_args.kwargs["strict"] is True
    dispatch.assert_not_called()
    split_write.assert_not_called()
    single_write.assert_not_called()


def test_worker_failed_text_extraction_on_reingest_writes_nothing() -> None:
    """The same for a county extracted from text only (no multimodal call):
    the text extractor's failed chunk is reported, nothing is written
    (#4948)."""
    from framework.llm_extractor import ExtractionIncompleteError

    worker = _make_worker()
    text = MagicMock()
    text.extract.side_effect = ExtractionIncompleteError("text extraction failed on chunks [1]")
    event = {
        **_event(),
        "county": "Ventura",
        "scraper_id": "ca-ventura-tentatives",
        "content_format": "html",
        "s3_key": "ca/ventura/superior_court/raw/abc.html",
        "ruling_text": _DOC_TEXT,
        REPLACE_SPLIT_SET_KEY: True,
    }

    with (
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
        patch.object(worker, "_write_split_batch") as split_write,
        patch.object(worker, "_write_one_ruling") as single_write,
        pytest.raises(SplitExtractionFailedError, match="chunks"),
    ):
        worker.process_event(event)

    assert text.extract.call_args.kwargs["strict"] is True
    split_write.assert_not_called()
    single_write.assert_not_called()


@pytest.mark.parametrize(("key_has_rows", "written"), [(True, False), (False, True)])
def test_worker_failed_extraction_on_live_capture(key_has_rows: bool, written: bool) -> None:
    """A live capture whose extraction failed does not raise.  It leaves a
    key that already holds rows alone, and writes a new key's document on
    its own through the single-document fallback, as before (#4948)."""
    worker = _make_worker()
    text = _failing_extractors(worker)
    event = {**_event(), "ruling_text": _DOC_TEXT}

    with (
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
        patch.object(worker, "_fetch_raw_pdf_from_s3", return_value=b"%PDF-1.7"),
        patch.object(worker, "_key_has_rows", return_value=key_has_rows) as has_rows,
        patch.object(worker, "_write_split_batch") as split_write,
        patch.object(worker, "_write_one_ruling") as single_write,
    ):
        worker.process_event(event)

    has_rows.assert_called_once_with(event["s3_key"])
    split_write.assert_not_called()
    assert single_write.called is written
    # A key left alone ends the read's transaction; a new key's fallback
    # commits its incomplete record before the write (#5008).
    assert worker._get_connection().commit.called
    cur = worker._get_connection().cursor.return_value.__enter__.return_value
    recorded = any(
        "INSERT INTO telemetry.ingest_incomplete" in c.args[0] for c in cur.execute.call_args_list
    )
    assert recorded is written
    if written:
        assert single_write.call_args.args[0].event_data["_incomplete_fallback"] is True


def test_worker_key_has_rows_reads_the_documents_on_the_key() -> None:
    worker = _make_worker()
    cur = worker._get_connection().cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = None
    assert worker._key_has_rows("ca/orange/k.pdf") is False
    assert cur.execute.call_args.args[1] == ("ca/orange/k.pdf",)
    cur.fetchone.return_value = (1,)
    assert worker._key_has_rows("ca/orange/k.pdf") is True


def test_worker_text_with_nothing_to_read_is_not_a_verdict() -> None:
    """Multimodal failed and the fallback text is blank: the empty result says
    nothing about the document, so no whole-document split (#4940); on a
    reingest the key is reported as failed (#4948)."""
    from framework.llm_extractor import ExtractionIncompleteError

    worker = _make_worker()
    multimodal = MagicMock()
    multimodal.extract_from_pdf.side_effect = ExtractionIncompleteError("no PDF page rendered")
    worker._multimodal_extractor = multimodal
    text = MagicMock()
    text.extract.return_value = []
    event = {**_event(), REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
        patch.object(worker, "_dispatch_split_child") as dispatch,
        pytest.raises(SplitExtractionFailedError, match="no PDF page rendered"),
    ):
        worker._llm_split_document(
            event, event["document_id"], "   ", "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )

    dispatch.assert_not_called()


def test_worker_failed_multimodal_with_no_text_extractor_is_a_failure() -> None:
    """Multimodal failed and no text extractor is configured to fall back
    on: still a failed extraction, reported on a reingest (#4948)."""
    worker = _make_worker()
    _failing_extractors(worker)
    event = {**_event(), REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_get_framework_extractor", return_value=None),
        patch.object(worker, "_get_county_extractor", return_value=None),
        pytest.raises(SplitExtractionFailedError),
    ):
        worker._llm_split_document(
            event, event["document_id"], _DOC_TEXT, "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )


def test_worker_with_no_extractor_configured_falls_back_to_the_single_document() -> None:
    """No extractor configured in ``optional`` extraction mode (the explicit
    opt-in of a run without LLM keys, set by the suite's conftest) is not a
    failed extraction: the single-document fallback (per-field and regex
    extraction) is the extraction, as before (#4948, #4953)."""
    worker = _make_worker()
    assert worker._extraction_required is False
    event = {
        **_event(),
        "county": "Ventura",
        "content_format": "html",
        "ruling_text": _DOC_TEXT,
        REPLACE_SPLIT_SET_KEY: True,
    }

    with (
        patch.object(worker, "_get_framework_extractor", return_value=None),
        patch.object(worker, "_get_county_extractor", return_value=None),
    ):
        handled = worker._llm_split_document(
            event, event["document_id"], _DOC_TEXT, "CA", "Ventura"
        )

    assert handled is False


def _required_worker() -> IngestionWorker:
    """A worker in ``required`` extraction mode (#4953) whose extractors
    have gone missing after startup (the runtime half of the check)."""
    worker = _make_worker()
    worker._extraction_required = True
    worker._extractor_init_errors["framework"] = "AuthenticationError: bad key"
    worker._extractor_init_errors["county"] = "AuthenticationError: bad key"
    worker._extractor_init_errors["multimodal"] = "ValueError: No Google API key provided"
    return worker


_VENTURA_REINGEST = {
    "county": "Ventura",
    "scraper_id": "ca-ventura-tentatives",
    "content_format": "html",
    "s3_key": "ca/ventura/superior_court/raw/abc.html",
    "ruling_text": _DOC_TEXT,
}


@pytest.mark.parametrize("reingest", [True, False])
def test_worker_extractor_unavailable_writes_nothing(
    reingest: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """In ``required`` mode a text extractor that cannot be built is a
    failed extraction, not "no extractor configured": nothing is written
    (no split-set write, no whole document as a ruling of its own) and
    ``ExtractorUnavailableError`` names the init error, on a reingest and
    on a live capture of a new key alike (#4953)."""
    worker = _required_worker()
    event = {**_event(), **_VENTURA_REINGEST}
    if reingest:
        event[REPLACE_SPLIT_SET_KEY] = True

    with (
        patch.object(worker, "_get_framework_extractor", return_value=None),
        patch.object(worker, "_get_county_extractor", return_value=None),
        patch.object(worker, "_key_has_rows", return_value=False) as has_rows,
        patch.object(worker, "_write_split_batch") as split_write,
        patch.object(worker, "_write_one_ruling") as single_write,
        caplog.at_level(logging.WARNING, logger="ingestion.worker"),
        pytest.raises(ExtractorUnavailableError, match="extractor unavailable: .*bad key"),
    ):
        worker.process_event(event)

    split_write.assert_not_called()
    single_write.assert_not_called()
    has_rows.assert_not_called()
    failed = [
        r
        for r in caplog.records
        if getattr(r, "telemetry_event", None) == SPLIT_EXTRACTION_FAILED_EVENT
    ]
    assert [r.action for r in failed] == ["extractor_unavailable"]


def test_worker_extractor_unavailable_multimodal_does_not_fall_back_to_text() -> None:
    """A required multimodal extractor that cannot be built fails the split
    before any text-extractor call (#4953)."""
    worker = _required_worker()
    text = MagicMock()
    event = {**_event(), "ruling_text": _DOC_TEXT, REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_get_multimodal_extractor", return_value=None),
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
        patch.object(worker, "_dispatch_split_child") as dispatch,
        pytest.raises(ExtractorUnavailableError, match="multimodal: ValueError"),
    ):
        worker._llm_split_document(
            event, event["document_id"], _DOC_TEXT, "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )

    text.extract.assert_not_called()
    dispatch.assert_not_called()


def test_worker_extractor_unavailable_after_failed_multimodal_names_both() -> None:
    """Multimodal failed and the required text extractor is missing: the
    error names both (#4948, #4953)."""
    worker = _required_worker()
    _failing_extractors(worker)
    event = {**_event(), REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_get_framework_extractor", return_value=None),
        patch.object(worker, "_get_county_extractor", return_value=None),
        pytest.raises(ExtractorUnavailableError, match="PDF pages.*extractor unavailable"),
    ):
        worker._llm_split_document(
            event, event["document_id"], _DOC_TEXT, "CA", "Orange", raw_pdf_bytes=b"%PDF-1.7"
        )


def test_extractor_unavailable_is_an_infrastructure_error() -> None:
    """The live worker keeps the message and exits for restart, rather than
    dead-lettering it after retries (#4953); a reingest still counts it as
    a failed key."""
    exc = ExtractorUnavailableError("extractor unavailable: framework: x")
    assert is_infrastructure_error(exc)
    assert isinstance(exc, SplitExtractionFailedError)
    assert not is_infrastructure_error(SplitExtractionFailedError("x"))


# ---------------------------------------------------------------------------
# #4968: a document no extractor could run on is a failed extraction
# ---------------------------------------------------------------------------

_VENTURA_PDF = {
    "county": "Ventura",
    "scraper_id": "ca-ventura-tentatives",
    "content_format": "pdf",
    "s3_key": "ca/ventura/superior_court/raw/abc.pdf",
}


@pytest.mark.parametrize("raw_pdf_bytes", [b"%PDF-1.7 scanned", None])
def test_text_first_county_pdf_without_text_is_a_failed_extraction(
    raw_pdf_bytes: bytes | None,
) -> None:
    """Ventura extracts from text only.  A PDF with no text layer gives its
    extractor nothing to read, so no extractor runs: that is a failed
    extraction, not "nothing to extract" (#4968).  Before, the split left
    ``extraction_failed`` unset, a reingest treated the key as done, and
    ``--write-failed-manifest`` never retried it.  With the raw bytes (a
    PDF read for its text layer) or without them (a multimodal county
    whose PDF could not be fetched takes the same branch)."""
    from ingestion.worker import NO_EXTRACTABLE_TEXT, _SplitBatch

    worker = _required_worker()
    text = MagicMock()
    event = {**_event(), **_VENTURA_PDF}
    batch = _SplitBatch(parent_document_id=event["document_id"])

    with (
        patch.object(worker, "_get_framework_extractor", return_value=text),
        patch.object(worker, "_get_county_extractor", return_value=text),
    ):
        split = worker._split_document(
            event,
            event["document_id"],
            "",
            "CA",
            "Ventura",
            raw_pdf_bytes=raw_pdf_bytes,
            batch=batch,
        )

    assert split is False
    assert batch.extraction_failed == NO_EXTRACTABLE_TEXT
    assert batch.extractor_unavailable is False
    assert batch.whole_document is False
    text.extract.assert_not_called()


def test_text_first_county_pdf_without_text_is_reported_on_reingest() -> None:
    """End to end: the reingest raises ``NoExtractableTextError``, the
    ``SplitExtractionFailedError`` its per-key handler reports apart from the
    retry list (#4983), and writes nothing (#4968)."""
    worker = _required_worker()
    event = {**_event(), **_VENTURA_PDF, "ruling_text": "", REPLACE_SPLIT_SET_KEY: True}

    with (
        patch.object(worker, "_write_split_batch") as split_write,
        patch.object(worker, "_write_one_ruling") as single_write,
        pytest.raises(NoExtractableTextError, match="no extractable text"),
    ):
        worker.process_event(event)

    split_write.assert_not_called()
    single_write.assert_not_called()


def test_text_first_county_pdf_without_text_leaves_a_live_key_alone() -> None:
    """A live capture of a key that already holds rows writes nothing and
    does not raise (#4948, #4968)."""
    worker = _required_worker()
    event = {**_event(), **_VENTURA_PDF}

    with patch.object(worker, "_key_has_rows", return_value=True):
        handled = worker._llm_split_document(event, event["document_id"], "", "CA", "Ventura")

    assert handled is True


def test_scraper_extracted_county_without_text_is_its_whole_document() -> None:
    """A county whose scraper extracts every field (``ExtractionMethod.NONE``)
    runs no extractor, with or without text: the document is the split's
    one child (#4940), not a failed extraction (#4968)."""
    from ingestion.worker import _SplitBatch

    worker = _required_worker()
    event = {**_event(), "state": "FEDERAL", "county": "Federal", "content_format": "html"}
    batch = _SplitBatch(parent_document_id=event["document_id"])

    split = worker._split_document(
        event, event["document_id"], "", "FEDERAL", "Federal", batch=batch
    )

    assert split is False
    assert batch.whole_document is True
    assert batch.extraction_failed is None


def test_optional_mode_pdf_without_text_is_not_a_failure() -> None:
    """In ``optional`` mode (no LLM keys) the single-document fallback is the
    extraction, as before (#4953)."""
    from ingestion.worker import _SplitBatch

    worker = _make_worker()
    assert worker._extraction_required is False
    event = {**_event(), **_VENTURA_PDF}
    batch = _SplitBatch(parent_document_id=event["document_id"])

    for raw_pdf_bytes in (b"%PDF-1.7 scanned", None):
        split = worker._split_document(
            event,
            event["document_id"],
            "",
            "CA",
            "Ventura",
            raw_pdf_bytes=raw_pdf_bytes,
            batch=batch,
        )
        assert split is False
        assert batch.extraction_failed is None


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
