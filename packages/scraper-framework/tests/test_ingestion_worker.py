"""Tests for per-child LlmEnrichmentExhaustedError handling in ingestion worker (#3600).

Verifies that:
1. When LlmEnrichmentExhaustedError is raised on the first child of a
   multi-ruling split, remaining children still dispatch and land.
2. Single-event behavior is unchanged — LlmEnrichmentExhaustedError on a
   non-split event still propagates so _process_message can retry/dead-letter.
3. _process_message retry counter does not accumulate from per-child exhaustion.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from framework.llm_enrichment import LlmEnrichmentExhaustedError
from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    # Disable enrichment client to avoid live LLM calls in tests.
    worker._enrichment_client = None
    # Disable framework extractor by default.
    worker._get_framework_extractor = lambda: None  # type: ignore[method-assign]
    return worker, redis_mock


def _make_sd_event(**overrides: object) -> dict:
    """Return an SD calendar-like event payload (non-split, triggers SD split path)."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "scraper_id": "ca-sd-calendar",
        "state": "CA",
        "county": "San Diego",
        "court": "Superior Court",
        "source_url": "https://www.sdcourt.ca.gov/tentativerulings/1",
        "content_format": "html",
        "content_hash": "abc123",
        "s3_key": "ca/san_diego/superior_court/raw/abc123.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "CIVIL CALENDAR For Department 1 Case No. 12345",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_la_event(**overrides: object) -> dict:
    """Return an LA HTML-like event payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000002",
        "scraper_id": "ca-la-tentatives-civil",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://www.lacourt.org/tentativerulings/2",
        "content_format": "html",
        "content_hash": "def456",
        "s3_key": "ca/los_angeles/superior_court/raw/def456.html",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": '<div id="speechSynthesis"><b>Case Number:</b> 24STCV001</div>',
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_fresno_event(**overrides: object) -> dict:
    """Return a Fresno PDF-like event payload."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000003",
        "scraper_id": "ca-fresno-tentatives",
        "state": "CA",
        "county": "FRESNO",
        "court": "Superior Court",
        "source_url": "https://fresno.courts.ca.gov/tentativerulings/3",
        "content_format": "pdf",
        "content_hash": "ghi789",
        "s3_key": "ca/fresno/superior_court/raw/ghi789.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "(1) Tentative Ruling\n(2) Tentative Ruling",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_riverside_event(**overrides: object) -> dict:
    """Return a Riverside PDF-like event payload (#3649)."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000005",
        "scraper_id": "ca-riverside-tentatives-civil",
        "state": "CA",
        "county": "Riverside",
        "court": "Superior Court",
        "source_url": "https://www.riverside.courts.ca.gov/tentativerulings/5",
        "content_format": "pdf",
        "content_hash": "mno345",
        "s3_key": "ca/riverside/superior_court/raw/mno345.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "1.\nCVPS2400001 SMITH vs JONES\nTentative Ruling: Granted.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


def _make_llm_event(**overrides: object) -> dict:
    """Return a generic event for LLM split path testing."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000004",
        "scraper_id": "ca-oc-tentatives",
        "state": "CA",
        "county": "Orange",
        "court": "Superior Court",
        "source_url": "https://www.occourts.org/tentativerulings/4",
        "content_format": "pdf",
        "content_hash": "jkl012",
        "s3_key": "ca/orange/superior_court/raw/jkl012.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "Case No. 2024-00001\nSmith v. Jones\nGRANTED.",
        "hearing_date": "2026-03-05",
        "capture_timestamp": "2026-03-04T23:00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures for fake split rulings
# ---------------------------------------------------------------------------


def _make_fake_sd_rulings() -> list:
    """Return 3 fake SDSplitRuling objects for testing."""
    from courts.ca.sd_calendar import SDSplitRuling

    return [
        SDSplitRuling(
            ruling_index=0,
            case_number=f"2024-0000{i}",
            ruling_text=f"Ruling text for case {i}",
            case_title=f"Case {i} Title",
        )
        for i in range(3)
    ]


def _make_fake_la_rulings() -> list:
    """Return 3 fake LASplitRuling objects for testing."""
    from courts.ca.la_tentatives import LASplitRuling

    return [
        LASplitRuling(
            ruling_index=i,
            case_number=f"24STCV0000{i}",
            ruling_text=f"LA ruling {i}",
        )
        for i in range(3)
    ]


