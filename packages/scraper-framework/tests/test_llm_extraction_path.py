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
    # Disable enrichment client by default so process_event tests don't trigger
    # live LLM calls that now raise LlmEnrichmentExhaustedError.
    worker._enrichment_client = None
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

    def test_max_output_tokens_passed_through(self) -> None:
        """max_output_tokens is forwarded to LlmExtractor when set (#2369).

        Dense multi-case department-calendar pages truncate at the default
        4,096-token cap; Orange County's config bumps this to 32,768.
        """
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result = worker._get_multimodal_extractor(max_output_tokens=32768)
            assert result is mock_instance
            mock_cls.assert_called_once_with(
                provider="google",
                model="gemini-2.5-flash-lite",
                max_output_tokens=32768,
            )

    def test_different_max_output_tokens_gets_separate_instances(self) -> None:
        """Different max_output_tokens values get separate cached instances (#2369)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_a = MagicMock()
            mock_b = MagicMock()
            mock_cls.side_effect = [mock_a, mock_b]

            result_a = worker._get_multimodal_extractor(max_output_tokens=4096)
            result_b = worker._get_multimodal_extractor(max_output_tokens=32768)
            assert result_a is not result_b
            assert mock_cls.call_count == 2

    def test_same_max_output_tokens_reuses_instance(self) -> None:
        """Calling with the same max_output_tokens reuses the cached extractor (#2369)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance

            result1 = worker._get_multimodal_extractor(max_output_tokens=32768)
            result2 = worker._get_multimodal_extractor(max_output_tokens=32768)
            assert result1 is result2
            mock_cls.assert_called_once()

    def test_max_output_tokens_failure_returns_none(self) -> None:
        """If init fails for a specific token limit, returns None (#2369)."""
        worker, _ = _make_worker()

        with patch(
            "ingestion.worker.LlmExtractor",
            side_effect=Exception("No Google API key"),
        ):
            result = worker._get_multimodal_extractor(max_output_tokens=32768)
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

    def test_different_max_output_tokens_gets_separate_instances(self) -> None:
        """Different max_output_tokens values get separate cached instances (#2355)."""
        worker, _ = _make_worker()

        with patch("ingestion.worker.LlmExtractor") as mock_cls:
            mock_a = MagicMock()
            mock_b = MagicMock()
            mock_cls.side_effect = [mock_a, mock_b]

            result_a = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 4096)
            result_b = worker._get_county_extractor("google", "gemini-2.5-flash-lite", 32768)
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
        # Cache key is (provider, model, max_chars_per_chunk, max_output_tokens).
        worker._county_extractors = {("google", "gemini-2.5-flash-lite", 0, 32768): mock_text}

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


# ---------------------------------------------------------------------------
# Tests: S3 PDF download for multimodal extraction (#2312)
# ---------------------------------------------------------------------------


class TestFetchRawPdfFromS3:
    """Tests for IngestionWorker._fetch_raw_pdf_from_s3."""

    def test_downloads_pdf_from_s3(self) -> None:
        """S3 get_object is called and bytes returned."""
        worker, _ = _make_worker()
        pdf_bytes = b"%PDF-1.4 fake content"
        body_mock = MagicMock()
        body_mock.read.return_value = pdf_bytes
        worker._s3_client.get_object.return_value = {"Body": body_mock}

        result = worker._fetch_raw_pdf_from_s3("ca/orange/raw/abc.pdf", "test-bucket", "doc-1")
        assert result == pdf_bytes
        worker._s3_client.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="ca/orange/raw/abc.pdf"
        )

    def test_returns_none_when_no_s3_key(self) -> None:
        """Returns None when s3_key is None."""
        worker, _ = _make_worker()
        result = worker._fetch_raw_pdf_from_s3(None, "test-bucket", "doc-1")
        assert result is None
        worker._s3_client.get_object.assert_not_called()

    def test_returns_none_on_s3_error(self) -> None:
        """Returns None when S3 raises an exception."""
        worker, _ = _make_worker()
        worker._s3_client.get_object.side_effect = Exception("NoSuchKey")

        result = worker._fetch_raw_pdf_from_s3("ca/orange/raw/abc.pdf", "test-bucket", "doc-1")
        assert result is None

    def test_uses_archive_bucket_when_s3_bucket_is_none(self) -> None:
        """Falls back to self._archive_bucket when s3_bucket is None."""
        worker, _ = _make_worker()
        body_mock = MagicMock()
        body_mock.read.return_value = b"%PDF-1.4"
        worker._s3_client.get_object.return_value = {"Body": body_mock}

        worker._fetch_raw_pdf_from_s3("key.pdf", None, "doc-1")
        worker._s3_client.get_object.assert_called_once_with(Bucket="test-bucket", Key="key.pdf")


