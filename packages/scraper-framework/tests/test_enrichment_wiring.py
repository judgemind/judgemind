"""Tests for enrichment wiring in the ingestion worker.

Verifies that EnrichmentEngine is called from process_event() with correct
arguments and that its results are applied properly to case_number, judge_name,
and logging output.

All external dependencies (Postgres, Redis, OpenSearch, S3) are mocked.
"""

from __future__ import annotations

import logging
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from framework.enrichment import CaseMatch, EnrichmentResult, JudgeResolution
from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers (mirrors test_ingestion.py patterns)
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict:
    """Return a minimal valid DocumentCapturedEvent payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://www.lacourt.org/tentativerulings/1",
        "content_format": "html",
        "content_hash": "abc123",
        "s3_key": "ca/los_angeles/superior_court/raw/abc123.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "case_number": "23STCV12345",
        "department": "Dept. 1",
        "judge_name": "Smith, John A.",
        "ruling_text": "The motion for summary judgment is GRANTED.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair configured for persistent connection."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _make_worker(pg_dsn: str = "postgresql://localhost/test") -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked OpenSearch and S3."""
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    worker = IngestionWorker(
        redis_client=redis_mock,
        pg_dsn=pg_dsn,
        opensearch_client=os_mock,
        s3_client=s3_mock,
        archive_bucket="test-bucket",
    )
    # Disable enrichment client by default so process_event tests don't trigger
    # live LLM calls that now raise LlmEnrichmentExhaustedError (#3549).
    worker._enrichment_client = None
    # These events are live captures of new keys: when their split
    # extraction fails (no LLM key in tests), the document is written on its
    # own.  A key that already holds rows would be left alone (#4948).
    worker._key_has_rows = lambda _s3_key: False  # type: ignore[method-assign]
    return worker, os_mock


def _no_match_enrichment_result() -> EnrichmentResult:
    """Return an EnrichmentResult with exact case match, no judge changes."""
    return EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id=None,
            canonical_name=None,
            match_type="new",
            confidence=0.0,
        ),
        party_resolutions=[],
        flags=[],
    )


# ---------------------------------------------------------------------------
# Tests — Enrichment wiring in process_event()
# ---------------------------------------------------------------------------


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_called_with_correct_args(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """EnrichmentEngine is instantiated with conn and enrich() called with correct params."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    mock_engine = MagicMock()
    mock_engine.enrich.return_value = _no_match_enrichment_result()
    mock_engine_cls.return_value = mock_engine

    event = _make_event()
    worker.process_event(event)

    # EnrichmentEngine instantiated with the connection
    mock_engine_cls.assert_called_once_with(mock_conn)

    # enrich() called with correct arguments
    mock_engine.enrich.assert_called_once()
    call_kwargs = mock_engine.enrich.call_args.kwargs
    assert call_kwargs["case_number"] == "23STCV12345"
    assert call_kwargs["court_id"] == "court-uuid-1"
    assert call_kwargs["judge_name"] == "Smith, John A."
    assert call_kwargs["department"] == "Dept. 1"
    assert call_kwargs["hearing_date"] == date(2026, 3, 5)
    assert call_kwargs["parties"] == []  # no parties in default event


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_fuzzy_match_updates_case_number(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When enrichment returns a fuzzy case match, case_number is updated."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    fuzzy_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12346",
            match_type="fuzzy",
            confidence=0.9,
            corrected_from="23STCV12345",
            levenshtein_distance=1,
            party_overlap=0.5,
        ),
        judge_resolution=JudgeResolution(
            judge_id=None,
            canonical_name=None,
            match_type="new",
            confidence=0.0,
        ),
        party_resolutions=[],
        flags=["case_number_corrected: 23STCV12345 -> 23STCV12346 (dist=1, overlap=0.50)"],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = fuzzy_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(case_number="23STCV12345")
    with caplog.at_level(logging.INFO):
        worker.process_event(event)

    # Verify the case upsert got the corrected case_number.  The search doc
    # is built from the committed derived.* row, so this is how it reaches
    # OpenSearch (#4785).
    case_calls = [c for c in mock_cur.execute.call_args_list if "INTO cases" in str(c)]
    assert case_calls
    assert "23STCV12346" in case_calls[0][0][1]
    assert "23STCV12345" not in case_calls[0][0][1]

    # Verify log message
    assert any("Enrichment corrected case_number" in r.getMessage() for r in caplog.records)


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_fuzzy_skipped_for_case_number_from_source(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """#4866: a case number a deterministic splitter read from the source
    (``_case_number_from_source``) is kept; the fuzzy match would move the
    ruling onto an older near-miss case."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",), (True,)]
    mock_cur.rowcount = 1

    mock_engine = MagicMock()
    mock_engine.enrich.return_value = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12346",
            match_type="fuzzy",
            confidence=0.9,
            corrected_from="23STCV12345",
            levenshtein_distance=1,
            party_overlap=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id=None, canonical_name=None, match_type="new", confidence=0.0
        ),
        party_resolutions=[],
        flags=[],
    )
    mock_engine_cls.return_value = mock_engine

    worker.process_event(_make_event(case_number="23STCV12345", _case_number_from_source=True))

    case_calls = [c for c in mock_cur.execute.call_args_list if "INTO cases" in str(c)]
    assert case_calls
    assert "23STCV12345" in case_calls[0][0][1]
    assert "23STCV12346" not in case_calls[0][0][1]