def _make_fake_fresno_rulings() -> list:
    """Return 3 fake Fresno SplitRuling objects for testing."""
    from courts.ca.fresno_tentatives import SplitRuling

    return [
        SplitRuling(
            ruling_index=i,
            case_number=f"24CV0000{i}",
            ruling_text=f"Fresno ruling {i}",
            case_title=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
        )
        for i in range(3)
    ]


def _make_fake_riverside_rulings() -> list:
    """Return 3 fake Riverside SplitRuling objects for testing (#3649)."""
    from courts.ca.riverside_tentatives import SplitRuling

    return [
        SplitRuling(
            ruling_index=i + 1,
            case_number=f"CVPS230000{i}",
            ruling_text=f"Riverside ruling {i}\nTentative Ruling: Granted.",
        )
        for i in range(3)
    ]


def _make_fake_extracted_rulings() -> list[ExtractedRuling]:
    """Return 3 fake ExtractedRuling objects for LLM split path testing.

    Ruling texts have different lengths to avoid triggering the duplicate
    ruling text length validation flag (#2350), which would add an extra commit.
    """
    texts = [
        "Motion GRANTED for Alpha v. Beta",
        "Motion DENIED for Gamma v. Delta and all related parties",
        "Motion CONTINUED pending submission of additional briefing by counsel",
    ]
    return [
        ExtractedRuling(
            extracted_case_number=f"2024-0000{i}",
            extracted_case_title=f"LLM Case {i}",
            ruling_text=texts[i],
            outcome=ExtractionOutcome.GRANTED,
        )
        for i in range(3)
    ]


# ---------------------------------------------------------------------------
# AC1: First-child exhaustion does not abort siblings
# ---------------------------------------------------------------------------


