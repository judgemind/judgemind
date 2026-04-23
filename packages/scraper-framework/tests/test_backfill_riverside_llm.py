"""Tests for the Riverside LLM backfill script.

Tests cover:
  - normalize_outcome: verifies shared import from ingestion.extract
  - match_llm_to_db_rulings: matching LLM extractions to existing DB records
  - compute_ruling_text_hash: consistent hashing for dedup
  - run_backfill: end-to-end with mocked DB, S3, and LLM
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# Add scripts/ to sys.path so we can import the backfill module.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, os.path.normpath(_SCRIPTS_DIR))

import backfill_riverside_llm  # noqa: E402
from backfill_riverside_llm import (  # noqa: E402
    compute_ruling_text_hash,
    match_llm_to_db_rulings,
    normalize_outcome,
)

# ---------------------------------------------------------------------------
# We need ExtractedRuling from the framework for building test fixtures.
# ---------------------------------------------------------------------------
from framework.llm_schema import (  # noqa: E402
    ExtractedRuling,
    ExtractionOutcome,
)

# ---------------------------------------------------------------------------
# normalize_outcome tests
# ---------------------------------------------------------------------------


class TestNormalizeOutcome:
    """Tests that backfill uses the shared normalize_outcome from ingestion.extract.

    Comprehensive unit tests for normalize_outcome() live in test_extract.py.
    These tests verify the backfill module correctly imports and uses the shared
    function (not a local copy).
    """

    def test_backfill_uses_shared_normalize_outcome(self) -> None:
        """backfill_riverside_llm.normalize_outcome should be the same function as
        ingestion.extract.normalize_outcome."""
        from ingestion.extract import normalize_outcome as shared_fn

        assert backfill_riverside_llm.normalize_outcome is shared_fn

    def test_basic_normalization_via_backfill(self) -> None:
        """Spot-check that the shared function works when called via the backfill module."""
        assert normalize_outcome("Granted") == "granted"
        assert normalize_outcome("No Tentative Ruling") == "other"
        assert normalize_outcome(None) is None


# ---------------------------------------------------------------------------
# compute_ruling_text_hash tests
# ---------------------------------------------------------------------------


class TestComputeRulingTextHash:
    """Test ruling text hash computation."""

    def test_consistent_hash(self) -> None:
        text = "The motion is GRANTED."
        h1 = compute_ruling_text_hash(text)
        h2 = compute_ruling_text_hash(text)
        assert h1 == h2

    def test_whitespace_normalization(self) -> None:
        t1 = "The  motion   is GRANTED."
        t2 = "The motion is GRANTED."
        assert compute_ruling_text_hash(t1) == compute_ruling_text_hash(t2)

    def test_case_normalization(self) -> None:
        t1 = "The Motion is GRANTED."
        t2 = "the motion is granted."
        assert compute_ruling_text_hash(t1) == compute_ruling_text_hash(t2)

    def test_different_text_different_hash(self) -> None:
        h1 = compute_ruling_text_hash("Motion granted.")
        h2 = compute_ruling_text_hash("Motion denied.")
        assert h1 != h2


# ---------------------------------------------------------------------------
# match_llm_to_db_rulings tests
# ---------------------------------------------------------------------------


def _make_db_ruling(
    ruling_id: str = "r1",
    case_number: str = "CVPS2306157",
    case_title: str = "Yeldell v. Henss",
    **kwargs: object,
) -> dict:
    """Create a mock DB ruling dict."""
    return {
        "ruling_id": ruling_id,
        "document_id": "d1",
        "case_id": "c1",
        "ruling_text": "Some text",
        "outcome": "granted",
        "motion_type": "demurrer",
        "hearing_date": "2026-03-01",
        "case_number": case_number,
        "case_title": case_title,
        "case_type": "civil",
        **kwargs,
    }


def _make_llm_ruling(
    case_number: str = "CVPS2306157",
    case_title: str = "Yeldell v. Henss",
    outcome: ExtractionOutcome = ExtractionOutcome.GRANTED,
    ruling_text: str = "The motion is GRANTED.",
    **kwargs: object,
) -> ExtractedRuling:
    """Create a mock LLM-extracted ruling."""
    return ExtractedRuling(
        extracted_case_number=case_number,
        extracted_case_title=case_title,
        outcome=outcome,
        ruling_text=ruling_text,
        **kwargs,
    )


class TestMatchLlmToDbRulings:
    """Test matching LLM-extracted rulings to DB ruling records."""

    def test_exact_case_number_match(self) -> None:
        db = [_make_db_ruling(ruling_id="r1", case_number="CVPS2306157")]
        llm = [_make_llm_ruling(case_number="CVPS2306157")]
        matches = match_llm_to_db_rulings(llm, db)
        assert len(matches) == 1
        assert matches[0][0]["ruling_id"] == "r1"
        assert matches[0][1].extracted_case_number == "CVPS2306157"

    def test_multiple_rulings_matched_by_case_number(self) -> None:
        db = [
            _make_db_ruling(ruling_id="r1", case_number="CVPS2306157"),
            _make_db_ruling(ruling_id="r2", case_number="CVPS2404518"),
        ]
        llm = [
            _make_llm_ruling(case_number="CVPS2404518"),
            _make_llm_ruling(case_number="CVPS2306157"),
        ]
        matches = match_llm_to_db_rulings(llm, db)
        assert len(matches) == 2
        # r1 should match CVPS2306157, r2 should match CVPS2404518
        match_dict = {m[0]["ruling_id"]: m[1].extracted_case_number for m in matches}
        assert match_dict["r1"] == "CVPS2306157"
        assert match_dict["r2"] == "CVPS2404518"

    def test_positional_fallback(self) -> None:
        """When case numbers don't match, fall back to positional matching."""
        db = [
            _make_db_ruling(ruling_id="r1", case_number="UNKNOWN-1"),
            _make_db_ruling(ruling_id="r2", case_number="UNKNOWN-2"),
        ]
        llm = [
            _make_llm_ruling(case_number="CVPS2306157"),
            _make_llm_ruling(case_number="CVPS2404518"),
        ]
        matches = match_llm_to_db_rulings(llm, db)
        assert len(matches) == 2
        # Positional: r1 -> first LLM, r2 -> second LLM
        assert matches[0][0]["ruling_id"] == "r1"
        assert matches[0][1].extracted_case_number == "CVPS2306157"
        assert matches[1][0]["ruling_id"] == "r2"
        assert matches[1][1].extracted_case_number == "CVPS2404518"

    def test_partial_case_number_match(self) -> None:
        """Case numbers that contain each other should still match."""
        db = [_make_db_ruling(ruling_id="r1", case_number="CVPS2306157")]
        llm = [_make_llm_ruling(case_number="PS2306157")]  # Missing CV prefix
        matches = match_llm_to_db_rulings(llm, db)
        assert len(matches) == 1

    def test_empty_llm_rulings(self) -> None:
        db = [_make_db_ruling()]
        matches = match_llm_to_db_rulings([], db)
        assert len(matches) == 0

    def test_empty_db_rulings(self) -> None:
        llm = [_make_llm_ruling()]
        matches = match_llm_to_db_rulings(llm, [])
        assert len(matches) == 0

    def test_more_db_than_llm(self) -> None:
        """When DB has more rulings than LLM, only matched ones are returned."""
        db = [
            _make_db_ruling(ruling_id="r1", case_number="CVPS2306157"),
            _make_db_ruling(ruling_id="r2", case_number="CVPS2404518"),
            _make_db_ruling(ruling_id="r3", case_number="RIC1904113"),
        ]
        llm = [_make_llm_ruling(case_number="CVPS2306157")]
        matches = match_llm_to_db_rulings(llm, db)
        # r1 matches by case number; r2, r3 unmatched but no LLM left
        assert len(matches) == 1
        assert matches[0][0]["ruling_id"] == "r1"

    def test_more_llm_than_db(self) -> None:
        """Extra LLM rulings with no DB match are ignored."""
        db = [_make_db_ruling(ruling_id="r1", case_number="CVPS2306157")]
        llm = [
            _make_llm_ruling(case_number="CVPS2306157"),
            _make_llm_ruling(case_number="CVPS2404518"),
        ]
        matches = match_llm_to_db_rulings(llm, db)
        assert len(matches) == 1