class TestS3PdfDownloadInProcessEvent:
    """Tests for the S3 PDF download logic in process_event (#2312).

    These tests patch ``_llm_split_document`` to return True so the test
    can focus on whether S3 PDF download logic fires correctly without
    needing the full DB mock chain for split-event processing.
    """

    @patch.object(IngestionWorker, "_llm_split_document", return_value=True)
    @patch("ingestion.worker.psycopg")
    def test_multimodal_county_downloads_pdf_from_s3(
        self,
        mock_psycopg: MagicMock,
        mock_llm_split: MagicMock,
    ) -> None:
        """For a MULTIMODAL county (e.g. Orange), the worker downloads
        raw PDF bytes from S3 when ruling_text is not raw binary."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),  # find_or_create_court
            None,  # find_existing_document
        ]

        # Mock S3 download
        pdf_bytes = b"%PDF-1.4 fake orange county pdf"
        body_mock = MagicMock()
        body_mock.read.return_value = pdf_bytes
        worker._s3_client.get_object.return_value = {"Body": body_mock}

        # Event for Orange County with pre-extracted text (not raw binary)
        event = _make_event(
            scraper_id="ca-oc-tentatives",
            state="CA",
            county="Orange",
            content_format="pdf",
            ruling_text="Motion: Compel\n\nCtrl Click on Line 7",
            s3_key="ca/orange/raw/abc.pdf",
        )
        worker.process_event(event)

        # S3 should have been called to download the PDF
        worker._s3_client.get_object.assert_called_once()
        # _llm_split_document should have received the downloaded PDF bytes
        call_kwargs = mock_llm_split.call_args[1]
        assert call_kwargs["raw_pdf_bytes"] == pdf_bytes

    @patch.object(IngestionWorker, "_llm_split_document", return_value=True)
    @patch("ingestion.worker.psycopg")
    def test_non_multimodal_county_skips_s3_download(
        self,
        mock_psycopg: MagicMock,
        mock_llm_split: MagicMock,
    ) -> None:
        """For a non-MULTIMODAL county (e.g. Ventura), the worker does NOT
        download raw PDF bytes from S3."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),  # find_or_create_court
            None,  # find_existing_document
        ]

        # Event for Ventura (LLM method, not MULTIMODAL)
        event = _make_event(
            scraper_id="ca-ventura-tentatives",
            state="CA",
            county="Ventura",
            content_format="pdf",
            ruling_text="Some extracted text from pdfplumber",
            s3_key="ca/ventura/raw/abc.pdf",
        )
        worker.process_event(event)

        # S3 should NOT have been called to download the PDF
        worker._s3_client.get_object.assert_not_called()
        # raw_pdf_bytes passed to _llm_split_document should be None
        call_kwargs = mock_llm_split.call_args[1]
        assert call_kwargs["raw_pdf_bytes"] is None

    @patch.object(IngestionWorker, "_llm_split_document", return_value=True)
    @patch("ingestion.worker.psycopg")
    def test_raw_binary_pdf_skips_s3_download(
        self,
        mock_psycopg: MagicMock,
        mock_llm_split: MagicMock,
    ) -> None:
        """When ruling_text IS raw PDF binary, skip S3 download (already have bytes)."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),  # find_or_create_court
            None,  # find_existing_document
        ]

        # Event for OC with raw PDF binary in ruling_text
        pdf_binary = b"%PDF-1.4 raw binary content here"
        event = _make_event(
            scraper_id="ca-oc-tentatives-civil",
            state="CA",
            county="Orange",
            content_format="pdf",
            ruling_text=pdf_binary.decode("latin-1"),
            s3_key="ca/orange/raw/abc.pdf",
        )

        with patch("ingestion.worker.extract_text_from_pdf", return_value="extracted text"):
            worker.process_event(event)

        # S3 should NOT have been called — raw_pdf_bytes came from ruling_text
        worker._s3_client.get_object.assert_not_called()
        # raw_pdf_bytes passed to _llm_split_document should be the decoded binary
        call_kwargs = mock_llm_split.call_args[1]
        assert call_kwargs["raw_pdf_bytes"] == pdf_binary

    @patch.object(IngestionWorker, "_llm_split_document", return_value=True)
    @patch("ingestion.worker.psycopg")
    def test_html_content_skips_s3_download(
        self,
        mock_psycopg: MagicMock,
        mock_llm_split: MagicMock,
    ) -> None:
        """HTML documents should never trigger S3 PDF download."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),  # find_or_create_court
            None,  # find_existing_document
        ]

        event = _make_event(
            state="CA",
            county="Los Angeles",
            content_format="html",
            ruling_text="<p>The motion is GRANTED.</p>",
            s3_key="ca/la/raw/abc.html",
        )
        worker.process_event(event)

        # S3 should NOT be called for HTML content
        worker._s3_client.get_object.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: multimodal split events still trigger LLM enrichment (#2406)
# ---------------------------------------------------------------------------


class TestMultimodalSplitEnrichment:
    """Regression tests for #2406.

    When the live worker processes a multimodal-derived split event
    (``_llm_extracted=True``) with empty ``outcome``/``motion_type``, it MUST
    call ``_llm_enrich_fields`` and apply the returned enrichment values.

    The per-field LLM call (``extract_fields_llm``) is intentionally skipped
    when ``_llm_extracted`` is set, because the transcription pipeline
    already produced the base fields.  But that skip must NOT extend to
    enrichment, which is responsible for outcome/motion_type/case_title/
    parties per ``architecture-spec-v1.md`` §5.2.1.
    """

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_split_event_triggers_enrichment(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """A split event with _llm_extracted=True and null outcome gets enriched."""
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Simulate a split event from the multimodal transcription path:
        # ruling_text is present, but enrichment fields are empty.
        split_event = _make_event(
            ruling_text="The motion for summary judgment is GRANTED.",
            case_number="30-2024-01234567",
            _split_processed=True,
            _llm_extracted=True,
            outcome=None,
            motion_type=None,
            case_title=None,
        )

        enrichment_result = LlmEnrichmentResult(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome="granted",
            parties=EnrichmentParties(plaintiffs=["Smith"], defendants=["Jones"]),
        )

        with patch.object(
            worker,
            "_llm_enrich_fields",
            return_value=(enrichment_result, "ok"),
        ) as mock_enrich:
            worker.process_event(split_event)

        # Enrichment must have been called despite _llm_extracted=True.
        mock_enrich.assert_called_once()
        # The ruling_text argument (first positional or keyword) must match.
        args, kwargs = mock_enrich.call_args
        called_text = args[0] if args else kwargs.get("ruling_text")
        assert called_text == "The motion for summary judgment is GRANTED."

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_extracted_split_event_applies_enrichment_to_ruling_record(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """The enrichment result is applied to the persisted ruling record."""
        from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult

        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        split_event = _make_event(
            ruling_text="The motion is GRANTED.",
            case_number="30-2024-01234567",
            _split_processed=True,
            _llm_extracted=True,
            outcome=None,
            motion_type=None,
            case_title=None,
        )

        enrichment_result = LlmEnrichmentResult(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome="granted",
            parties=EnrichmentParties(plaintiffs=["Smith"], defendants=["Jones"]),
        )

        with (
            patch.object(
                worker,
                "_llm_enrich_fields",
                return_value=(enrichment_result, "ok"),
            ),
            patch("ingestion.worker.insert_document_and_ruling") as mock_insert,
            patch(
                "ingestion.worker.upsert_case_returning_title",
                return_value=("case-uuid-1", "Smith v. Jones"),
            ) as mock_upsert_case,
        ):
            worker.process_event(split_event)

        # insert_document_and_ruling receives the enriched outcome/motion_type.
        assert mock_insert.called
        insert_kwargs = mock_insert.call_args.kwargs
        assert insert_kwargs.get("outcome") == "granted"
        assert insert_kwargs.get("motion_type") == "msj"

        # upsert_case_returning_title receives the enriched case_title.
        assert mock_upsert_case.called
        upsert_kwargs = mock_upsert_case.call_args.kwargs
        assert upsert_kwargs.get("case_title") == "Smith v. Jones"


# ---------------------------------------------------------------------------
# SD calendar deterministic split routing (#2447)
# ---------------------------------------------------------------------------


class TestSDCalendarDeterministicSplit:
    """The worker routes SD calendar HTML through the deterministic splitter
    BEFORE invoking the LLM, regardless of ``scraper_id``.

    This closes Bug A and Bug B in #2447:

    * Bug A (swapped identifiers) — the LLM saw 60-100KB of HTML with
      30+ cases but a prompt assuming a single case.  It extracted one
      case's ruling_text while the outer event kept another case's
      identifiers, producing cross-case contamination.
    * Bug B (50000-char dumps) — when LLM extraction produced an empty
      ruling, ``convert_extracted_rulings`` substituted the full HTML
      as ruling_text, hitting the 50000-char truncation cap.

    The fix — routing through ``_try_sd_calendar_split`` at the top of
    ``_llm_split_document`` — bypasses the LLM entirely for calendar
    HTML content.  These tests verify the routing, not the splitter
    itself (covered in tests/courts/test_sd_calendar.py).
    """

    _CALENDAR_HTML = (
        "<!DOCTYPE html>\n"
        "<html><body>\n"
        "<h1>CIVIL CALENDAR For Friday, 03/13/2026</h1>\n"
        "<h3>CENTRAL DIVISION, CENTRAL COURTHOUSE</h3>\n"
        "<div class='department'><h2>Department: C-60</h2>"
        "<table class='tables'><thead><tr>"
        "<th>Time</th><th>Case#</th><th>Entitlement</th><th>Event</th>"
        "<th>Hearing Officer</th><th>Party</th><th>Attorney</th></tr></thead>"
        "<tbody><tr>"
        "<td>9:00 AM</td><td>24CU016153C</td>"
        "<td>Smith vs Jones</td><td>Motion Hearing</td>"
        "<td>Judge MATTHEW C. BRANER</td>"
        "<td><p>(PL) John Smith</p><p>(DF) Jane Jones</p></td>"
        "<td><p>Attorney A</p><p>Attorney B</p></td>"
        "</tr>"
        "<tr>"
        "<td>10:00 AM</td><td>25CU003887C</td>"
        "<td>Doe vs Roe</td><td>Demurrer/Motion to Strike</td>"
        "<td>Judge MATTHEW C. BRANER</td>"
        "<td><p>(PL) John Doe</p><p>(DF) Jane Roe</p></td>"
        "<td><p>Attorney C</p><p>Attorney D</p></td>"
        "</tr></tbody></table></div>\n"
        "</body></html>\n"
    )

    def test_rebuild_scraper_id_triggers_deterministic_split(self) -> None:
        """Synthetic rebuild events route through ``_try_sd_calendar_split``.

        This is the specific failure mode from #2447: ``rebuild_db.py``
        emits events with ``scraper_id='rebuild-ca-san_diego'`` that
        previously fell through to the county LLM default.
        """
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-san_diego",
                state="CA",
                county="San Diego",
                content_format="html",
                ruling_text=self._CALENDAR_HTML,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                self._CALENDAR_HTML,
                "CA",
                "San Diego",
            )

        assert result is True, (
            "_llm_split_document must return True when the deterministic "
            "splitter dispatches synthetic split events."
        )
        # Two motion-type hearings in the fixture → two dispatched events.
        assert mock_process.call_count == 2

    def test_ca_sd_calendar_scraper_id_triggers_deterministic_split(self) -> None:
        """Live ``ca-sd-calendar`` captures also route through the splitter."""
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="ca-sd-calendar",
                state="CA",
                county="San Diego",
                content_format="html",
                ruling_text=self._CALENDAR_HTML,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                self._CALENDAR_HTML,
                "CA",
                "San Diego",
            )

        assert result is True
        assert mock_process.call_count == 2

    def test_split_dispatched_events_carry_focused_ruling_text(self) -> None:
        """Each dispatched synthetic event has ruling_text that is:

        * A focused plain-text excerpt (no ``<`` / ``>`` HTML tags).
        * Well below the 50000-char truncation cap.
        * Contains ONLY its own case number (no cross-contamination).
        """
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-san_diego",
                state="CA",
                county="San Diego",
                content_format="html",
                ruling_text=self._CALENDAR_HTML,
            )
            worker._llm_split_document(
                event,
                event["document_id"],
                self._CALENDAR_HTML,
                "CA",
                "San Diego",
            )

        dispatched_events = [call.args[0] for call in mock_process.call_args_list]
        assert len(dispatched_events) == 2

        case_numbers = {e["case_number"] for e in dispatched_events}
        assert case_numbers == {"24CU016153C", "25CU003887C"}

        for ev in dispatched_events:
            rt = ev["ruling_text"]
            assert rt is not None
            assert "<" not in rt
            assert ">" not in rt
            assert "<!doctype" not in rt.lower()
            assert len(rt) < 50_000

            # No foreign case numbers in this ruling's text.
            own_case = ev["case_number"]
            other_cases = case_numbers - {own_case}
            for other in other_cases:
                assert other not in rt, f"ruling_text for {own_case} leaked {other}"

    def test_split_dispatched_events_are_marked_llm_extracted(self) -> None:
        """Synthetic split events must carry the ``_llm_extracted`` marker
        so downstream per-field LLM calls are skipped."""
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-san_diego",
                state="CA",
                county="San Diego",
                content_format="html",
                ruling_text=self._CALENDAR_HTML,
            )
            worker._llm_split_document(
                event,
                event["document_id"],
                self._CALENDAR_HTML,
                "CA",
                "San Diego",
            )

        for call in mock_process.call_args_list:
            dispatched = call.args[0]
            assert dispatched.get("_split_processed") is True
            assert dispatched.get("_llm_extracted") is True
            assert dispatched.get("_original_document_id") == event["document_id"]

    def test_non_calendar_content_does_not_trigger_split(self) -> None:
        """Non-calendar content falls through to the normal LLM extraction
        flow (the splitter returns False and the caller continues)."""
        worker, _ = _make_worker()

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="24STCV01234",
                extracted_case_title="Smith v. Jones",
                ruling_text="Motion granted.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion for Summary Judgment",
                extracted_parties=[],
            ),
        ]

        plain_text = "Case No. 24STCV01234\nSmith v. Jones\nMotion granted."

        with (
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch(
                "framework.extraction_config.get_county_extraction_config",
                return_value=None,
            ),
            patch.object(worker, "process_event") as mock_process,
        ):
            mock_cls.return_value = mock_extractor
            event = _make_event(
                scraper_id="ca-la-tentatives",
                state="CA",
                county="Los Angeles",
                content_format="text",
                ruling_text=plain_text,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                plain_text,
                "CA",
                "Los Angeles",
            )

        assert result is True
        # Normal LLM path — extractor called, one ruling dispatched.
        mock_extractor.extract.assert_called_once()
        assert mock_process.call_count == 1