def _fuzzy_result(printed: str, near_miss: str) -> EnrichmentResult:
    return EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number=near_miss,
            match_type="fuzzy",
            confidence=0.9,
            corrected_from=printed,
            levenshtein_distance=2,
            party_overlap=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id=None, canonical_name=None, match_type="new", confidence=0.0
        ),
        party_resolutions=[],
        flags=[],
    )


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_fuzzy_skipped_when_text_prints_number_but_not_match(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """#4915: the document prints ``CVRI2401570``; the fuzzy matcher's
    ``CVRI2401073`` (an existing, different case) is not in it, so the
    printed number is kept.  On dev this filed rulings under another case."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",), (True,)]
    mock_cur.rowcount = 1
    mock_engine_cls.return_value.enrich.return_value = _fuzzy_result("CVRI2401570", "CVRI2401073")
    text = "CVRI2401570 Smith v. Jones. " + "The motion is granted in part and denied. " * 6

    worker.process_event(_make_event(case_number="CVRI2401570", ruling_text=text))

    case_calls = [c for c in mock_cur.execute.call_args_list if "INTO cases" in str(c)]
    assert case_calls
    assert "CVRI2401570" in case_calls[0][0][1]
    assert "CVRI2401073" not in case_calls[0][0][1]


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_fuzzy_applied_when_text_prints_the_match(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Control for #4915: the document prints both numbers, so the gate
    does not refute the fuzzy match and the correction stands."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",), (True,)]
    mock_cur.rowcount = 1
    mock_engine_cls.return_value.enrich.return_value = _fuzzy_result("CVRI2401570", "CVRI2401073")
    text = "CVRI2401570 (see CVRI2401073). " + "The motion is granted in part and denied. " * 6

    worker.process_event(_make_event(case_number="CVRI2401570", ruling_text=text))

    case_calls = [c for c in mock_cur.execute.call_args_list if "INTO cases" in str(c)]
    assert case_calls
    assert "CVRI2401073" in case_calls[0][0][1]


def _fuzzy_case_upserted(event: dict) -> str:
    """Run *event* with the fuzzy matcher proposing ``CVRI2401073`` for
    ``CVRI2401570``; return the case number the case upsert received."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_cur.fetchone.side_effect = [("court-uuid-1",), ("case-uuid-1",), (True,)]
    mock_cur.rowcount = 1
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.EnrichmentEngine") as mock_engine_cls,
        patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1"),
        patch.object(IngestionWorker, "_llm_split_document", return_value=False),
    ):
        mock_psycopg.connect.return_value = mock_conn
        mock_engine_cls.return_value.enrich.return_value = _fuzzy_result(
            "CVRI2401570", "CVRI2401073"
        )
        worker.process_event(event)
    case_calls = [c for c in mock_cur.execute.call_args_list if "INTO cases" in str(c)]
    assert case_calls
    params = case_calls[0][0][1]
    return "CVRI2401073" if "CVRI2401073" in params else "CVRI2401570"


def test_fuzzy_refused_when_usable_text_prints_neither() -> None:
    """#4927: the gate refuses a correction the usable source text does not
    print, even when it does not print the (scraper's, trusted) original
    either.  Before, the correction stood unless the text printed the
    original."""
    text = "Tentative ruling. " + "The motion is granted in part and denied in part. " * 6
    event = _make_event(case_number="CVRI2401570", ruling_text=text)
    assert _fuzzy_case_upserted(event) == "CVRI2401570"


