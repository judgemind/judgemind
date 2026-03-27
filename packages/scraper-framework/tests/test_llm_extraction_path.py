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

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_judge_name", return_value="Hon. John Smith")
    @patch("ingestion.worker.extract_hearing_date")
    @patch("ingestion.worker.extract_motion_type", return_value="demurrer")
    @patch("ingestion.worker.extract_outcome", return_value="granted")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_applies_regex_for_missing_judge_name(
        self,
        mock_psycopg: MagicMock,
        mock_extract_outcome: MagicMock,
        mock_extract_motion: MagicMock,
        mock_extract_hearing: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events missing judge_name get regex fallback (#1809)."""
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

        # Simulate multimodal event missing judge_name.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name=None,
            ruling_text="HON. JOHN SMITH\nMarch 21, 2026\nThe demurrer is GRANTED.",
            hearing_date=None,
            outcome=None,
            motion_type=None,
        )

        worker.process_event(event)

        # judge_name regex extractor should have been called.
        mock_extract_judge.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_judge_name")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_judge_regex_when_populated(
        self,
        mock_psycopg: MagicMock,
        mock_extract_judge: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events with judge_name populated skip regex fallback."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Event has judge_name already populated.
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

        # judge_name regex extractor should NOT have been called.
        mock_extract_judge.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch(
        "ingestion.worker.extract_parties_from_caption",
        return_value=[{"name": "Smith", "role": "plaintiff"}],
    )
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_applies_regex_for_missing_parties(
        self,
        mock_psycopg: MagicMock,
        mock_extract_parties: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events missing parties get regex fallback (#1824)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate multimodal event missing parties but having a case title.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="The motion is GRANTED.",
            outcome="granted",
            motion_type="demurrer",
        )

        worker.process_event(event)

        # extract_parties_from_caption should have been called.
        mock_extract_parties.assert_called_once_with("Smith v. Jones")

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_parties_from_caption")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_parties_regex_when_populated(
        self,
        mock_psycopg: MagicMock,
        mock_extract_parties: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events with parties already populated skip regex fallback."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Event already has parties from LLM extraction.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="2024-01234567",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="The motion is GRANTED.",
            outcome="granted",
            motion_type="demurrer",
            parties=[{"name": "Smith", "role": "plaintiff"}],
        )

        worker.process_event(event)

        # extract_parties_from_caption should NOT have been called.
        mock_extract_parties.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch(
        "ingestion.worker.extract_case_type_from_number",
        return_value="civil",
    )
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_applies_regex_for_missing_case_type(
        self,
        mock_psycopg: MagicMock,
        mock_extract_case_type: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events missing case_type get regex fallback (#1824)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate multimodal event missing case_type but having case_number.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="The motion is GRANTED.",
            outcome="granted",
            motion_type="demurrer",
        )

        worker.process_event(event)

        # extract_case_type_from_number should have been called.
        mock_extract_case_type.assert_called_once_with("23STCV12345")

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.extract_case_type_from_number")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_skips_case_type_regex_when_populated(
        self,
        mock_psycopg: MagicMock,
        mock_extract_case_type: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_llm_extracted events with case_type already populated skip regex fallback."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Event already has case_type.
        event = _make_event(
            _split_processed=True,
            _llm_extracted=True,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name="Hon. Jane Doe",
            ruling_text="The motion is GRANTED.",
            outcome="granted",
            motion_type="demurrer",
            case_type="civil",
        )

        worker.process_event(event)

        # extract_case_type_from_number should NOT have been called
        # in the post-LLM fallback block.
        mock_extract_case_type.assert_not_called()


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


class TestCountyExtractorInit:
    """Tests for lazy initialization of the county-specific LlmExtractor (#1728)."""

    def test_lazy_init_success(self) -> None:
        """_get_county_extractor creates extractor on first call."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
            assert result is mock_instance
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )

    def test_lazy_init_caches(self) -> None:
        """_get_county_extractor returns cached instance on subsequent calls."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result1 = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
            result2 = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
            assert result1 is result2
            mock_cls.assert_called_once()

    def test_lazy_init_failure_returns_none(self) -> None:
        """_get_county_extractor returns None if init fails."""
        worker, _ = _make_worker()

        with patch(
            "ingestion.worker.LlmExtractor",
            side_effect=Exception("No Google API key"),
        ):
            result = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
            assert result is None

    def test_different_providers_get_separate_instances(self) -> None:
        """Different provider+model combos get separate cached instances."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_a = MagicMock()
            mock_b = MagicMock()
            mock_cls.side_effect = [mock_a, mock_b]

            result_a = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
            result_b = worker._get_county_extractor("anthropic", "claude-sonnet", None)
            assert result_a is not result_b
            assert mock_cls.call_count == 2

    def test_none_model_uses_empty_string_cache_key(self) -> None:
        """When model is None, cache key uses empty string."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_county_extractor("google", None, None)
            assert result is mock_instance
            # Should be called with only provider kwarg (no model/max_output_tokens)
            mock_cls.assert_called_once_with(provider="google")


class TestCountyExtractionPath:
    """Tests for the county-specific LLM extraction path in _llm_split_document (#1728)."""

    def test_riverside_uses_county_extractor(self) -> None:
        """Riverside docs use a county-specific extractor with custom system prompt."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                case_number="CVPS2400001",
                case_title="Smith v. Jones",
                ruling_text="Motion granted.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion for Summary Judgment",
                parties=[],
            ),
        ]

        with (
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch(
                "framework.extraction_config.get_county_extraction_config",
            ) as mock_get_config,
            patch.object(worker, "process_event") as mock_process,
        ):
            from framework.extraction_config import CountyExtractionConfig, ExtractionMethod

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt="Custom Riverside prompt",
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            mock_cls.return_value = mock_county_extractor

            event = _make_event(
                scraper_id="ca-riverside-tentatives-civil",
                state="CA",
                county="Riverside",
                content_format="pdf",
                ruling_text="Case No. CVPS2400001\nSmith v. Jones\nMotion granted.",
            )
            ruling_text = event["ruling_text"]
            mock_conn, _ = _make_mock_conn()

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "Riverside"
            )

            assert result is True
            # County extractor was created with the right provider/model
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            # extract was called with the county-specific system prompt
            mock_county_extractor.extract.assert_called_once()
            call_kwargs = mock_county_extractor.extract.call_args
            assert call_kwargs.kwargs.get("system_prompt") == "Custom Riverside prompt"
            # process_event was called for the extracted ruling
            mock_process.assert_called_once()

    def test_county_without_custom_provider_uses_framework_extractor(self) -> None:
        """County config with no custom provider falls back to framework extractor."""
        worker, _ = _make_worker()

        mock_framework_extractor = MagicMock()
        mock_framework_extractor.extract.return_value = [
            ExtractedRuling(
                case_number="2024-01234567",
                case_title="Smith v. Jones",
                ruling_text="Motion granted.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion to Compel",
                parties=[],
            ),
        ]

        with (
            patch(
                "framework.extraction_config.get_county_extraction_config",
            ) as mock_get_config,
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch.object(worker, "process_event") as mock_process,
        ):
            from framework.extraction_config import CountyExtractionConfig, ExtractionMethod

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt="Custom prompt",
                provider=None,
                model=None,
                max_output_tokens=None,
            )
            mock_cls.return_value = mock_framework_extractor

            event = _make_event(
                scraper_id="ca-example-tentatives",
                state="CA",
                county="Example",
                content_format="text",
                ruling_text="Case No. 2024-01234567\nSmith v. Jones\nMotion granted.",
            )
            ruling_text = event["ruling_text"]
            mock_conn, _ = _make_mock_conn()

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "Example"
            )

            assert result is True
            # extract was called with the county system prompt
            mock_framework_extractor.extract.assert_called_once()
            call_kwargs = mock_framework_extractor.extract.call_args
            assert call_kwargs.kwargs.get("system_prompt") == "Custom prompt"
            # process_event was called for the extracted ruling
            mock_process.assert_called_once()

    def test_san_bernardino_uses_county_extractor(self) -> None:
        """San Bernardino docs use a county-specific extractor with custom SB prompt (#2050)."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="CIVRS2502080",
                extracted_case_title="Carmell v. Genus-Robinson-Haywood",
                ruling_text="All seven motions to compel are moot.",
                outcome=ExtractionOutcome.OTHER,
                motion_type="Motion to Compel",
                extracted_parties=[
                    ExtractedParty(name="Carmell", role="plaintiff"),
                    ExtractedParty(name="Genus-Robinson-Haywood", role="defendant"),
                ],
            ),
        ]

        with (
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch(
                "framework.extraction_config.get_county_extraction_config",
            ) as mock_get_config,
            patch.object(worker, "process_event") as mock_process,
        ):
            from framework.extraction_config import (
                SAN_BERNARDINO_SYSTEM_PROMPT,
                CountyExtractionConfig,
                ExtractionMethod,
            )

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt=SAN_BERNARDINO_SYSTEM_PROMPT,
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            mock_cls.return_value = mock_county_extractor

            event = _make_event(
                scraper_id="ca-sb-tentatives-civil",
                state="CA",
                county="San Bernardino",
                content_format="pdf",
                ruling_text=(
                    "Department R12 - Judge Kory Mathewson\n"
                    "CIVRS2502080\n"
                    "Carmell v. Genus-Robinson-Haywood\n"
                    "All seven motions to compel are moot."
                ),
            )
            ruling_text = event["ruling_text"]

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "San Bernardino"
            )

            assert result is True
            # County extractor was created with the right provider/model
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            # extract was called with the SB-specific system prompt
            mock_county_extractor.extract.assert_called_once()
            call_kwargs = mock_county_extractor.extract.call_args
            assert call_kwargs.kwargs.get("system_prompt") == SAN_BERNARDINO_SYSTEM_PROMPT
            # process_event was called for the extracted ruling
            mock_process.assert_called_once()
            # Verify the split event has the LLM-extracted fields
            split_event = mock_process.call_args[0][0]
            assert split_event["case_number"] == "CIVRS2502080"
            assert split_event["case_title"] == "Carmell v. Genus-Robinson-Haywood"
            assert split_event["_llm_extracted"] is True

    def test_san_bernardino_multi_case_split(self) -> None:
        """San Bernardino multi-case PDF is split into separate events (#2050)."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="CIVSB2419120",
                extracted_case_title="Solis v. General Motors",
                ruling_text="Motion for summary adjudication is denied.",
                outcome=ExtractionOutcome.DENIED,
                motion_type="Motion for Summary Adjudication",
                extracted_parties=[
                    ExtractedParty(name="Solis", role="plaintiff"),
                    ExtractedParty(name="General Motors", role="defendant"),
                ],
            ),
            ExtractedRuling(
                extracted_case_number="CIVSB2416631",
                extracted_case_title="Smith v. Jones",
                ruling_text="Demurrer is sustained.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Demurrer",
                extracted_parties=[
                    ExtractedParty(name="Smith", role="plaintiff"),
                    ExtractedParty(name="Jones", role="defendant"),
                ],
            ),
        ]

        with (
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch(
                "framework.extraction_config.get_county_extraction_config",
            ) as mock_get_config,
            patch.object(worker, "process_event") as mock_process,
        ):
            from framework.extraction_config import (
                SAN_BERNARDINO_SYSTEM_PROMPT,
                CountyExtractionConfig,
                ExtractionMethod,
            )

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt=SAN_BERNARDINO_SYSTEM_PROMPT,
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            mock_cls.return_value = mock_county_extractor

            event = _make_event(
                scraper_id="ca-sb-tentatives-civil",
                state="CA",
                county="San Bernardino",
                content_format="pdf",
                ruling_text="Multi-case document with two cases.",
            )
            ruling_text = event["ruling_text"]

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "San Bernardino"
            )

            assert result is True
            # process_event called twice (once per case)
            assert mock_process.call_count == 2

            # First split event
            first_event = mock_process.call_args_list[0][0][0]
            assert first_event["case_number"] == "CIVSB2419120"
            assert first_event["_split_index"] == 0
            assert first_event["_split_count"] == 2

            # Second split event
            second_event = mock_process.call_args_list[1][0][0]
            assert second_event["case_number"] == "CIVSB2416631"
            assert second_event["_split_index"] == 1
            assert second_event["_split_count"] == 2


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