# ---------------------------------------------------------------------------
# run_backfill integration test (mocked dependencies)
# ---------------------------------------------------------------------------


class TestRunBackfill:
    """Test the main backfill function with mocked DB, S3, and LLM."""

    @patch("backfill_riverside_llm.boto3")
    @patch("backfill_riverside_llm.psycopg")
    @patch("backfill_riverside_llm.LlmExtractor")
    @patch("backfill_riverside_llm.extract_pdf_text", return_value="PDF text content")
    def test_dry_run_no_writes(
        self,
        mock_extract_pdf: MagicMock,
        mock_extractor_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Dry run should not execute UPDATE queries."""

        # Mock cursor using a helper to create proper name attributes
        def _make_desc(col_name: str) -> MagicMock:
            d = MagicMock()
            d.name = col_name
            return d

        doc_columns = [
            "document_id",
            "s3_key",
            "s3_bucket",
            "content_hash",
            "source_url",
            "captured_at",
            "doc_hearing_date",
            "format",
            "doc_case_id",
            "state",
            "county",
            "court_name",
            "court_id",
        ]
        ruling_columns = [
            "ruling_id",
            "document_id",
            "case_id",
            "ruling_text",
            "outcome",
            "motion_type",
            "hearing_date",
            "case_number",
            "case_title",
            "case_type",
        ]

        doc_row = (
            "doc-1",
            "s3/key.pdf",
            "bucket",
            "hash123",
            "https://court.example/ruling.pdf",
            "2026-03-01",
            "2026-03-01",
            "pdf",
            "case-1",
            "CA",
            "Riverside",
            "Superior Court",
            "court-1",
        )
        ruling_row = (
            "r-1",
            "doc-1",
            "case-1",
            "Old ruling text",
            "granted",
            "demurrer",
            "2026-03-01",
            "CVPS2306157",
            "Yeldell v. Henss",
            "civil",
        )

        # Track cursor() calls to return different results
        call_count = 0

        def make_cursor() -> MagicMock:
            nonlocal call_count
            call_count += 1
            cur = MagicMock()
            # Cursor is used as a context manager: `with conn.cursor() as cur:`
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            if call_count == 1:
                # First cursor: fetch documents query
                cur.description = [_make_desc(c) for c in doc_columns]
                cur.fetchall.return_value = [doc_row]
            else:
                # Subsequent cursors: fetch rulings query
                cur.description = [_make_desc(c) for c in ruling_columns]
                cur.fetchall.return_value = [ruling_row]
            return cur

        # Set up mock DB connection
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = make_cursor
        # Make psycopg.connect() work as a context manager returning mock_conn
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        # Mock S3
        mock_s3 = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"PDF content"
        mock_s3.get_object.return_value = {"Body": mock_body}
        mock_boto3.client.return_value = mock_s3

        # Mock LLM extractor
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="CVPS2306157",
                extracted_case_title="Yeldell v. Henss",
                outcome=ExtractionOutcome.GRANTED,
                ruling_text="Better ruling text from LLM.",
                motion_type="demurrer",
            ),
        ]
        mock_extractor_cls.return_value = mock_extractor

        from backfill_riverside_llm import run_backfill

        stats = run_backfill(
            "postgresql://test/db",
            dry_run=True,
            limit=1,
        )

        assert stats["documents_processed"] == 1
        assert stats["rulings_updated"] == 1
        # Verify no UPDATE was executed (dry run)
        mock_conn.commit.assert_not_called()