def test_fuzzy_applied_when_text_too_short_to_settle() -> None:
    """A whole document under 200 characters (a scanned PDF) is not
    evidence: the fuzzy correction goes ahead."""
    event = _make_event(case_number="CVRI2401570", ruling_text="Page 1")
    assert _fuzzy_case_upserted(event) == "CVRI2401073"


def test_fuzzy_refused_by_short_deterministic_child_text() -> None:
    """A deterministic split child's own text counts at any length: it
    prints ``CVRI2401570``, not the match, so the correction is refused.
    Before, child text under 200 characters allowed every correction."""
    event = _make_event(
        case_number="CVRI2401570",
        ruling_text="CVRI2401570: demurrer OVERRULED.",
        _split_processed=True,
        _original_document_id="aaaaaaaa-0000-0000-0000-00000000aaaa",
        _split_index=0,
        _split_count=2,
    )
    assert _fuzzy_case_upserted(event) == "CVRI2401570"


@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_fuzzy_guard_ignores_transcription(
    mock_psycopg: MagicMock, mock_engine_cls: MagicMock
) -> None:
    """#4927: an LLM split of a scanned PDF (no usable text layer).  The
    LLM misread ``CVRI2401073`` as ``CVRI2401570`` and its transcription of
    the ruling repeats the misread.  The transcription is LLM output, not
    evidence: the fuzzy correction to the real, existing ``CVRI2401073``
    goes ahead.  Before the fix ``_write_split_batch`` kept the
    transcription as the fuzzy guard's evidence whenever the document text
    was unusable, so the guard kept the misread."""
    from framework.llm_schema import ExtractedRuling, ExtractionOutcome

    worker, _ = _make_worker()
    mock_conn, _cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    mock_engine_cls.return_value.enrich.return_value = _fuzzy_result("CVRI2401570", "CVRI2401073")
    transcription = (
        "Plaintiff's motion to compel in CVRI2401570 is GRANTED. Defendant shall serve "
        "verified further responses within 20 days. " * 3
    )
    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = [
        ExtractedRuling(
            extracted_case_number="CVRI2401570",
            extracted_case_title="Smith v. Jones",
            ruling_text=transcription,
            outcome=ExtractionOutcome.GRANTED,
        ),
        ExtractedRuling(
            extracted_case_number="CVRI2400044",
            extracted_case_title="Doe v. Roe",
            ruling_text="The demurrer is OVERRULED. Defendant to answer in 10 days.",
            outcome=ExtractionOutcome.DENIED,
        ),
    ]
    worker._get_framework_extractor = lambda: mock_extractor  # type: ignore[method-assign]
    batches: list = []
    scanned = _make_event(
        document_id="aaaaaaaa-0000-0000-0000-000000004927",
        county="Orange",
        scraper_id="ca-oc-tentatives",
        content_format="pdf",
        case_number=None,
        judge_name=None,
        ruling_text="Page 1",
    )
    with patch.object(
        IngestionWorker,
        "_write_split_batch",
        autospec=True,
        side_effect=lambda _self, _event, _parent, batch: batches.append(batch),
    ):
        worker.process_event(scanned)
    child = next(w for w in batches[0].children if w.case_number == "CVRI2401570")

    with (
        patch("ingestion.worker.upsert_court", return_value="court-uuid-1"),
        patch(
            "ingestion.worker.upsert_case_returning_title", return_value=("case-uuid-1", None)
        ) as upsert_case,
    ):
        worker._prepare_ruling_write(mock_conn, child)

    assert child.case_number == "CVRI2401073"
    assert upsert_case.call_args[0][1] == "CVRI2401073"
    assert child.case_number_evidence == ()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_alias_match_updates_judge_name(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When enrichment resolves a judge alias, judge_name is updated."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    alias_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id="judge-uuid-1",
            canonical_name="Smith, Jonathan A.",
            match_type="alias",
            confidence=1.0,
        ),
        party_resolutions=[],
        flags=[],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = alias_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(judge_name="J. Smith")
    with caplog.at_level(logging.INFO):
        worker.process_event(event)

    # Verify resolve_judge was called with canonical name and source label
    mock_resolve_judge.assert_called_once_with(
        mock_conn,
        "Smith, Jonathan A.",
        "court-uuid-1",
        source="ca-la-tentatives-civil",
        renamed_judge_ids=set(),
        document_id="aaaaaaaa-0000-0000-0000-000000000001",
        # The vote is for the spelling the document printed (#4928).
        printed_name="J. Smith",
        arbitrate=True,
    )

    # The canonical name reaches OpenSearch through the judge row that
    # resolve_judge (asserted above) links — the search doc is built from
    # the committed derived.* row (#4785).

    # Verify log message
    assert any("Enrichment resolved judge via alias" in r.getMessage() for r in caplog.records)


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_directory_mismatch_logs_warning_no_override(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Directory mismatch logs a warning but does NOT override judge name."""
    worker, os_mock = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    mismatch_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id="judge-uuid-1",
            canonical_name="Smith, John A.",
            match_type="alias",
            confidence=0.7,
            directory_mismatch=True,
            directory_judge="Jones, Mary B.",
        ),
        party_resolutions=[],
        flags=["judge_directory_mismatch: extracted=Smith, John A., directory=Jones, Mary B."],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = mismatch_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(judge_name="Smith, John A.")
    with caplog.at_level(logging.WARNING):
        worker.process_event(event)

    # Judge name should NOT be changed to directory judge
    mock_resolve_judge.assert_called_once_with(
        mock_conn,
        "Smith, John A.",
        "court-uuid-1",
        source="ca-la-tentatives-civil",
        renamed_judge_ids=set(),
        document_id="aaaaaaaa-0000-0000-0000-000000000001",
        printed_name=None,
        arbitrate=True,
    )

    # Verify directory mismatch warning was logged
    assert any(
        "judge/directory mismatch" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_flags_logged(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enrichment flags are logged for human review."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    flagged_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        flags=["case_number_corrected: 23STCV12345 -> 23STCV12346 (dist=1, overlap=0.50)"],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = flagged_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event()
    with caplog.at_level(logging.INFO):
        worker.process_event(event)

    assert any("Enrichment flags for human review" in r.getMessage() for r in caplog.records)


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_skipped_when_no_case_number(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment is skipped when case_number is empty."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(case_number=None)
    worker.process_event(event)

    # EnrichmentEngine should NOT be instantiated
    mock_engine_cls.assert_not_called()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_runs_when_no_hearing_date(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment runs even when hearing_date is None, as long as case_number is present (#2215).

    Prior to #2215, enrichment was skipped when hearing_date was None.  Now
    enrichment proceeds using case_number as the primary lookup key.
    """
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    # Configure mock enrichment engine
    mock_engine = MagicMock()
    mock_engine_cls.return_value = mock_engine
    from framework.enrichment import CaseMatch, EnrichmentResult

    mock_engine.enrich.return_value = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-1",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
    )

    event = _make_event(hearing_date=None)
    worker.process_event(event)

    # EnrichmentEngine SHOULD be instantiated — hearing_date=None no longer
    # skips enrichment when case_number is present (#2215)
    mock_engine_cls.assert_called_once()
    mock_engine.enrich.assert_called_once()
    enrich_kwargs = mock_engine.enrich.call_args.kwargs
    assert enrich_kwargs["hearing_date"] is None
    assert enrich_kwargs["case_number"] == "23STCV12345"


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_skipped_for_synthetic_case_number(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment is skipped when case_number starts with UNKNOWN-."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    event = _make_event(case_number="UNKNOWN-doc123")
    worker.process_event(event)

    # EnrichmentEngine should NOT be instantiated
    mock_engine_cls.assert_not_called()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_works_with_llm_extracted_events(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Enrichment runs for _llm_extracted=True events that have case_number and hearing_date."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    mock_engine = MagicMock()
    mock_engine.enrich.return_value = _no_match_enrichment_result()
    mock_engine_cls.return_value = mock_engine

    event = _make_event(_llm_extracted=True, _split_processed=True)
    worker.process_event(event)

    # EnrichmentEngine SHOULD be called even for LLM-extracted events
    mock_engine_cls.assert_called_once_with(mock_conn)
    mock_engine.enrich.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_passes_party_names(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Party names from parties_data are passed to enrich()."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
        ("party-uuid-1",),  # batch_upsert_parties: Smith
        ("party-uuid-2",),  # batch_upsert_parties: Jones
    ]
    mock_cur.fetchall.return_value = []
    mock_cur.nextset.side_effect = [True, False]
    mock_cur.rowcount = 1

    mock_engine = MagicMock()
    mock_engine.enrich.return_value = _no_match_enrichment_result()
    mock_engine_cls.return_value = mock_engine

    parties = [
        {"name": "John Smith", "role": "plaintiff"},
        {"name": "Jane Jones", "role": "defendant"},
    ]
    event = _make_event(parties=parties)
    worker.process_event(event)

    call_kwargs = mock_engine.enrich.call_args.kwargs
    assert call_kwargs["parties"] == ["John Smith", "Jane Jones"]


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_no_db_api_changes(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """DB write functions are called with unchanged signatures (no API changes)."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    mock_engine = MagicMock()
    mock_engine.enrich.return_value = _no_match_enrichment_result()
    mock_engine_cls.return_value = mock_engine

    event = _make_event()
    worker.process_event(event)

    # Verify resolve_judge is called with the source label from scraper_id
    mock_resolve_judge.assert_called_once_with(
        mock_conn,
        "Smith, John A.",
        "court-uuid-1",
        source="ca-la-tentatives-civil",
        renamed_judge_ids=set(),
        document_id="aaaaaaaa-0000-0000-0000-000000000001",
        printed_name=None,
        arbitrate=True,
    )

    # Verify commit was called (transaction completed)
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_judge_alias_same_name_no_log(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When alias resolves to the same name, no 'resolved judge' log is emitted."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    same_name_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id="judge-uuid-1",
            canonical_name="Smith, John A.",
            match_type="alias",
            confidence=1.0,
        ),
        party_resolutions=[],
        flags=[],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = same_name_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(judge_name="Smith, John A.")
    with caplog.at_level(logging.INFO):
        worker.process_event(event)

    # Should NOT log "resolved judge via alias" since name is the same
    assert not any("Enrichment resolved judge via alias" in r.getMessage() for r in caplog.records)


@patch("ingestion.worker.resolve_judge", return_value=None)
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_invalid_judge_name_handled_gracefully(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
) -> None:
    """Invalid judge name from enrichment does not crash — falls through to resolve_judge."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    invalid_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="",
            case_number="23STCV12345",
            match_type="new",
            confidence=0.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id=None,
            canonical_name=None,
            match_type="invalid",
            confidence=0.0,
        ),
        party_resolutions=[],
        flags=[],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = invalid_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(judge_name="XYZ Invalid")
    worker.process_event(event)

    # Judge name should remain unchanged — enrichment did not resolve it
    mock_resolve_judge.assert_called_once_with(
        mock_conn,
        "XYZ Invalid",
        "court-uuid-1",
        source="ca-la-tentatives-civil",
        renamed_judge_ids=set(),
        document_id="aaaaaaaa-0000-0000-0000-000000000001",
        printed_name=None,
        arbitrate=True,
    )
    mock_conn.commit.assert_called_once()


@patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
@patch("ingestion.worker.EnrichmentEngine")
@patch("ingestion.worker.psycopg")
def test_enrichment_directory_mismatch_logs_original_judge_name(
    mock_psycopg: MagicMock,
    mock_engine_cls: MagicMock,
    mock_resolve_judge: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When alias resolution + directory mismatch both occur, log shows original name."""
    worker, _ = _make_worker()
    mock_conn, mock_cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn

    mock_cur.fetchone.side_effect = [
        ("court-uuid-1",),  # upsert_court
        ("case-uuid-1",),  # upsert_case
        (True,),  # insert_document
    ]
    mock_cur.rowcount = 1

    # Alias resolves "J. Smith" -> "Smith, Jonathan A." but directory says "Jones, Mary B."
    combined_result = EnrichmentResult(
        case_match=CaseMatch(
            case_id="case-uuid-existing",
            case_number="23STCV12345",
            match_type="exact",
            confidence=1.0,
        ),
        judge_resolution=JudgeResolution(
            judge_id="judge-uuid-1",
            canonical_name="Smith, Jonathan A.",
            match_type="alias",
            confidence=0.7,
            directory_mismatch=True,
            directory_judge="Jones, Mary B.",
        ),
        party_resolutions=[],
        flags=["judge_directory_mismatch: extracted=J. Smith, directory=Jones, Mary B."],
    )
    mock_engine = MagicMock()
    mock_engine.enrich.return_value = combined_result
    mock_engine_cls.return_value = mock_engine

    event = _make_event(judge_name="J. Smith")
    with caplog.at_level(logging.WARNING):
        worker.process_event(event)

    # The directory mismatch warning should log the ORIGINAL extracted name,
    # not the post-alias canonical name.
    mismatch_warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "judge/directory mismatch" in r.getMessage()
    ]
    assert len(mismatch_warnings) == 1
    # Verify it logged "J. Smith" (original), not "Smith, Jonathan A." (canonical)
    assert mismatch_warnings[0].extracted_judge == "J. Smith"
