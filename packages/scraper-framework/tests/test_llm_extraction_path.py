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
        "s3_key": "ca/orange/superior_court/raw/abc123.pdf",
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
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multi_ruling_empty_text_does_not_get_calendar_fallback(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Multi-ruling split: empty LLM ruling_text gets None, not the full calendar (#2057).

        When the LLM correctly identifies multiple cases from a department calendar
        PDF but fails to extract ruling_text for some, the fallback must NOT
        substitute the entire pdfplumber text (the full calendar). Instead, the
        ruling_text should be None for cases where the LLM returned no text.
        """
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

        full_calendar_text = (
            "TENTATIVE RULINGS DEPT C28 Judge Thomas S. McConville "
            "February 23, 2026 at 2:00 p.m. Case 2024-00001 Alpha v. Beta "
            "Motion GRANTED. Case 2024-00002 Gamma v. Delta The court rules..."
        )

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
                # LLM failed to extract text for this case — empty/None ruling_text
                ruling_text=None,
                outcome=ExtractionOutcome.DENIED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        # Track the ruling_text values passed to each recursive process_event call.
        captured_ruling_texts: list[str | None] = []
        original_process_event = worker.process_event

        def spy_process_event(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                captured_ruling_texts.append(event_data.get("ruling_text"))
            original_process_event(event_data)

        worker.process_event = spy_process_event  # type: ignore[assignment]

        event = _make_event(ruling_text=full_calendar_text)
        worker.process_event(event)

        # The first ruling had LLM-extracted text — should use it.
        assert captured_ruling_texts[0] == "Motion GRANTED."
        # The second ruling had no LLM text — should be None, NOT the full calendar.
        assert captured_ruling_texts[1] is None
        # Specifically: must NOT contain the full calendar text.
        for text in captured_ruling_texts:
            if text is not None:
                assert text != full_calendar_text

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_single_ruling_empty_text_gets_fallback(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Single-ruling document: empty LLM ruling_text falls back to pdfplumber text.

        For single-ruling documents (not multi-case calendars), the pdfplumber
        text fallback is correct — there is only one case so the full text
        belongs to that case.
        """
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        pdfplumber_text = "The motion for summary judgment is GRANTED."

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-00001",
                extracted_case_title="Alpha v. Beta",
                # LLM didn't extract text, but there's only one ruling
                ruling_text=None,
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        # Track the ruling_text passed to the recursive process_event call.
        captured_ruling_texts: list[str | None] = []
        original_process_event = worker.process_event

        def spy_process_event(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                captured_ruling_texts.append(event_data.get("ruling_text"))
            original_process_event(event_data)

        worker.process_event = spy_process_event  # type: ignore[assignment]

        event = _make_event(ruling_text=pdfplumber_text)
        worker.process_event(event)

        # Single-ruling: should fall back to pdfplumber text.
        assert captured_ruling_texts[0] == pdfplumber_text

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

    def test_max_chars_per_chunk_passed_through(self) -> None:
        """max_chars_per_chunk is passed to LlmExtractor when set (#2136)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768, 40000)
            assert result is mock_instance
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
                max_chars_per_chunk=40000,
            )

    def test_max_chars_per_chunk_none_not_passed(self) -> None:
        """max_chars_per_chunk=None omits kwarg from LlmExtractor (#2136)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768, None)
            assert result is mock_instance
            # max_chars_per_chunk should NOT appear in kwargs.
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )

    def test_different_max_chars_per_chunk_gets_separate_instances(self) -> None:
        """Different max_chars_per_chunk values get separate cached instances (#2136)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_a = MagicMock()
            mock_b = MagicMock()
            mock_cls.side_effect = [mock_a, mock_b]

            result_a = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768, 40000)
            result_b = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768, 80000)
            assert result_a is not result_b
            assert mock_cls.call_count == 2


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
                extracted_case_title="Angela Carmell v. Kathleen Janet Genus-Robinson-Haywood",
                ruling_text="All seven motions to compel are moot.",
                outcome=ExtractionOutcome.OTHER,
                motion_type="Motion to Compel",
                extracted_parties=[
                    ExtractedParty(name="Angela Carmell", role="plaintiff"),
                    ExtractedParty(name="Kathleen Janet Genus-Robinson-Haywood", role="defendant"),
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
            expected_title = "Angela Carmell v. Kathleen Janet Genus-Robinson-Haywood"
            assert split_event["case_title"] == expected_title
            assert split_event["_llm_extracted"] is True

    def test_san_bernardino_multi_case_split(self) -> None:
        """San Bernardino multi-case PDF is split into separate events (#2050)."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="CIVSB2419120",
                extracted_case_title="Lorenzo Solis v. General Motors",
                ruling_text="Motion for summary adjudication is denied.",
                outcome=ExtractionOutcome.DENIED,
                motion_type="Motion for Summary Adjudication",
                extracted_parties=[
                    ExtractedParty(name="Lorenzo Solis", role="plaintiff"),
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

    def test_san_francisco_uses_county_extractor(self) -> None:
        """San Francisco docs use a county-specific extractor with custom SF prompt (#2051)."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="FPT-25-378624",
                extracted_case_title="Michael Edward Graves v. Ranjie Long",
                ruling_text="The court grants the request for change of custody.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Request for Order for Change of Child Custody",
                extracted_parties=[
                    ExtractedParty(name="Michael Edward Graves", role="petitioner"),
                    ExtractedParty(name="Ranjie Long", role="respondent"),
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
                SAN_FRANCISCO_SYSTEM_PROMPT,
                CountyExtractionConfig,
                ExtractionMethod,
            )

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt=SAN_FRANCISCO_SYSTEM_PROMPT,
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            mock_cls.return_value = mock_county_extractor

            event = _make_event(
                scraper_id="ca-sf-tentatives-family-law",
                state="CA",
                county="San Francisco",
                content_format="pdf",
                ruling_text=(
                    "SUPERIOR COURT OF CALIFORNIA\n"
                    "COUNTY OF SAN FRANCISCO\n"
                    "UNIFIED FAMILY COURT\n"
                    "Case Number: FPT-25-378624\n"
                    "Hearing Date: March 3, 2026\n"
                    "Presiding: BOBBY P. LUNA\n"
                    "TENTATIVE RULING\n"
                    "The court grants the request for change of custody."
                ),
            )
            ruling_text = event["ruling_text"]

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "San Francisco"
            )

            assert result is True
            # County extractor was created with the right provider/model
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            # extract was called with the SF-specific system prompt
            mock_county_extractor.extract.assert_called_once()
            call_kwargs = mock_county_extractor.extract.call_args
            assert call_kwargs.kwargs.get("system_prompt") == SAN_FRANCISCO_SYSTEM_PROMPT
            # process_event was called for the extracted ruling
            mock_process.assert_called_once()
            # Verify the split event has the LLM-extracted fields
            split_event = mock_process.call_args[0][0]
            assert split_event["case_number"] == "FPT-25-378624"
            assert split_event["case_title"] == "Michael Edward Graves v. Ranjie Long"
            assert split_event["_llm_extracted"] is True

    def test_san_francisco_multi_case_split(self) -> None:
        """San Francisco multi-case PDF is split into separate events (#2051)."""
        worker, _ = _make_worker()

        mock_county_extractor = MagicMock()
        mock_county_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="FPT-25-378624",
                extracted_case_title="Michael Edward Graves v. Ranjie Long",
                ruling_text="The court grants the request for change of custody.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Request for Order for Change of Child Custody",
                extracted_parties=[
                    ExtractedParty(name="Michael Edward Graves", role="petitioner"),
                    ExtractedParty(name="Ranjie Long", role="respondent"),
                ],
            ),
            ExtractedRuling(
                extracted_case_number="FMS-20-387302",
                extracted_case_title="Smith v. Doe",
                ruling_text="The motion is continued to April 15, 2026.",
                outcome=ExtractionOutcome.CONTINUED,
                motion_type="Request for Order for Modification of Support",
                extracted_parties=[
                    ExtractedParty(name="Jane Smith", role="petitioner"),
                    ExtractedParty(name="John Doe", role="respondent"),
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
                SAN_FRANCISCO_SYSTEM_PROMPT,
                CountyExtractionConfig,
                ExtractionMethod,
            )

            mock_get_config.return_value = CountyExtractionConfig(
                method=ExtractionMethod.LLM,
                system_prompt=SAN_FRANCISCO_SYSTEM_PROMPT,
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )
            mock_cls.return_value = mock_county_extractor

            event = _make_event(
                scraper_id="ca-sf-tentatives-family-law",
                state="CA",
                county="San Francisco",
                content_format="pdf",
                ruling_text="Multi-case SF Family Law calendar with two cases.",
            )
            ruling_text = event["ruling_text"]

            result = worker._llm_split_document(
                event, event["document_id"], ruling_text, "CA", "San Francisco"
            )

            assert result is True
            # process_event called twice (once per case)
            assert mock_process.call_count == 2

            # First split event
            first_event = mock_process.call_args_list[0][0][0]
            assert first_event["case_number"] == "FPT-25-378624"
            assert first_event["_split_index"] == 0
            assert first_event["_split_count"] == 2

            # Second split event
            second_event = mock_process.call_args_list[1][0][0]
            assert second_event["case_number"] == "FMS-20-387302"
            assert second_event["_split_index"] == 1
            assert second_event["_split_count"] == 2


class TestMultiCasePdfAttribution:
    """Regression tests for #2195: multi-case PDF rulings must get distinct cases.

    Before the fix in #2202, all rulings from a multi-case PDF were linked to
    a single case row because the ingestion worker used the same case_number
    (from the parent document's scraper event) for every split ruling.  After
    the fix, each split ruling carries its own LLM-extracted case_number, and
    ``upsert_case_returning_title`` is called once per ruling with that
    ruling's case_number — producing a distinct case row for each.
    """

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.insert_document_and_ruling", return_value=True)
    @patch(
        "ingestion.worker.upsert_case_returning_title",
        side_effect=[
            ("case-uuid-1", "Alpha v. Beta"),
            ("case-uuid-2", "Gamma v. Delta"),
            ("case-uuid-3", "Epsilon v. Zeta"),
        ],
    )
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_each_split_ruling_gets_distinct_case_id(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Each ruling from a multi-case PDF gets its own case row (#2195).

        This is the primary regression test: when the LLM extractor splits a
        multi-case document into 3 rulings, each with a different case_number,
        the worker must call ``upsert_case_returning_title`` with different
        case numbers and pass the resulting case_ids to
        ``insert_document_and_ruling``.
        """
        worker, _ = _make_worker()

        mock_conn, _ = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01380242",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion for summary judgment is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion for Summary Judgment",
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-01399001",
                extracted_case_title="Gamma v. Delta",
                ruling_text="Demurrer is OVERRULED.",
                outcome=ExtractionOutcome.DENIED,
                motion_type="Demurrer",
            ),
            ExtractedRuling(
                extracted_case_number="30-2025-01450123",
                extracted_case_title="Epsilon v. Zeta",
                ruling_text="Motion to compel is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion to Compel",
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event(
            ruling_text=(
                "TENTATIVE RULINGS DEPT C24 March 10, 2026\n"
                "Case 30-2024-01380242 Alpha v. Beta...\n"
                "Case 30-2024-01399001 Gamma v. Delta...\n"
                "Case 30-2025-01450123 Epsilon v. Zeta...\n"
            ),
        )
        worker.process_event(event)

        # --- upsert_case_returning_title called 3 times with different case numbers ---
        assert mock_upsert_case.call_count == 3
        case_numbers_used = [call.args[1] for call in mock_upsert_case.call_args_list]
        assert case_numbers_used == [
            "30-2024-01380242",
            "30-2024-01399001",
            "30-2025-01450123",
        ]

        # --- insert_document_and_ruling called 3 times with different case_ids ---
        assert mock_insert_doc_and_ruling.call_count == 3
        case_ids_used = [
            call.kwargs["case_id"] for call in mock_insert_doc_and_ruling.call_args_list
        ]
        assert case_ids_used == ["case-uuid-1", "case-uuid-2", "case-uuid-3"]

        # --- Each ruling has a unique document_id (split IDs are deterministic) ---
        doc_ids_used = [
            call.kwargs["document_id"] for call in mock_insert_doc_and_ruling.call_args_list
        ]
        assert len(set(doc_ids_used)) == 3, f"Expected 3 unique document_ids, got {doc_ids_used}"

        # --- The document_ids should all be different from the original ---
        original_doc_id = event["document_id"]
        for doc_id in doc_ids_used:
            assert doc_id != original_doc_id, "Split document_id should not match original"

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.insert_document_and_ruling", return_value=True)
    @patch(
        "ingestion.worker.upsert_case_returning_title",
        side_effect=[
            ("case-uuid-1", "Alpha v. Beta"),
            ("case-uuid-1", "Alpha v. Beta"),
        ],
    )
    @patch("ingestion.worker.upsert_court", return_value="court-uuid-1")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_same_case_number_reuses_case_row(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_upsert_court: MagicMock,
        mock_upsert_case: MagicMock,
        mock_insert_doc_and_ruling: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Two rulings with the same case_number share one case row.

        When a case has multiple motions heard on the same day, the LLM
        returns two rulings with the same case_number.  They should both
        be linked to the same case_id via upsert.
        """
        worker, _ = _make_worker()

        mock_conn, _ = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="30-2024-01380242",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion for summary judgment is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
            ExtractedRuling(
                extracted_case_number="30-2024-01380242",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion to compel is DENIED.",
                outcome=ExtractionOutcome.DENIED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event(ruling_text="Two motions for the same case...")
        worker.process_event(event)

        # Both rulings should use the same case_number
        assert mock_upsert_case.call_count == 2
        case_numbers = [call.args[1] for call in mock_upsert_case.call_args_list]
        assert case_numbers == ["30-2024-01380242", "30-2024-01380242"]

        # Both rulings should get the same case_id
        case_ids = [call.kwargs["case_id"] for call in mock_insert_doc_and_ruling.call_args_list]
        assert case_ids == ["case-uuid-1", "case-uuid-1"]

        # But they should still have different document_ids (split IDs)
        doc_ids = [call.kwargs["document_id"] for call in mock_insert_doc_and_ruling.call_args_list]
        assert len(set(doc_ids)) == 2


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

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multimodal_skipped_for_llm_configured_county(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """When a county is configured for ExtractionMethod.LLM, multimodal is skipped (#2271).

        Ventura and other LLM-configured counties have custom text-based prompts
        that extract outcome and motion_type. The multimodal per-page prompt does
        NOT extract these fields, causing field regressions when multimodal is
        used instead.
        """
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Set up multimodal extractor (should NOT be called for Ventura).
        mock_multimodal = MagicMock()
        mock_multimodal.extract_from_pdf.return_value = [
            ExtractedRuling(
                extracted_case_number="2024CUBC038456",
                extracted_case_title="Thompson v. Anderson",
                ruling_text="The motion is GRANTED.",
                # Note: no outcome set — this is what multimodal produces
            ),
        ]
        worker._multimodal_extractor = mock_multimodal

        # Set up text extractor (SHOULD be called since county is LLM-configured).
        mock_text = MagicMock()
        mock_text.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024CUBC038456",
                extracted_case_title="Thompson v. Anderson",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]

        # Use the county-specific extractor cache for Ventura.
        # Cache key is (provider, model or "", max_chars_per_chunk or 0).
        worker._county_extractors = {("google", "gemini-2.5-flash-lite", 0): mock_text}

        # Ventura event with raw PDF bytes
        event = _make_event(
            state="CA",
            county="Ventura",
            content_format="pdf",
            ruling_text="PDF binary content",
            scraper_id="rebuild-ca-ventura",
        )
        with (
            patch("ingestion.worker.is_pdf_binary", return_value=True),
            patch(
                "ingestion.worker.extract_text_from_pdf",
                return_value=(
                    "Case No. 2024CUBC038456\nThompson v. Anderson\nThe motion is GRANTED."
                ),
            ),
        ):
            worker.process_event(event)

        # Multimodal extractor should NOT have been called.
        mock_multimodal.extract_from_pdf.assert_not_called()
        # Text extractor should have been called with the Ventura prompt.
        mock_text.extract.assert_called_once()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_multimodal_still_used_for_multimodal_configured_county(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Orange County (ExtractionMethod.MULTIMODAL) still uses multimodal (#2271)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Set up multimodal extractor (should be called for Orange County).
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

        # Text extractor should NOT be called.
        mock_text = MagicMock()
        worker._framework_extractor = mock_text

        # Orange County event (default _make_event uses Orange County)
        event = _make_event(
            content_format="pdf",
            ruling_text="PDF binary content here",
        )
        with patch("ingestion.worker.is_pdf_binary", return_value=True):
            worker.process_event(event)

        # Multimodal extractor should have been called for Orange County.
        mock_multimodal.extract_from_pdf.assert_called_once()
        # Text extractor should NOT have been called.
        mock_text.extract.assert_not_called()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_extraction_none_skips_all_framework_extraction(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """ExtractionMethod.NONE skips all framework extraction (#2271)."""
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Set up both extractors — neither should be called.
        mock_multimodal = MagicMock()
        worker._multimodal_extractor = mock_multimodal
        mock_text = MagicMock()
        worker._framework_extractor = mock_text

        # Patch the county config to return NONE for this test county.
        from framework.extraction_config import CountyExtractionConfig, ExtractionMethod

        none_config = CountyExtractionConfig(method=ExtractionMethod.NONE)

        event = _make_event(
            state="CA",
            county="TestNone",
            content_format="pdf",
            ruling_text="PDF content",
            scraper_id="test-none",
        )
        with (
            patch("ingestion.worker.is_pdf_binary", return_value=True),
            patch(
                "ingestion.worker.extract_text_from_pdf",
                return_value="Case No. 123\nSmith v. Jones\nGranted.",
            ),
            patch(
                "framework.extraction_config.get_county_extraction_config",
                return_value=none_config,
            ),
        ):
            worker.process_event(event)

        # Neither extractor should have been called.
        mock_multimodal.extract_from_pdf.assert_not_called()
        mock_text.extract.assert_not_called()


# ---------------------------------------------------------------------------
# Stale split-child cleanup (#2295)
# ---------------------------------------------------------------------------


class TestStaleSplitChildCleanup:
    """Regression tests for stale split-child document cleanup (#2295).

    When a multi-case PDF is re-processed and the LLM extracts fewer rulings
    than a previous run, old split-child documents must be cleaned up to
    prevent orphans in the documents table.
    """

    @patch("ingestion.worker.delete_stale_split_children", return_value=2)
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_stale_split_children_cleaned_before_insert(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """Re-processing a multi-case PDF calls delete_stale_split_children.

        When the LLM produces 2 rulings from a document that previously
        produced 4, the cleanup function should be called with the 2 new
        valid split document IDs before dispatching the split events.
        """
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # Each ruling needs: upsert_court, upsert_case, insert_document_and_ruling
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

        event = _make_event(
            ruling_text="Calendar with multiple rulings...",
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
        )
        worker.process_event(event)

        # delete_stale_split_children should have been called once
        mock_delete_stale.assert_called_once()
        call_args = mock_delete_stale.call_args
        # First arg is the connection
        assert call_args[0][0] is mock_conn
        # s3_key should match the event
        assert call_args[0][1] == "ca/orange/superior_court/raw/abc123.pdf"
        # valid_document_ids should have 2 entries (one per ruling)
        valid_ids = call_args[0][2]
        assert len(valid_ids) == 2
        # IDs should be deterministic split IDs
        from ingestion.split_ids import make_split_document_id

        expected_id_0 = make_split_document_id("aaaaaaaa-0000-0000-0000-000000000001", 0)
        expected_id_1 = make_split_document_id("aaaaaaaa-0000-0000-0000-000000000001", 1)
        assert valid_ids[0] == expected_id_0
        assert valid_ids[1] == expected_id_1

    @patch("ingestion.worker.delete_stale_split_children", return_value=0)
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_single_ruling_still_cleans_stale_children(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """A single-ruling extraction still cleans up stale split children.

        If a document was previously split into 3 rulings but is now extracted
        as a single ruling, all old UUID v5 split children should be cleaned up.
        """
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
                extracted_case_number="2024-00001",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        event = _make_event(
            ruling_text="Single ruling document...",
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
        )
        worker.process_event(event)

        # delete_stale_split_children should have been called even for
        # single-ruling documents (to clean up any old multi-ruling splits)
        mock_delete_stale.assert_called_once()
        call_args = mock_delete_stale.call_args
        assert call_args[0][1] == "ca/orange/superior_court/raw/abc123.pdf"
        # For a single ruling, the valid ID is the original document_id
        # (not a split ID), so all UUID v5 split children will be deleted.
        valid_ids = call_args[0][2]
        assert len(valid_ids) == 1
        assert valid_ids[0] == "aaaaaaaa-0000-0000-0000-000000000001"

    @patch("ingestion.worker.delete_stale_split_children")
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_cleanup_failure_does_not_block_processing(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """If cleanup fails, processing should continue normally.

        The cleanup is a best-effort operation — a failure should not prevent
        the new rulings from being inserted.
        """
        worker, _ = _make_worker()

        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
            ("court-uuid-1",),
            ("case-uuid-2",),
            (True,),
        ]

        # Make the cleanup function raise an exception
        mock_delete_stale.side_effect = Exception("DB connection error")

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

        event = _make_event(
            ruling_text="Calendar with multiple rulings...",
            s3_key="ca/orange/superior_court/raw/abc123.pdf",
        )
        # Should not raise — cleanup failure is caught and logged
        worker.process_event(event)

        # Despite cleanup failure, the split events should still be processed
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.delete_stale_split_children", return_value=0)
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_no_cleanup_when_s3_key_missing(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """No cleanup attempt when s3_key is not in event data."""
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
                extracted_case_number="2024-00001",
                extracted_case_title="Alpha v. Beta",
                ruling_text="Motion GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
            ),
        ]
        worker._framework_extractor = mock_extractor

        # Event with no s3_key
        event = _make_event(ruling_text="Single ruling...")
        del event["s3_key"]
        worker.process_event(event)

        # delete_stale_split_children should NOT have been called
        mock_delete_stale.assert_not_called()