class TestLADeterministicSplit:
    """The worker routes LA tentative-ruling HTML through the deterministic
    splitter BEFORE invoking the LLM, regardless of ``scraper_id``.

    This closes #2450: events emitted by ``scripts/rebuild_db.py`` with
    ``scraper_id='rebuild-ca-los_angeles'`` previously fell through to the
    county LLM default.  On Word-pasted HTML variants (lacking the standard
    ``DEPARTMENT N LAW AND MOTION RULINGS`` header), the LLM failed to
    extract ``case_number``, and the worker synthesized
    ``UNKNOWN-<uuid>`` — producing ~12% UNKNOWN rate among LA rulings.

    The fix — routing through ``_try_la_html_split`` at the top of
    ``_llm_split_document`` — bypasses the LLM entirely for LA HTML
    content.  These tests verify the routing, not the splitter itself
    (covered in tests/courts/test_la_tentatives.py).
    """

    def _load_fixture(self) -> str:
        from pathlib import Path

        fixture_path = Path(__file__).parent / "fixtures" / "la_ruling_25stcv31489_word_paste.html"
        return fixture_path.read_text(encoding="utf-8", errors="replace")

    def test_rebuild_scraper_id_triggers_deterministic_split(self) -> None:
        """Synthetic rebuild events route through ``_try_la_html_split``.

        This is the specific failure mode from #2450: ``rebuild_db.py`` emits
        events with ``scraper_id='rebuild-ca-los_angeles'`` that previously
        fell through to the county LLM default and produced UNKNOWN-<uuid>
        case numbers.
        """
        la_html = self._load_fixture()
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-los_angeles",
                state="CA",
                county="Los Angeles",
                content_format="html",
                ruling_text=la_html,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                la_html,
                "CA",
                "Los Angeles",
            )

        assert result is True, (
            "_llm_split_document must return True when the deterministic "
            "LA splitter dispatches synthetic split events."
        )
        assert mock_process.call_count == 1

        dispatched = mock_process.call_args_list[0].args[0]
        # The fixture's explicit Case Number header must produce the real
        # case number — no UNKNOWN-<uuid> fallback.
        assert dispatched["case_number"] == "25STCV31489"
        assert not dispatched["case_number"].startswith("UNKNOWN-")

    def test_live_ca_la_tentatives_scraper_id_triggers_deterministic_split(
        self,
    ) -> None:
        """Live ``ca-la-tentatives-civil`` captures also route through the
        splitter — the detection is content-based, not scraper_id-based."""
        la_html = self._load_fixture()
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="ca-la-tentatives-civil",
                state="CA",
                county="Los Angeles",
                content_format="html",
                ruling_text=la_html,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                la_html,
                "CA",
                "Los Angeles",
            )

        assert result is True
        assert mock_process.call_count == 1
        dispatched = mock_process.call_args_list[0].args[0]
        assert dispatched["case_number"] == "25STCV31489"

    def test_la_split_events_carry_expected_fields(self) -> None:
        """Dispatched synthetic events carry the _llm_extracted marker and
        the fields extracted from the LA HTML (hearing_date, department)."""
        la_html = self._load_fixture()
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-los_angeles",
                state="CA",
                county="Los Angeles",
                content_format="html",
                ruling_text=la_html,
            )
            worker._llm_split_document(
                event,
                event["document_id"],
                la_html,
                "CA",
                "Los Angeles",
            )

        dispatched = mock_process.call_args_list[0].args[0]
        assert dispatched["_split_processed"] is True
        assert dispatched["_llm_extracted"] is True
        assert dispatched["_original_document_id"] == event["document_id"]
        # Fixture header: "Hearing Date: March 10, 2026" and "Dept: 55".
        assert dispatched["hearing_date"] == "2026-03-10"
        assert dispatched["department"] == "55"

    def test_la_split_dispatched_ruling_text_is_focused(self) -> None:
        """The dispatched ruling_text must be a focused per-case excerpt,
        not the full raw HTML page.  (Avoids #2447-style 50000-char dumps.)"""
        la_html = self._load_fixture()
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-los_angeles",
                state="CA",
                county="Los Angeles",
                content_format="html",
                ruling_text=la_html,
            )
            worker._llm_split_document(
                event,
                event["document_id"],
                la_html,
                "CA",
                "Los Angeles",
            )

        dispatched = mock_process.call_args_list[0].args[0]
        rt = dispatched["ruling_text"]
        assert rt is not None
        # Fixture references the case number inside the ruling text.
        assert "25STCV31489" in rt
        # Text should be well below the 50000-char cap.
        assert len(rt) < 50_000

    def test_non_la_content_does_not_trigger_split(self) -> None:
        """Non-LA content (no div#speechSynthesis marker) falls through to
        the normal LLM extraction flow (the splitter returns False)."""
        worker, _ = _make_worker()

        # Plain-text OC PDF content — no LA HTML marker.
        plain_text = "Case No. 2024-01234567\nSmith v. Jones\nThe motion is GRANTED."

        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = [
            ExtractedRuling(
                extracted_case_number="2024-01234567",
                extracted_case_title="Smith v. Jones",
                ruling_text="The motion is GRANTED.",
                outcome=ExtractionOutcome.GRANTED,
                motion_type="Motion for Summary Judgment",
                extracted_parties=[],
            ),
        ]

        with (
            patch("ingestion.worker.LlmExtractor") as mock_cls,
            patch(
                "framework.extraction_config.get_county_extraction_config",
                return_value=None,
            ),
            patch.object(worker, "process_event") as mock_process,
        ):
            mock_cls.return_value = mock_extractor
            event = _make_event(
                scraper_id="ca-oc-tentatives",
                state="CA",
                county="Orange",
                content_format="pdf",
                ruling_text=plain_text,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                plain_text,
                "CA",
                "Orange",
            )

        assert result is True
        # LA splitter skipped → normal LLM path invoked.
        mock_extractor.extract.assert_called_once()
        assert mock_process.call_count == 1