class TestFirstChildExhaustionDoesNotAbortSiblings:
    """AC1: LlmEnrichmentExhaustedError on first child must not abort remaining siblings.

    Parametrized over all 4 splitter paths.
    """

    def _make_side_effect(self, worker: IngestionWorker) -> tuple[list, object]:
        """Return (call_log, side_effect_fn) that raises on first split child, succeeds after."""
        call_log: list[dict] = []
        real_process_event = worker.process_event

        def side_effect(event_data: dict) -> object:
            call_log.append(event_data.copy())
            if event_data.get("_split_processed") and len(call_log) == 1:
                raise LlmEnrichmentExhaustedError("exhausted on first child")
            return real_process_event(event_data)

        return call_log, side_effect

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_sd_calendar_first_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """SD calendar: first child exhaustion does not abort siblings 2 and 3."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # 2 successful children each need: upsert_court, upsert_case, insert_document
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
            ("court-uuid-1",),
            ("case-uuid-2",),
            (True,),
        ]

        fake_rulings = _make_fake_sd_rulings()
        call_log, side_effect = self._make_side_effect(worker)
        worker.process_event = side_effect  # type: ignore[method-assign]

        with (
            patch("courts.ca.sd_calendar.is_sd_calendar_html", return_value=True),
            patch("courts.ca.sd_calendar._split_rulings", return_value=fake_rulings),
        ):
            event = _make_sd_event()
            # SD path triggers inside process_event → _llm_split_document chain;
            # however the split functions are called at the top of process_event.
            # We test the splitter functions directly with process_event as dispatch.
            from ingestion.worker import _try_sd_calendar_split

            _try_sd_calendar_split(event, event["document_id"], event["ruling_text"], side_effect)

        # All 3 children were attempted.
        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3, f"Expected 3 dispatch calls, got {len(split_calls)}"
        # Children 2 and 3 committed (first was exhausted and skipped).
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_la_html_first_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LA HTML: first child exhaustion does not abort siblings 2 and 3."""
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

        fake_rulings = _make_fake_la_rulings()
        call_log, side_effect = self._make_side_effect(worker)

        with (
            patch("courts.ca.la_tentatives.is_la_html", return_value=True),
            patch("courts.ca.la_tentatives._split_rulings", return_value=fake_rulings),
        ):
            event = _make_la_event()
            from ingestion.worker import _try_la_html_split

            _try_la_html_split(event, event["document_id"], event["ruling_text"], side_effect)

        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_la_html_split_passes_judge_name_through(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LA HTML: split_event carries LASplitRuling.judge_name through to dispatch (AC3, #4282).

        Without this pass-through, dept-25 rulings whose HTML uses the
        JUDGE/DEPT form-layout header fall through to the directory's
        primary-assignment judge (Latrice A. G. Byrdsong) instead of the
        day-of-bench judge (Mkrtchyan) named in the document text.
        """
        from courts.ca.la_tentatives import LASplitRuling

        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # One LASplitRuling with a non-None judge_name.  The worker should
        # surface that value in the dispatched split_event so downstream
        # process_event sees a populated judge_name and skips the
        # dept-to-judge directory fallback.
        fake_rulings = [
            LASplitRuling(
                ruling_index=0,
                case_number="25STLC05162",
                ruling_text="LA ruling for dept-25 — Mkrtchyan presiding.",
                department="25",
                judge_name="Mkrtchyan",
            )
        ]
        call_log, side_effect = self._make_side_effect(worker)

        with (
            patch("courts.ca.la_tentatives.is_la_html", return_value=True),
            patch("courts.ca.la_tentatives._split_rulings", return_value=fake_rulings),
        ):
            event = _make_la_event()
            from ingestion.worker import _try_la_html_split

            _try_la_html_split(event, event["document_id"], event["ruling_text"], side_effect)

        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 1
        # AC3 verification: the split_event passed to dispatch carries
        # judge_name=Mkrtchyan.
        assert split_calls[0]["judge_name"] == "Mkrtchyan"

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_la_html_split_falls_back_to_event_judge_name(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LA HTML: when sr.judge_name is None, fall back to event_data['judge_name'] (#4282).

        Preserves the pre-fix behaviour for split rulings whose HTML
        carries no JUDGE/DEPT or signature line — the original event's
        judge_name (e.g. seeded from the DB on a reingest) flows through
        instead of being clobbered to None.
        """
        from courts.ca.la_tentatives import LASplitRuling

        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        fake_rulings = [
            LASplitRuling(
                ruling_index=0,
                case_number="25STLC05162",
                ruling_text="LA ruling with no JUDGE/DEPT or signature line.",
                department="25",
                judge_name=None,
            )
        ]
        call_log, side_effect = self._make_side_effect(worker)

        with (
            patch("courts.ca.la_tentatives.is_la_html", return_value=True),
            patch("courts.ca.la_tentatives._split_rulings", return_value=fake_rulings),
        ):
            event = _make_la_event(judge_name="DB Seeded Judge")
            from ingestion.worker import _try_la_html_split

            _try_la_html_split(event, event["document_id"], event["ruling_text"], side_effect)

        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 1
        # Fallback: when sr.judge_name is None, use event_data['judge_name'].
        assert split_calls[0]["judge_name"] == "DB Seeded Judge"

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_fresno_pdf_first_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Fresno PDF: first child exhaustion does not abort siblings 2 and 3."""
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

        fake_rulings = _make_fake_fresno_rulings()
        call_log, side_effect = self._make_side_effect(worker)

        with patch("courts.ca.fresno_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_fresno_event()
            from ingestion.worker import _try_fresno_pdf_split

            _try_fresno_pdf_split(event, event["document_id"], event["ruling_text"], side_effect)

        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_riverside_pdf_first_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """Riverside PDF: first child exhaustion does not abort siblings 2 and 3 (#3649)."""
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

        fake_rulings = _make_fake_riverside_rulings()
        call_log, side_effect = self._make_side_effect(worker)

        with patch("courts.ca.riverside_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_riverside_event()
            from ingestion.worker import _try_riverside_pdf_split

            _try_riverside_pdf_split(event, event["document_id"], event["ruling_text"], side_effect)

        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3
        assert mock_conn.commit.call_count == 2

    @patch("ingestion.worker.delete_stale_split_children", return_value=0)
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_split_first_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """LLM split path: first child exhaustion does not abort siblings 2 and 3."""
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # 2 successful children each need: upsert_court, upsert_case, insert_document
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
            ("court-uuid-1",),
            ("case-uuid-2",),
            (True,),
        ]

        fake_rulings = _make_fake_extracted_rulings()
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = fake_rulings
        worker._framework_extractor = mock_extractor
        worker._get_framework_extractor = lambda: mock_extractor  # type: ignore[method-assign]

        call_log: list[dict] = []
        split_child_count = {"n": 0}
        original_process_event = worker.process_event

        def side_effect(event_data: dict) -> object:
            call_log.append(event_data.copy())
            if event_data.get("_split_processed"):
                split_child_count["n"] += 1
                if split_child_count["n"] == 1:
                    raise LlmEnrichmentExhaustedError("exhausted on first child")
            return original_process_event(event_data)

        worker.process_event = side_effect  # type: ignore[method-assign]

        event = _make_llm_event()
        # Call the top-level process_event which will trigger _llm_split_document.
        # The first call is the outer (non-split) event.
        worker.process_event(event)

        # All 3 split children were dispatched (first raised, but loop continued).
        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3, f"Expected 3 split dispatch calls, got {len(split_calls)}"
        # Children 2 and 3 committed successfully (2 commits from 2 successful children).
        assert mock_conn.commit.call_count == 2


# ---------------------------------------------------------------------------
# AC1 continued: All-child exhaustion logs and succeeds (no re-raise)
# ---------------------------------------------------------------------------


class TestAllChildExhaustionLogsAndSucceeds:
    """All children exhausted: each is logged as critical, splitter returns True."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_sd_all_child_exhaustion_logs_and_succeeds(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SD calendar: all children exhausted → logged, returns True (no re-raise)."""
        import logging

        fake_rulings = _make_fake_sd_rulings()

        def always_exhausted(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                raise LlmEnrichmentExhaustedError("all exhausted")

        with (
            patch("courts.ca.sd_calendar.is_sd_calendar_html", return_value=True),
            patch("courts.ca.sd_calendar._split_rulings", return_value=fake_rulings),
            caplog.at_level(logging.CRITICAL, logger="ingestion.worker"),
        ):
            event = _make_sd_event()
            from ingestion.worker import _try_sd_calendar_split

            result = _try_sd_calendar_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True
        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) == 3, f"Expected 3 critical logs, got {len(critical_msgs)}"
        for record in critical_msgs:
            assert "per-child enrichment exhausted" in record.getMessage()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_fresno_all_child_exhaustion_logs_and_succeeds(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fresno PDF: all children exhausted → logged, returns True (no re-raise)."""
        import logging

        fake_rulings = _make_fake_fresno_rulings()

        def always_exhausted(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                raise LlmEnrichmentExhaustedError("all exhausted")

        with (
            patch("courts.ca.fresno_tentatives._split_rulings", return_value=fake_rulings),
            caplog.at_level(logging.CRITICAL, logger="ingestion.worker"),
        ):
            event = _make_fresno_event()
            from ingestion.worker import _try_fresno_pdf_split

            result = _try_fresno_pdf_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True
        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) == 3
        for record in critical_msgs:
            assert "per-child enrichment exhausted" in record.getMessage()

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_riverside_all_child_exhaustion_logs_and_succeeds(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Riverside PDF: all children exhausted → logged, returns True (no re-raise) (#3649)."""
        import logging

        fake_rulings = _make_fake_riverside_rulings()

        def always_exhausted(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                raise LlmEnrichmentExhaustedError("all exhausted")

        with (
            patch("courts.ca.riverside_tentatives._split_rulings", return_value=fake_rulings),
            caplog.at_level(logging.CRITICAL, logger="ingestion.worker"),
        ):
            event = _make_riverside_event()
            from ingestion.worker import _try_riverside_pdf_split

            result = _try_riverside_pdf_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True
        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) == 3
        for record in critical_msgs:
            assert "per-child enrichment exhausted" in record.getMessage()


# ---------------------------------------------------------------------------
# AC2: Single-event (non-split) exhaustion still propagates
# ---------------------------------------------------------------------------


class TestSingleEventExhaustionStillPropagates:
    """AC2: Non-split events must not have LlmEnrichmentExhaustedError swallowed."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_single_event_exhaustion_still_propagates(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LlmEnrichmentExhaustedError on a non-split event propagates out of process_event.

        This verifies AC2: the per-child swallow logic only applies inside split
        dispatch loops — standalone events (without _split_processed) must not
        have their exhaustion errors eaten.

        We use a synthetic split event (_split_processed=True, _llm_extracted=True)
        so it bypasses all the split-detection paths and goes straight to the
        single-document processing flow that calls _llm_enrich_fields.
        """
        worker, _ = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
        ]

        # Enable the enrichment client so _llm_enrich_fields is called.
        worker._enrichment_client = MagicMock()
        worker._llm_enrichment_enabled = True

        # Simulate _llm_enrich_fields returning (None, "errored") — the new
        # signature after #3780: the helper catches LlmEnrichmentExhaustedError
        # internally and surfaces it as a status tag.  The dispatch site then
        # re-raises after recording the diagnostic marker.
        with patch.object(
            worker,
            "_llm_enrich_fields",
            return_value=(None, "errored"),
        ):
            # Use a split event (pre-extracted, bypasses all split-detection paths)
            # with case_number set so it's not treated as a bad-split fragment.
            event = _make_llm_event(
                _split_processed=True,
                _llm_extracted=True,
                _split_index=0,
                _split_count=1,
                case_number="24CV00001",
                outcome=None,
                motion_type=None,
            )

            with pytest.raises(LlmEnrichmentExhaustedError):
                worker.process_event(event)

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_split_event_exhaustion_is_swallowed_by_dispatch_loop(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """LlmEnrichmentExhaustedError from a split event is swallowed by the dispatch loop.

        Contrast with the single-event test above: when the exhaustion happens
        inside a split loop's dispatch call, it is caught and logged — not re-raised.
        """
        fake_rulings = _make_fake_sd_rulings()[:1]  # single ruling

        def always_exhausted(event_data: dict) -> None:
            raise LlmEnrichmentExhaustedError("exhausted")

        with (
            patch("courts.ca.sd_calendar.is_sd_calendar_html", return_value=True),
            patch("courts.ca.sd_calendar._split_rulings", return_value=fake_rulings),
        ):
            event = _make_sd_event()
            from ingestion.worker import _try_sd_calendar_split

            # Must NOT raise — the loop swallows the exhaustion.
            result = _try_sd_calendar_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True


# ---------------------------------------------------------------------------
# AC3: _process_message retry counter not inflated by per-child exhaustion
# ---------------------------------------------------------------------------


class TestPerChildExhaustionDoesNotConsumeMaxRetries:
    """AC3: Per-child exhaustion must not cause _process_message to retry the batch."""

    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_process_message_acked_once_on_per_child_exhaustion(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
    ) -> None:
        """_process_message acks message exactly once when first child exhausts.

        When the first Fresno child's enrichment is exhausted but others succeed,
        the outer _process_message must:
        - NOT increment its retry counter
        - ack the message exactly once
        """
        worker, redis_mock = _make_worker()
        mock_conn, mock_cur = _make_mock_conn()
        mock_psycopg.connect.return_value = mock_conn
        # 2 successful children each need: upsert_court, upsert_case, insert_document
        mock_cur.fetchone.side_effect = [
            ("court-uuid-1",),
            ("case-uuid-1",),
            (True,),
            ("court-uuid-1",),
            ("case-uuid-2",),
            (True,),
        ]

        fake_rulings = _make_fake_fresno_rulings()

        call_count = {"n": 0}
        original_process_event = worker.process_event

        def side_effect(event_data: dict) -> object:
            if event_data.get("_split_processed"):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise LlmEnrichmentExhaustedError("exhausted on first child")
            return original_process_event(event_data)

        worker.process_event = side_effect  # type: ignore[method-assign]

        event = _make_fresno_event()
        msg_id = b"1234567890-0"
        msg_data = {b"data": json.dumps(event).encode()}

        with patch("courts.ca.fresno_tentatives._split_rulings", return_value=fake_rulings):
            worker._process_message(msg_id, msg_data)

        # Message must be acked exactly once (no retry loop).
        redis_mock.xack.assert_called_once()
        # Max retries must not be consumed — attempt counter stays at 0.
        # We confirm this indirectly: if retries were consumed, xack would still
        # be called but the warning log would fire.  The key invariant is one ack.
        assert redis_mock.xack.call_count == 1


# ---------------------------------------------------------------------------
# Non-exhaustion exceptions re-raise (covers the `raise` branches)
# ---------------------------------------------------------------------------


class TestNonExhaustionExceptionsReraise:
    """Non-LlmEnrichmentExhaustedError exceptions must propagate out of split loops."""

    def test_sd_calendar_reraises_non_exhaustion_exception(self) -> None:
        """_try_sd_calendar_split re-raises ValueError (covers worker.py line 246)."""
        fake_rulings = _make_fake_sd_rulings()[:1]

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with (
            patch("courts.ca.sd_calendar.is_sd_calendar_html", return_value=True),
            patch("courts.ca.sd_calendar._split_rulings", return_value=fake_rulings),
        ):
            event = _make_sd_event()
            from ingestion.worker import _try_sd_calendar_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_sd_calendar_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_la_html_reraises_non_exhaustion_exception(self) -> None:
        """_try_la_html_split re-raises ValueError (covers worker.py line 355)."""
        fake_rulings = _make_fake_la_rulings()[:1]

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with (
            patch("courts.ca.la_tentatives.is_la_html", return_value=True),
            patch("courts.ca.la_tentatives._split_rulings", return_value=fake_rulings),
        ):
            event = _make_la_event()
            from ingestion.worker import _try_la_html_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_la_html_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_fresno_pdf_reraises_non_exhaustion_exception(self) -> None:
        """_try_fresno_pdf_split re-raises ValueError (covers worker.py line 468)."""
        fake_rulings = _make_fake_fresno_rulings()  # must be >1 to pass the single-ruling guard

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with patch("courts.ca.fresno_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_fresno_event()
            from ingestion.worker import _try_fresno_pdf_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_fresno_pdf_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_riverside_pdf_reraises_non_exhaustion_exception(self) -> None:
        """_try_riverside_pdf_split re-raises ValueError on non-exhaustion exception (#3649)."""
        fake_rulings = _make_fake_riverside_rulings()  # must be >1 to pass single-ruling guard

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with patch("courts.ca.riverside_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_riverside_event()
            from ingestion.worker import _try_riverside_pdf_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_riverside_pdf_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_riverside_pdf_split_skips_non_riverside_county(self) -> None:
        """_try_riverside_pdf_split returns False (skips) when county != Riverside (#3649)."""
        from ingestion.worker import _try_riverside_pdf_split

        # Fresno event with Riverside-like text — must be skipped because county is wrong.
        event = _make_fresno_event()
        result = _try_riverside_pdf_split(
            event,
            event["document_id"],
            "1.\nCVPS2400001 SMITH vs JONES\nTentative Ruling: Granted.",
            lambda _e: None,
        )
        assert result is False

    def test_riverside_pdf_split_skips_non_pdf_format(self) -> None:
        """_try_riverside_pdf_split returns False (skips) when content_format != pdf (#3649)."""
        from ingestion.worker import _try_riverside_pdf_split

        # Riverside event with html format — must be skipped (gate is pdf-only).
        event = _make_riverside_event(content_format="html")
        result = _try_riverside_pdf_split(
            event,
            event["document_id"],
            "1.\nCVPS2400001\nTentative Ruling: Granted.",
            lambda _e: None,
        )
        assert result is False

    def test_riverside_pdf_split_falls_through_for_no_numbered_entries(self) -> None:
        """_try_riverside_pdf_split returns False when splitter finds no numbered entries.

        This is the "No Tentative Rulings" placeholder path — the LLM
        (which would otherwise be skipped) should run normally to
        populate fields for the placeholder, matching the Fresno
        fall-through behaviour for placeholder PDFs.
        """
        from ingestion.worker import _try_riverside_pdf_split

        event = _make_riverside_event()
        with patch("courts.ca.riverside_tentatives._split_rulings", return_value=[]):
            result = _try_riverside_pdf_split(
                event,
                event["document_id"],
                "No Tentative Rulings March 2, 2026",
                lambda _e: None,
            )
        assert result is False

    def test_riverside_pdf_split_falls_through_for_single_ruling_pdf(self) -> None:
        """_try_riverside_pdf_split returns False when splitter finds exactly 1 entry.

        The deterministic regex extraction does not populate
        motion_type/outcome/case_title.  Falling through to the LLM
        path lets the per-field enrichment fill those fields — there is
        no carry-forward window with a single entry.
        """
        from courts.ca.riverside_tentatives import SplitRuling
        from ingestion.worker import _try_riverside_pdf_split

        single_ruling = [
            SplitRuling(ruling_index=1, case_number="CVPS2400001", ruling_text="Lone ruling text")
        ]
        event = _make_riverside_event()
        with patch("courts.ca.riverside_tentatives._split_rulings", return_value=single_ruling):
            result = _try_riverside_pdf_split(
                event,
                event["document_id"],
                event["ruling_text"],
                lambda _e: None,
            )
        assert result is False

    def test_riverside_pdf_split_dispatches_with_split_metadata(self) -> None:
        """When the splitter finds multiple entries, each dispatched event carries
        the synthetic split metadata that ``IngestionWorker.process_event`` uses
        to short-circuit redundant LLM split attempts and route to per-field
        enrichment (#3649).

        This test pins the dispatch contract: ``_split_processed=True`` (so
        the worker's outer split-attempt path is skipped on the recursive
        call), AND ``_llm_extracted`` is NOT set to True (so per-field LLM
        enrichment STILL runs against each child's ruling_text individually,
        eliminating the cross-entry carry-forward window).
        """
        from ingestion.worker import _try_riverside_pdf_split

        captured: list[dict] = []

        def capture(event_data: dict) -> None:
            captured.append(event_data)

        fake_rulings = _make_fake_riverside_rulings()
        with patch("courts.ca.riverside_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_riverside_event()
            result = _try_riverside_pdf_split(
                event, event["document_id"], event["ruling_text"], capture
            )

        assert result is True
        assert len(captured) == 3
        for idx, child in enumerate(captured):
            assert child["_split_processed"] is True, (
                f"child {idx} missing _split_processed — would re-trigger split path"
            )
            # Critical contract: _llm_extracted must NOT be True so per-field
            # LLM enrichment runs for each child individually.  This is the
            # key behavioural difference from the Fresno splitter, which
            # extracts those fields deterministically.
            assert not child.get("_llm_extracted", False), (
                f"child {idx} has _llm_extracted=True — would skip per-field "
                f"enrichment and lose motion_type/outcome/case_title"
            )
            assert child["_original_document_id"] == event["document_id"]
            # Each child's document_id must be unique (split_ids salt by index).
            assert child["document_id"] != event["document_id"]
            # Each child carries its own ruling text and case_number.
            assert child["case_number"] == fake_rulings[idx].case_number
            assert child["ruling_text"] == fake_rulings[idx].ruling_text

    @patch("ingestion.worker.delete_stale_split_children", return_value=0)
    @patch("ingestion.worker.batch_upsert_parties")
    @patch("ingestion.worker.resolve_judge", return_value="judge-uuid-1")
    @patch("ingestion.worker.psycopg")
    def test_llm_split_reraises_non_exhaustion_exception(
        self,
        mock_psycopg: MagicMock,
        mock_resolve_judge: MagicMock,
        mock_batch_upsert: MagicMock,
        mock_delete_stale: MagicMock,
    ) -> None:
        """_llm_split_document re-raises ValueError on split child (covers worker.py line 3073)."""
        worker, _ = _make_worker()

        fake_rulings = _make_fake_extracted_rulings()[:1]
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = fake_rulings
        worker._get_framework_extractor = lambda: mock_extractor  # type: ignore[method-assign]

        original_process_event = worker.process_event

        def raises_value_error_on_split(event_data: dict) -> object:
            if event_data.get("_split_processed"):
                raise ValueError("non-exhaustion error on split child")
            return original_process_event(event_data)

        worker.process_event = raises_value_error_on_split  # type: ignore[method-assign]

        event = _make_llm_event()
        with pytest.raises(ValueError, match="non-exhaustion error on split child"):
            worker.process_event(event)


# ---------------------------------------------------------------------------
# Helpers used by multiple test classes
# ---------------------------------------------------------------------------


def _make_mock_conn() -> tuple[MagicMock, MagicMock]:
    """Return a (mock_conn, mock_cur) pair for the persistent connection pattern."""
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur
