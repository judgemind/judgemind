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

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_motion_type", return_value="demurrer")
    @patch("ingestion.worker.extract_outcome", return_value="granted")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_applies_regex_for_missing_motion_type_and_outcome(
        self,
        mock_psycopg: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_extract_motion: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events missing motion_type/outcome get regex fallback (#1770)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate multimodal extraction event: has ruling_text but no
        # motion_type or outcome (transcription-only pipeline).
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="The demurrer is GRANTED with leave to amend.",
            outcome=None,
            motion_type=None,
        )

        worker.process_event(event)

        # Regex extractors SHOULD have been called for the missing fields.
        mock_extract_outcome.assert_called_once()
        mock_extract_motion.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_motion_type")
    @patch("ingestion.worker.extract_outcome")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_regex_when_fields_populated(
        self,
        mock_psycopg: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_extract_motion: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Events with _llm_extracted=True and populated fields skip regex post-LLM fallback."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Event has both motion_type and outcome already populated.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            outcome="granted",
            motion_type="demurrer",
        )

        worker.process_event(event)

        # Regex extractors should NOT have been called — fields are already populated.
        mock_extract_outcome.assert_not_called()
        mock_extract_motion.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_fields_llm")
    @patch("ingestion.worker.extract_motion_type", return_value="msj")
    @patch("ingestion.worker.extract_outcome", return_value="denied")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_regex_post_llm_skips_per_field_llm(
        self,
        mock_psycopg: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_extract_motion: MagicMock,
        mock_extract_fields_llm: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Regex post-LLM runs but per-field LLM skipped for _llm_extracted."""
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
            ruling_text="The motion for summary judgment is DENIED.",
            outcome=None,
            motion_type=None,
        )

        worker.process_event(event)

        # Per-field LLM extraction should NOT have been called.
        mock_extract_fields_llm.assert_not_called()
        # But regex extractors SHOULD have been called.
        mock_extract_outcome.assert_called_once()
        mock_extract_motion.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_hearing_date")
    @patch("ingestion.worker.extract_motion_type", return_value="demurrer")
    @patch("ingestion.worker.extract_outcome", return_value="granted")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_applies_regex_for_missing_hearing_date(
        self,
        mock_psycopg: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_extract_motion: MagicMock,
        mock_extract_hearing: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events missing hearing_date get regex fallback."""
        from datetime import date

        mock_extract_hearing.return_value = date(2026, 3, 21)
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate multimodal event missing hearing_date.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="March 21, 2026\nThe demurrer is GRANTED.",
            hearing_date=None,
            outcome=None,
            motion_type=None,
        )

        worker.process_event(event)

        # hearing_date regex extractor should have been called.
        mock_extract_hearing.assert_called_once()


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


class TestMultimodalExtractorInit:
    """Tests for lazy initialization of the multimodal LlmExtractor."""

    def test_lazy_init_success(self) -> None:
        """_get_multimodal_extractor creates extractor on first call."""
        worker, _ = _make_worker()
        assert worker._multimodal_extractor is None

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_multimodal_extractor()
            assert result is mock_instance
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
            )

    def test_lazy_init_caches(self) -> None:
        """_get_multimodal_extractor returns cached instance on subsequent calls."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result1 = worker._get_multimodal_extractor()
            result2 = worker._get_multimodal_extractor()
            assert result1 is result2
            mock_cls.assert_called_once()

    def test_lazy_init_failure_returns_none(self) -> None:
        """_get_multimodal_extractor returns None if init fails."""
        worker, _ = _make_worker()

        with patch(
            "ingestion.worker.LlmExtractor",
            side_effect=Exception("No Google API key"),
        ):
            result = worker._get_multimodal_extractor()
            assert result is None


class TestMultimodalExtractionPath:
    """Tests for the multimodal extraction path in _llm_split_document."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multimodal_path_used_for_pdf_with_raw_bytes(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When raw_pdf_bytes are available, multimodal extraction is used."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Set up multimodal extractor mock.
        mock_multimodal = MagicMock()
        mock_multimodal.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._multimodal_extractor = mock_multimodal

        # Also set up text extractor (should NOT be called).
        mock_text = MagicMock()
        worker._framework_extractor = mock_text

        event = _make_event(
            content_format="pdf",
            ruling_text="PDF binary content here",
        )
        # Simulate raw PDF bytes by patching is_pdf_binary.
        with patch("ingestion.worker.is_pdf_binary", return_value=True):
            worker.process_event(event)

        # Multimodal extractor should have been called.
        mock_multimodal.extract_from_pdf.assert_called_once()
        # Text extractor should NOT have been called.
        mock_text.extract.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multimodal_failure_falls_back_to_text(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When multimodal extraction fails, text-based extraction is used."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Set up multimodal extractor that fails.
        mock_multimodal = MagicMock()
        mock_multimodal.extract_from_pdf.side_effect = Exception("API error")
        worker._multimodal_extractor = mock_multimodal

        # Set up text extractor (should be called as fallback).
        mock_text = MagicMock()
        mock_text.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_text

        event = _make_event(
            content_format="pdf",
            ruling_text="PDF binary content here",
        )
        with patch("ingestion.worker.is_pdf_binary", return_value=True):
            worker.process_event(event)

        # Text extractor should have been called as fallback.
        mock_text.extract.assert_called_once()
        # Document should still be processed.
        mock_conn.commit.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_empty_multimodal_result_does_not_fallback(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When multimodal returns empty list, text fallback is NOT used."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Multimodal extractor returns empty list (no rulings found).
        mock_multimodal = MagicMock()
        mock_multimodal.extract_from_pdf.return_value = []
        worker._multimodal_extractor = mock_multimodal

        # Text extractor should NOT be called.
        mock_text = MagicMock()
        worker._framework_extractor = mock_text

        event = _make_event(
            content_format="pdf",
            ruling_text="PDF binary content here",
        )
        with patch("ingestion.worker.is_pdf_binary", return_value=True):
            worker.process_event(event)

        # Multimodal was called.
        mock_multimodal.extract_from_pdf.assert_called_once()
        # Text extractor should NOT have been called — empty list
        # from multimodal is authoritative.
        mock_text.extract.assert_not_called()