class TestFresnoPdfSplit:
    """The worker routes Fresno multi-ruling PDFs through the deterministic
    splitter BEFORE invoking the LLM, regardless of ``scraper_id``.

    This closes #3534: Fresno multi-case PDFs were mis-attributing every
    ruling to the case referenced in the page-1 preamble, because the LLM
    only extracted the first case encountered in the document.

    The fix — routing through ``_try_fresno_pdf_split`` at the top of
    ``_llm_split_document`` (after the SD and LA splits) — bypasses the LLM
    entirely for Fresno multi-ruling PDFs.  These tests verify the routing,
    not the splitter itself (covered in tests/courts/test_fresno_tentatives.py).
    """

    def _load_fixture_text(self, name: str) -> str:
        """Load PDF fixture via _extract_pdf_text (same path as production)."""
        from pathlib import Path

        from courts.ca.pdf_link_scraper import _extract_pdf_text

        fixture_path = Path(__file__).parent / "fixtures" / name
        return _extract_pdf_text(fixture_path.read_bytes())

    def test_rebuild_scraper_id_triggers_deterministic_split(self) -> None:
        """Synthetic rebuild events route through ``_try_fresno_pdf_split``.

        This is the specific failure mode from #3534: ``rebuild_db.py`` emits
        events with ``scraper_id='rebuild-ca-fresno'`` that previously fell
        through to the LLM and mis-attributed all rulings to the preamble case.
        The 403 fixture has 8 rulings; all 8 must be dispatched.
        """
        ruling_text = self._load_fixture_text("fresno_403_20260310_d019042f.pdf")
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-fresno",
                state="CA",
                county="Fresno",
                content_format="pdf",
                ruling_text=ruling_text,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                ruling_text,
                "CA",
                "Fresno",
            )

        assert result is True, (
            "_llm_split_document must return True when the deterministic "
            "Fresno splitter dispatches synthetic split events."
        )
        assert mock_process.call_count == 8, (
            f"Expected 8 dispatched events (one per ruling in the 403 fixture), "
            f"got {mock_process.call_count}"
        )

    def test_live_scraper_id_triggers_deterministic_split(self) -> None:
        """Live ``ca-fresno-tentatives-civil`` captures also route through the
        splitter — detection is county+format gated, not scraper_id-gated."""
        ruling_text = self._load_fixture_text("fresno_403_20260310_d019042f.pdf")
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="ca-fresno-tentatives-civil",
                state="CA",
                county="Fresno",
                content_format="pdf",
                ruling_text=ruling_text,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                ruling_text,
                "CA",
                "Fresno",
            )

        assert result is True
        assert mock_process.call_count == 8

    def test_single_ruling_falls_through_to_llm(self) -> None:
        """Single-ruling Fresno PDFs return False, falling through to the LLM.

        The 503 fixture has exactly 1 ruling.  ``_split_rulings`` still
        returns a 1-element list — but the task spec (AC4) says the LLM path
        handles it.  For a true single-ruling case we use synthetic text that
        produces zero split entries so ``_split_rulings`` returns ``[]``.

        We verify ``_try_fresno_pdf_split`` returns ``False`` directly (the
        split function is module-level, so we can call it without the full
        worker machinery).
        """
        from ingestion.worker import _try_fresno_pdf_split

        dispatched: list = []

        # Text with no numbered ruling entries — _split_rulings returns [].
        no_rulings_text = (
            "Tentative Rulings for March 11, 2026\n"
            "Department 503\n"
            "There are no tentative rulings for the following cases.\n"
            "The hearing will go forward on these matters.\n"
        )

        event = _make_event(
            scraper_id="ca-fresno-tentatives-civil",
            state="CA",
            county="Fresno",
            content_format="pdf",
        )
        result = _try_fresno_pdf_split(
            event,
            event["document_id"],
            no_rulings_text,
            dispatched.append,
        )

        assert result is False, (
            "_try_fresno_pdf_split must return False when _split_rulings "
            "finds no numbered entries, so the caller falls through to the LLM."
        )
        assert dispatched == [], "No events should be dispatched when split returns []"

    def test_split_events_carry_distinct_case_numbers(self) -> None:
        """Each dispatched event has a distinct case_number matching its own
        ruling_text — no cross-case contamination (the bug fixed by #3534).
        """
        import re

        ruling_text = self._load_fixture_text("fresno_403_20260310_d019042f.pdf")
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-fresno",
                state="CA",
                county="Fresno",
                content_format="pdf",
                ruling_text=ruling_text,
            )
            worker._llm_split_document(
                event,
                event["document_id"],
                ruling_text,
                "CA",
                "Fresno",
            )

        dispatched_events = [call.args[0] for call in mock_process.call_args_list]
        assert len(dispatched_events) == 8

        case_numbers = [e["case_number"] for e in dispatched_events]
        # All case numbers must be distinct.
        assert len(case_numbers) == len(set(case_numbers)), (
            f"Duplicate case_numbers detected: {case_numbers}"
        )

        # Each event's case_number must appear in its own ruling_text.
        _case_num_re = re.compile(r"\d{2,4}CECG\d+")
        for ev in dispatched_events:
            rt = ev["ruling_text"]
            assert ev["case_number"] is not None
            found = _case_num_re.findall(rt)
            assert ev["case_number"] in found, (
                f"case_number {ev['case_number']!r} not found in its own ruling_text. "
                f"Found case numbers in text: {found}"
            )

        # Verify split event metadata fields.
        for ev in dispatched_events:
            assert ev.get("_split_processed") is True
            assert ev.get("_llm_extracted") is True
            assert ev.get("_original_document_id") == event["document_id"]
            assert ev.get("ruling_text_html") is None

    def test_non_fresno_county_does_not_trigger_split(self) -> None:
        """Non-Fresno counties (e.g. Orange) are not routed through the
        Fresno splitter even when content_format is ``'pdf'``."""
        from ingestion.worker import _try_fresno_pdf_split

        dispatched: list = []

        # Fresno-style numbered entry text — would split if county were Fresno.
        text = (
            "(1) Tentative Ruling\n"
            "Re: Smith v. Jones\n"
            "Superior Court Case No. 25CECG00001\n"
            "Tentative Ruling:\nGranted.\n"
            "(2) Tentative Ruling\n"
            "Re: Doe v. Roe\n"
            "Superior Court Case No. 25CECG00002\n"
            "Tentative Ruling:\nDenied.\n"
        )

        event = _make_event(
            scraper_id="ca-oc-tentatives",
            state="CA",
            county="Orange",
            content_format="pdf",
        )
        result = _try_fresno_pdf_split(
            event,
            event["document_id"],
            text,
            dispatched.append,
        )

        assert result is False, "_try_fresno_pdf_split must return False for non-Fresno counties."
        assert dispatched == []

    def test_non_pdf_content_format_does_not_trigger_split(self) -> None:
        """Fresno county with non-PDF content_format falls through — the
        splitter only triggers on county=Fresno AND content_format=pdf."""
        from ingestion.worker import _try_fresno_pdf_split

        dispatched: list = []

        text = (
            "(1) Tentative Ruling\n"
            "Re: Smith v. Jones\n"
            "Superior Court Case No. 25CECG00001\n"
            "Tentative Ruling:\nGranted.\n"
        )

        event = _make_event(
            scraper_id="ca-fresno-tentatives-civil",
            state="CA",
            county="Fresno",
            content_format="html",
        )
        result = _try_fresno_pdf_split(
            event,
            event["document_id"],
            text,
            dispatched.append,
        )

        assert result is False, (
            "_try_fresno_pdf_split must return False when content_format != 'pdf'."
        )
        assert dispatched == []

    def test_single_numbered_ruling_falls_through_to_llm(self) -> None:
        """Single-ruling Fresno PDFs (len == 1) must return False (#3599).

        When ``_split_rulings`` returns a 1-element list, the deterministic
        splitter cannot reliably populate outcome/motion_type, so the function
        must fall through to the LLM path.  This is the regression assertion
        for #3599: before the fix, len==1 would proceed through the split loop
        and dispatch an event with ``_llm_extracted=True`` but without a real
        LLM call, leaving outcome/motion_type as None.
        """
        from ingestion.worker import _try_fresno_pdf_split

        dispatched: list = []

        # One numbered entry — _split_rulings returns a 1-element list.
        single_ruling_text = (
            "(1) Tentative Ruling\n"
            "Re: Smith v. Jones\n"
            "Superior Court Case No. 25CECG00001\n"
            "Hearing Date: April 28, 2026 (Dept. 503)\n"
            "Motion: Demurrer to Complaint\n"
            "Tentative Ruling:\n"
            "To sustain the demurrer with leave to amend.\n"
        )

        event = _make_event(
            scraper_id="ca-fresno-tentatives-civil",
            state="CA",
            county="Fresno",
            content_format="pdf",
        )
        result = _try_fresno_pdf_split(
            event,
            event["document_id"],
            single_ruling_text,
            dispatched.append,
        )

        assert result is False, (
            "_try_fresno_pdf_split must return False when _split_rulings "
            "returns a 1-element list, so the caller falls through to the LLM."
        )
        assert dispatched == [], "No events should be dispatched for a single-ruling Fresno PDF."

    def test_multi_ruling_still_dispatches_with_llm_extracted_flag(self) -> None:
        """Multi-ruling Fresno PDFs still dispatch with _split_processed=True
        and _llm_extracted=True (regression guard for #3534).

        Uses the 403 fixture (8 rulings).  Checks that the ``_llm_extracted``
        flag is set on every dispatched event so downstream workers skip a
        redundant LLM call.
        """
        ruling_text = self._load_fixture_text("fresno_403_20260310_d019042f.pdf")
        worker, _ = _make_worker()

        with patch.object(worker, "process_event") as mock_process:
            event = _make_event(
                scraper_id="rebuild-ca-fresno",
                state="CA",
                county="Fresno",
                content_format="pdf",
                ruling_text=ruling_text,
            )
            result = worker._llm_split_document(
                event,
                event["document_id"],
                ruling_text,
                "CA",
                "Fresno",
            )

        assert result is True
        assert mock_process.call_count == 8

        dispatched_events = [call.args[0] for call in mock_process.call_args_list]
        for ev in dispatched_events:
            assert ev.get("_split_processed") is True, (
                f"Expected _split_processed=True on event {ev.get('case_number')!r}"
            )
            assert ev.get("_llm_extracted") is True, (
                f"Expected _llm_extracted=True on event {ev.get('case_number')!r}"
            )
