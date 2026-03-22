"""Tests for the LLM extraction path in the ingestion worker (#1473, #1475).

Verifies that:
1. The LLM extraction path correctly splits multi-ruling documents.
2. When LLM extraction fails, single-document processing continues with
   per-field LLM + regex fallback.
3. The _llm_extracted flag skips redundant per-field extraction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from framework.llm_schema import (
    ExtractedParty,
    ExtractedRuling,
    ExtractionOutcome,
)
from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(**overrides: object) -> dict:
    """Return a minimal valid DocumentCapturedEvent payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "scraper_id": "ca-oc-tentatives",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "source_url": "https://www.occourts.org/tentativerulings/1",
        "content_format": "pdf",
        "content_hash": "abc123",
        "s3_key": "ca/orange/superior_court/raw/2026/03/05/aaaaaaaa.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "Case No. 2024-01234567\nSmith v. Jones\nThe motion is GRANTED.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair for the persistent connection pattern."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def _make_worker() -> tuple[IngestionWorker, MagicMock]:
    """Return a worker with mocked external dependencies."""
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    worker = IngestionWorker(
        redis_client=redis_mock,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=s3_mock,
        archive_bucket="test-bucket",
    )
    return worker, os_mock


# ---------------------------------------------------------------------------
# Tests: LLM extraction path
# ---------------------------------------------------------------------------


class TestLlmExtractionPath:
    """Tests for _llm_split_document and the LLM extraction flow."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_path_called_for_all_counties(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """All counties use the LLM extraction path (no per-county routing)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Mock the framework extractor to return a single ruling.
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                extracted_parties=[
                    ExtractedParty(name="Smith", role="plaintiff"),
                    ExtractedParty(name="Jones", role="defendant"),
                ],
                extracted_judge_name="Hon. Jane Doe",
                department="C12",
                motion_type="Motion for Summary Judgment",
                outcome=ExtractionOutcome.GRANTED,
                ruling_text="The motion is GRANTED.",
                hearing_date="2026-03-05",
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event()
        worker.process_event(event)

        # The framework extractor should have been called.
        mock_extractor.extract.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multi_ruling_split(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LLM extractor splits a multi-ruling document into individual events."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # Each ruling needs: upsert_court, upsert_case, insert_document
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
            ("court-uuid-1",),
            ("case-uuid-2",),
            (True,),
        ]

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
            ExtractedRuling(
                extracted_case_number="2024-00002",
                extracted_case_title="Gamma v. Delta",
                ruling_text="Motion DENIED.",
                outcome=ExtractionOutcome.DENIED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event(ruling_text="Calendar with multiple rulings...")
        worker.process_event(event)

        # The extractor should have been called once.
        mock_extractor.extract.assert_called_once()
        # Two rulings should result in two commit calls (one per ruling).
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value=None)
    @patch("ingestion.worker.psycopg")
    def test_continues_on_llm_failure(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When LLM extraction fails, single-document processing continues."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # LLM extractor that raises an exception.
        mock_extractor = MagicMock()
        mock_extractor.extract.side_effect = Exception("API error")
        worker._framework_extractor = mock_extractor

        event = _make_event(
            case_number="2024-01234567",
            case_title="Smith v. Jones",
        )
        worker.process_event(event)

        # Should still process the document (single-document path).
        mock_conn.commit.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value=None)
    @patch("ingestion.worker.psycopg")
    def test_continues_on_empty_llm_results(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When LLM extraction returns no rulings, single-document processing continues."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = []
        worker._framework_extractor = mock_extractor

        event = _make_event(
            case_number="2024-01234567",
            case_title="Smith v. Jones",
        )
        worker.process_event(event)

        # Should still process the document.
        mock_conn.commit.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_single_ruling_keeps_original_document_id(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Single-ruling LLM extraction keeps the original document_id."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        original_doc_id = "aaaaaaaa-0000-0000-0000-000000000001"
        event = _make_event(document_id=original_doc_id)
        worker.process_event(event)

        # The single ruling should use the original document_id.
        mock_extractor.extract.assert_called_once()
        assert mock_conn.commit.call_count == 1

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_parties_passed_to_db(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Parties extracted by LLM are passed to batch_upsert_parties."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                extracted_parties=[
                    ExtractedParty(name="Smith", role="plaintiff"),
                    ExtractedParty(name="Jones", role="defendant"),
                ],
                ruling_text="The motion is GRANTED.",
                hearing_date="2026-03-05",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event()
        worker.process_event(event)

        # batch_upsert_parties should be called with the LLM-extracted parties.
        mock_batch_upsert.assert_called_once()
        call_args = mock_batch_upsert.call_args
        parties = call_args[0][2]  # Third positional arg is parties_data
        assert len(parties) == 2
        assert parties[0]["name"] == "Smith"
        assert parties[0]["role"] == "plaintiff"
        assert parties[1]["name"] == "Jones"
        assert parties[1]["role"] == "defendant"


class TestLlmExtractedFlag:
    """Tests for the _llm_extracted flag that skips redundant extraction."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_outcome")
    @patch("ingestion.worker.extract_motion_type")
    @patch("ingestion.worker.extract_case_number")
    @patch("ingestion.worker.extract_case_title")
    @patch("ingestion.worker.extract_judge_name")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_regex_fallback(
        self,
        mock_psycopg: MagicMock,
        mock_extract_judge: MagicMock,
        mock_extract_title: MagicMock,
        mock_extract_number: MagicMock,
        mock_extract_motion: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Events with _llm_extracted=True skip all regex fallback extraction."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate an event that was produced by the LLM extraction path.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            outcome="granted",
            motion_type="Motion for Summary Judgment",
        )

        worker.process_event(event)

        # None of the regex extractors should have been called.
        mock_extract_judge.assert_not_called()
        mock_extract_title.assert_not_called()
        mock_extract_number.assert_not_called()
        mock_extract_motion.assert_not_called()
        mock_extract_outcome.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_fields_llm")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_per_field_llm(
        self,
        mock_psycopg: MagicMock,
        mock_extract_fields_llm: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Events with _llm_extracted=True skip per-field LLM extraction."""
        worker, _ = _make_worker()
        worker._llm_client = MagicMock()  # Enable per-field LLM

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            outcome="granted",
        )

        worker.process_event(event)

        # Per-field LLM extraction should NOT have been called.
        mock_extract_fields_llm.assert_not_called()


class TestFrameworkExtractorInit:
    """Tests for lazy initialization of the framework LlmExtractor."""

    def test_lazy_init_success(self) -> None:
        """_get_framework_extractor creates extractor on first call."""
        worker, _ = _make_worker()
        assert worker._framework_extractor is None

        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_instance = MagicMock()
            mock_extractor_cls.return_value = mock_instance

            result = worker._get_framework_extractor()
            assert result is mock_instance
            mock_extractor_cls.assert_called_once()

    def test_lazy_init_caches(self) -> None:
        """_get_framework_extractor returns cached instance on subsequent calls."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_instance = MagicMock()
            mock_extractor_cls.return_value = mock_instance

            result1 = worker._get_framework_extractor()
            result2 = worker._get_framework_extractor()
            assert result1 is result2
            # Only called once -- second call returns cached.
            mock_extractor_cls.assert_called_once()

    def test_lazy_init_failure_returns_none(self) -> None:
        """_get_framework_extractor returns None if init fails."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor", side_effect=Exception("No API key")):
            result = worker._get_framework_extractor()
            assert result is None
