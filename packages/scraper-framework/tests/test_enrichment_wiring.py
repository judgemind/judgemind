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

    # Verify OpenSearch got the corrected case_number
    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    assert indexed_doc["case_number"] == "23STCV12346"

    # Verify log message
    assert any("Enrichment corrected case_number" in r.getMessage() for r in caplog.records)


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
        mock_conn, "Smith, Jonathan A.", "court-uuid-1", source="ca-la-tentatives-civil"
    )

    # Verify OpenSearch got the canonical judge name
    os_mock.index.assert_called_once()
    indexed_doc = os_mock.index.call_args.kwargs["body"]
    assert indexed_doc["judge_name"] == "Smith, Jonathan A."

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
        mock_conn, "Smith, John A.", "court-uuid-1", source="ca-la-tentatives-civil"
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
        mock_conn, "Smith, John A.", "court-uuid-1", source="ca-la-tentatives-civil"
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
        mock_conn, "XYZ Invalid", "court-uuid-1", source="ca-la-tentatives-civil"
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
