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
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from framework.llm_enrichment import LlmEnrichmentExhaustedError
from framework.llm_schema import ExtractedRuling, ExtractionOutcome
from ingestion.worker import IngestionWorker


@pytest.fixture(autouse=True)
def _no_split_sibling_move() -> Iterator[None]:
    """The mocked cursors here feed fixed ``fetchone`` sequences; the #4820
    sibling-ruling lookup is covered by ``test_split_shift_move*``."""
    with patch("ingestion.db._move_split_sibling_ruling", return_value=False):
        yield


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


def _make_sf_event(**overrides: object) -> dict:
    """Return a SF family-law PDF-like event payload (#4304)."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000006",
        "scraper_id": "ca-sf-tentatives-family-law",
        "state": "CA",
        "county": "San Francisco",
        "court": "Superior Court",
        "source_url": "https://webapps.sftc.org/ufctr/403_Tentative_Rulings.pdf",
        "content_format": "pdf",
        "content_hash": "pqr678",
        "s3_key": "ca/san_francisco/superior_court/raw/pqr678.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": (
            "SUPERIOR COURT OF CALIFORNIA\n"
            "COUNTY OF SAN FRANCISCO\n"
            "Case Number: FPT-25-378624\n"
            "Department: 403\n"
            "TENTATIVE RULING\n"
        ),
        "hearing_date": "2026-03-03",
        "capture_timestamp": "2026-03-03T14:00:00",
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


def _make_fake_sf_rulings() -> list:
    """Return 3 fake SF family-law SplitRuling objects for testing (#4304)."""
    from courts.ca.sf_tentatives import SplitRuling

    return [
        SplitRuling(
            ruling_index=i,
            case_number=f"FDI-25-80000{i}",
            ruling_text=(
                f"SUPERIOR COURT OF CALIFORNIA\n"
                f"COUNTY OF SAN FRANCISCO\n"
                f"Case Number: FDI-25-80000{i}\n"
                f"Department: 403\n"
                f"TENTATIVE RULING\n"
                f"SF ruling {i} body."
            ),
            department="403",
        )
        for i in range(3)
    ]


def _make_sc_event(**overrides: object) -> dict:
    """Return a Santa Clara PDF-like event payload (#4303)."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000000007",
        "scraper_id": "ca-sc-tentatives-civil",
        "state": "CA",
        "county": "Santa Clara",
        "court": "Superior Court",
        "source_url": "https://santaclara.courts.ca.gov/tentativerulings/7",
        "content_format": "pdf",
        "content_hash": "stu678",
        "s3_key": "ca/santa_clara/superior_court/raw/stu678.pdf",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": (
            "Line 1\n"
            "Case Name: Foo v. Bar\n"
            "Case No.: 25CV111111\n"
            "The motion is GRANTED.  Body text long enough to clear the "
            "minimum-entry threshold.\n"
            "Line 2\n"
            "Case Name: Alpha v. Beta\n"
            "Case No.: 25CV222222\n"
            "The demurrer is overruled.  Body text long enough to clear "
            "the minimum-entry threshold.\n"
        ),
        "hearing_date": "2026-03-04",
        "capture_timestamp": "2026-03-03T23:00:00",
    }
    base.update(overrides)
    return base


def _make_fake_sc_rulings() -> list:
    """Return 3 fake Santa Clara SplitRuling objects for testing (#4303)."""
    from courts.ca.sc_tentatives import SplitRuling

    return [
        SplitRuling(
            ruling_index=i + 1,
            case_number=f"25CV{i:06d}",
            case_title=f"Plaintiff{i} v. Defendant{i}",
            ruling_text=f"Santa Clara ruling {i}\nThe motion is granted.",
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


class TestDispatchSplitChildProtectsFailedSlot:
    """#4878: every split path dispatches its children through
    ``_dispatch_split_child``, which protects the slot of a child whose
    per-child enrichment is exhausted so the split-set write keeps it."""

    def _batch(self) -> object:
        from ingestion.worker import _SplitBatch

        return _SplitBatch(parent_document_id="parent")

    def test_exhausted_child_is_kept_and_reraised(self) -> None:
        worker, _ = _make_worker()
        batch = self._batch()
        worker.process_event = MagicMock(  # type: ignore[method-assign]
            side_effect=LlmEnrichmentExhaustedError("exhausted")
        )
        with pytest.raises(LlmEnrichmentExhaustedError):
            worker._dispatch_split_child(batch, {"document_id": "child-1"})
        assert batch.keep_ids == {"child-1"}
        assert batch.replace is True

    def test_other_errors_do_not_protect_the_slot(self) -> None:
        worker, _ = _make_worker()
        batch = self._batch()
        worker.process_event = MagicMock(side_effect=ValueError("bad"))  # type: ignore[method-assign]
        with pytest.raises(ValueError):
            worker._dispatch_split_child(batch, {"document_id": "child-1"})
        assert batch.keep_ids == set()

    def test_successful_child_is_not_protected(self) -> None:
        worker, _ = _make_worker()
        batch = self._batch()
        worker.process_event = MagicMock(return_value=None)  # type: ignore[method-assign]
        worker._dispatch_split_child(batch, {"document_id": "child-1"})
        assert batch.keep_ids == set()
        assert batch.replace is True


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
        with patch.object(IngestionWorker, "_write_split_batch", autospec=True) as mock_write:
            worker.process_event(event)

        # All 3 split children were dispatched (first raised, but loop continued).
        split_calls = [c for c in call_log if c.get("_split_processed")]
        assert len(split_calls) == 3, f"Expected 3 split dispatch calls, got {len(split_calls)}"
        # Children 2 and 3 are written as the split set (#4845); child 1's
        # slot is protected, so its existing rows survive the transient failure.
        mock_write.assert_called_once()
        batch = mock_write.call_args.args[3]
        ids = [c["document_id"] for c in split_calls]
        assert [w.document_id for w in batch.children] == ids[1:]
        assert batch.keep_ids == {ids[0]}


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

    def test_santa_clara_pdf_reraises_non_exhaustion_exception(self) -> None:
        """_try_sc_pdf_split re-raises ValueError on non-exhaustion exception (#4303)."""
        fake_rulings = _make_fake_sc_rulings()  # >1 to pass single-ruling guard

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with patch("courts.ca.sc_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_sc_event()
            from ingestion.worker import _try_sc_pdf_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_sc_pdf_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_santa_clara_pdf_split_skips_non_sc_county(self) -> None:
        """_try_sc_pdf_split returns False (skips) when county != Santa Clara (#4303)."""
        from ingestion.worker import _try_sc_pdf_split

        # Riverside event with SC-like text — must be skipped because county is wrong.
        event = _make_riverside_event()
        result = _try_sc_pdf_split(
            event,
            event["document_id"],
            "Line 1\nCase No.: 25CV111111\nThe motion is granted.",
            lambda _e: None,
        )
        assert result is False

    def test_santa_clara_pdf_split_skips_non_pdf_format(self) -> None:
        """_try_sc_pdf_split returns False (skips) when content_format != pdf (#4303)."""
        from ingestion.worker import _try_sc_pdf_split

        # SC event with html format — must be skipped (gate is pdf-only).
        event = _make_sc_event(content_format="html")
        result = _try_sc_pdf_split(
            event,
            event["document_id"],
            "Line 1\nCase No.: 25CV111111\nThe motion is granted.",
            lambda _e: None,
        )
        assert result is False

    def test_santa_clara_pdf_split_falls_through_for_no_numbered_entries(self) -> None:
        """_try_sc_pdf_split returns False when splitter finds no numbered entries.

        This is the compact-summary-table path (e.g. dept 6) — the LLM
        path runs normally so the existing prompt's anti-carry-forward
        rule remains the only protection until a future PR extends the
        splitter to compact tables.
        """
        from ingestion.worker import _try_sc_pdf_split

        event = _make_sc_event()
        with patch("courts.ca.sc_tentatives._split_rulings", return_value=[]):
            result = _try_sc_pdf_split(
                event,
                event["document_id"],
                "Some compact summary table without Line N markers",
                lambda _e: None,
            )
        assert result is False

    def test_santa_clara_pdf_split_falls_through_for_single_ruling_pdf(self) -> None:
        """_try_sc_pdf_split returns False when splitter finds exactly 1 entry.

        The deterministic regex extraction does not populate
        motion_type/outcome.  Falling through to the LLM path lets the
        per-field enrichment fill those fields — there is no carry-
        forward window with a single entry (matches Riverside #3649).
        """
        from courts.ca.sc_tentatives import SplitRuling
        from ingestion.worker import _try_sc_pdf_split

        single_ruling = [
            SplitRuling(
                ruling_index=1,
                case_number="25CV111111",
                case_title="Foo v. Bar",
                ruling_text="Lone ruling text",
            )
        ]
        event = _make_sc_event()
        with patch("courts.ca.sc_tentatives._split_rulings", return_value=single_ruling):
            result = _try_sc_pdf_split(
                event,
                event["document_id"],
                event["ruling_text"],
                lambda _e: None,
            )
        assert result is False

    def test_santa_clara_pdf_split_dispatches_with_split_metadata(self) -> None:
        """When the splitter finds multiple entries, each dispatched event carries
        the synthetic split metadata that ``IngestionWorker.process_event`` uses
        to short-circuit redundant LLM split attempts and route to per-field
        enrichment (#4303 — mirrors #3649 contract).

        This test pins the dispatch contract: ``_split_processed=True`` (so
        the worker's outer split-attempt path is skipped on the recursive
        call), AND ``_llm_extracted`` is NOT set to True (so per-field LLM
        enrichment STILL runs against each child's ruling_text individually,
        eliminating the cross-entry carry-forward window).
        """
        from ingestion.worker import _try_sc_pdf_split

        captured: list[dict] = []

        def capture(event_data: dict) -> None:
            captured.append(event_data)

        fake_rulings = _make_fake_sc_rulings()
        with patch("courts.ca.sc_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_sc_event()
            result = _try_sc_pdf_split(event, event["document_id"], event["ruling_text"], capture)

        assert result is True
        assert len(captured) == 3
        for idx, child in enumerate(captured):
            assert child["_split_processed"] is True, (
                f"child {idx} missing _split_processed — would re-trigger split path"
            )
            # Critical contract: _llm_extracted must NOT be True so per-field
            # LLM enrichment runs for each child individually.  Mirrors the
            # Riverside splitter's behavior — the SC splitter does not
            # populate motion_type/outcome deterministically.
            assert not child.get("_llm_extracted", False), (
                f"child {idx} has _llm_extracted=True — would skip per-field "
                f"enrichment and lose motion_type/outcome"
            )
            assert child["_original_document_id"] == event["document_id"]
            # Each child's document_id must be unique (split_ids salt by index).
            assert child["document_id"] != event["document_id"]
            # Each child carries its own ruling text, case_number, and case_title.
            assert child["case_number"] == fake_rulings[idx].case_number
            assert child["case_title"] == fake_rulings[idx].case_title
            assert child["ruling_text"] == fake_rulings[idx].ruling_text


def test_sc_split_child_without_case_number_does_not_inherit_doc_case_number() -> None:
    """#4726: a live SC event carries the PDF's FIRST case number (the
    scraper's ``parse_all_case_numbers(text)[0]``).  A split child with no
    case number of its own must stay without one — inheriting the doc-level
    number would file another case's ruling under it.  Same for the title."""
    from courts.ca.sc_tentatives import SplitRuling
    from ingestion.worker import _try_sc_pdf_split

    rulings = [
        SplitRuling(1, "25CV111111", "first ruling", "Foo v. Bar"),
        SplitRuling(2, None, "second ruling, case number not printed", None),
    ]
    event = _make_sc_event(case_number="25CV111111", case_title="Foo v. Bar")
    captured: list[dict] = []
    with patch("courts.ca.sc_tentatives._split_rulings", return_value=rulings):
        assert _try_sc_pdf_split(event, event["document_id"], event["ruling_text"], captured.append)
    assert [c["case_number"] for c in captured] == ["25CV111111", None]
    assert [c["case_title"] for c in captured] == ["Foo v. Bar", None]
    # #4866: only the child whose number the splitter read is marked.
    assert [c.get("_case_number_from_source") for c in captured] == [True, False]


def _two_rulings(module: str) -> list:
    """A splitter result whose second entry has no case number of its own."""
    if module == "sd_calendar":
        from courts.ca.sd_calendar import SDSplitRuling as Cls
    elif module == "la_tentatives":
        from courts.ca.la_tentatives import LASplitRuling as Cls
    elif module == "fresno_tentatives":
        from courts.ca.fresno_tentatives import SplitRuling as Cls
    elif module == "riverside_tentatives":
        from courts.ca.riverside_tentatives import SplitRuling as Cls
    else:
        from courts.ca.sf_tentatives import SplitRuling as Cls
    extra: dict = {}
    if module == "fresno_tentatives":
        extra = {"motion_type": None, "outcome": None, "hearing_date": None}
    return [
        Cls(
            ruling_index=0,
            case_number="FIRST-1",
            ruling_text="first ruling",
            case_title="A v. B",
            **extra,
        ),
        Cls(
            ruling_index=1, case_number=None, ruling_text="second ruling", case_title=None, **extra
        ),
    ]


_DETERMINISTIC_SPLITTERS = [
    ("_try_sd_calendar_split", "sd_calendar", "is_sd_calendar_html", _make_sd_event),
    ("_try_la_html_split", "la_tentatives", "is_la_html", _make_la_event),
    ("_try_fresno_pdf_split", "fresno_tentatives", None, _make_fresno_event),
    ("_try_riverside_pdf_split", "riverside_tentatives", None, _make_riverside_event),
    ("_try_sf_pdf_split", "sf_tentatives", None, _make_sf_event),
]


@pytest.mark.parametrize(("fn_name", "module", "detector", "make_event"), _DETERMINISTIC_SPLITTERS)
def test_case_number_from_source_split_child_does_not_inherit_doc_case(
    fn_name: str, module: str, detector: str | None, make_event: object
) -> None:
    """#4868: a live event carries the document's FIRST case number (and,
    for SF, its first caption).  A child of a multi-case split without a
    case number of its own stays without one instead of being filed under
    the first case; only a number the splitter read is marked as from the
    source (#4866)."""
    import ingestion.worker as worker_mod

    event = make_event(case_number="FIRST-1", case_title="A v. B")  # type: ignore[operator]
    captured: list[dict] = []
    with ExitStack() as stack:
        stack.enter_context(
            patch(f"courts.ca.{module}._split_rulings", return_value=_two_rulings(module))
        )
        if detector:
            stack.enter_context(patch(f"courts.ca.{module}.{detector}", return_value=True))
        split = getattr(worker_mod, fn_name)
        assert split(event, event["document_id"], event["ruling_text"], captured.append)
    assert [c["case_number"] for c in captured] == ["FIRST-1", None]
    assert [c["case_title"] for c in captured] == ["A v. B", None]
    assert [c["_case_number_from_source"] for c in captured] == [True, False]


@pytest.mark.parametrize(
    ("fn_name", "module", "detector", "make_event"), _DETERMINISTIC_SPLITTERS[:2]
)
def test_case_number_from_source_single_split_keeps_event_case(
    fn_name: str, module: str, detector: str | None, make_event: object
) -> None:
    """A split into one ruling is the document itself: with no number of its
    own it keeps the event's, unmarked (the splitter did not read it)."""
    import ingestion.worker as worker_mod

    event = make_event(case_number="ONLY-1", case_title="C v. D")  # type: ignore[operator]
    captured: list[dict] = []
    with (
        patch(f"courts.ca.{module}._split_rulings", return_value=_two_rulings(module)[1:]),
        patch(f"courts.ca.{module}.{detector}", return_value=True),
    ):
        split = getattr(worker_mod, fn_name)
        assert split(event, event["document_id"], event["ruling_text"], captured.append)
    assert [(c["case_number"], c["case_title"]) for c in captured] == [("ONLY-1", "C v. D")]
    assert captured[0]["_case_number_from_source"] is False


@patch("ingestion.worker.psycopg")
def test_llm_split_child_without_case_number_does_not_inherit_doc_case(
    mock_psycopg: MagicMock,
) -> None:
    """#4868: the LLM split path has the same rule.  A multi-ruling child the
    LLM returned without a case number does not take the event's (the
    document's first case); an LLM-read number is never marked as from the
    source, so the fuzzy correction still applies to it."""
    worker, _ = _make_worker()
    mock_conn, _cur = _make_mock_conn()
    mock_psycopg.connect.return_value = mock_conn
    rulings = _make_fake_extracted_rulings()
    rulings[1].extracted_case_number = None
    rulings[1].extracted_case_title = None
    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = rulings
    worker._get_framework_extractor = lambda: mock_extractor  # type: ignore[method-assign]

    captured: list[dict] = []
    original = worker.process_event

    def capture(event_data: dict) -> None:
        if event_data.get("_split_processed"):
            captured.append(event_data)
            return
        original(event_data)

    worker.process_event = capture  # type: ignore[method-assign]
    event = _make_llm_event(case_number="2024-00000", case_title="LLM Case 0")
    with patch.object(IngestionWorker, "_write_split_batch", autospec=True):
        worker.process_event(event)
    assert [c["case_number"] for c in captured] == ["2024-00000", None, "2024-00002"]
    assert [c["case_title"] for c in captured] == ["LLM Case 0", None, "LLM Case 2"]
    assert [c["_case_number_from_source"] for c in captured] == [False, False, False]


class TestScPdfSplitDept12:
    """#4681: a real Dept 12 PDF is split deterministically — one child per
    case with a real case number — instead of falling through to the LLM
    split that produced ``UNKNOWN-`` case numbers."""

    def test_dept12_split_dispatches_one_child_per_case(self) -> None:
        from courts.ca.sc_tentatives import extract_pdf_text
        from ingestion.worker import _try_sc_pdf_split

        pdf_bytes = (Path(__file__).parent / "fixtures" / "sc_dept12_table_4681_c.pdf").read_bytes()
        event = _make_sc_event(
            ruling_text=extract_pdf_text(pdf_bytes),
            hearing_date=None,
            capture_timestamp="2026-09-22T18:47:00+00:00",
        )
        captured: list[dict] = []
        assert _try_sc_pdf_split(
            event,
            event["document_id"],
            event["ruling_text"],
            captured.append,
            raw_pdf_bytes=pdf_bytes,
        )
        case_numbers = [c["case_number"] for c in captured]
        assert len(case_numbers) == 9
        assert not any(cn.startswith("UNKNOWN") for cn in case_numbers)
        assert all(c["hearing_date"] == "2026-09-23" for c in captured)

    def test_table_split_dispatches_dept16_children_with_case_numbers(self) -> None:
        """#4696: a Dept 16 table-layout PDF that format A split into
        entries with no case number now dispatches one child per case."""
        from courts.ca.sc_tentatives import extract_pdf_text
        from ingestion.worker import _try_sc_pdf_split

        pdf_bytes = (Path(__file__).parent / "fixtures" / "sc_table_4696_dept16.pdf").read_bytes()
        event = _make_sc_event(ruling_text=extract_pdf_text(pdf_bytes))
        captured: list[dict] = []
        assert _try_sc_pdf_split(
            event,
            event["document_id"],
            event["ruling_text"],
            captured.append,
            raw_pdf_bytes=pdf_bytes,
        )
        case_numbers = [c["case_number"] for c in captured]
        assert len(case_numbers) == 10
        assert all(cn and not cn.startswith("UNKNOWN") for cn in case_numbers)


class TestScPdfSplitHeaderHearingDate:
    """#4667: prefix-mode reingest / rebuild events carry no hearing_date, so
    the SC split must derive it from the PDF header rather than leaving each
    child to guess from its own body text (which yielded dates of birth and
    discovery service dates)."""

    _HEADER = (
        "SUPERIOR COURT, STATE OF CALIFORNIA\n"
        "COUNTY OF SANTA CLARA\n"
        "Department 12\n"
        "DATE: 09/23/2026 TIME: 9:00 A.M. and 9:01 A.M.\n"
    )

    def _run(self, **overrides: object) -> list[dict]:
        from ingestion.worker import _try_sc_pdf_split

        captured: list[dict] = []
        event = _make_sc_event(**overrides)
        with patch("courts.ca.sc_tentatives._split_rulings", return_value=_make_fake_sc_rulings()):
            assert _try_sc_pdf_split(
                event, event["document_id"], event["ruling_text"], captured.append
            )
        return captured

    def test_issue_4667_split_children_get_header_date_when_event_has_none(self) -> None:
        children = self._run(
            hearing_date=None,
            capture_timestamp="2026-09-24T01:43:00+00:00",
            ruling_text=self._HEADER + "claimant's date of birth (September 13, 1972).\n",
        )
        assert [c["hearing_date"] for c in children] == ["2026-09-23"] * 3

    def test_issue_4667_split_header_date_year_typo_corrected(self) -> None:
        children = self._run(
            hearing_date=None,
            capture_timestamp="2026-09-15T01:43:00Z",
            ruling_text="Department 16\nDATE: 9/16/2025 TIME: 9:00 A.M.\nbody\n",
        )
        assert [c["hearing_date"] for c in children] == ["2026-09-16"] * 3

    def test_issue_4667_scraper_hearing_date_still_wins(self) -> None:
        children = self._run(hearing_date="2026-09-22", ruling_text=self._HEADER)
        assert [c["hearing_date"] for c in children] == ["2026-09-22"] * 3

    def _process_event_split_input(self, **overrides: object) -> dict:
        """Run ``process_event`` and return the event handed to the split
        dispatcher (``_llm_split_document``), which fans out to both the
        deterministic SC splitter and the LLM splitter."""
        worker, _ = _make_worker()
        seen: list[dict] = []

        def fake_split(event_data: dict, *_a: object, **_k: object) -> bool:
            seen.append(event_data)
            return True

        worker._llm_split_document = fake_split  # type: ignore[method-assign]
        worker.process_event(_make_sc_event(s3_key=None, **overrides))
        assert len(seen) == 1
        return seen[0]

    def test_issue_4667_process_event_sets_header_date_before_llm_split(self) -> None:
        """Most Dept 12 PDFs don't match the deterministic splitter and go
        through the LLM split — the header date must already be on the event."""
        event = self._process_event_split_input(
            hearing_date=None,
            capture_timestamp="2026-09-24T01:43:00Z",
            ruling_text=self._HEADER + "claimant's date of birth (September 13, 1972).\n",
        )
        assert event["hearing_date"] == "2026-09-23"

    def test_issue_4667_process_event_keeps_scraper_date(self) -> None:
        event = self._process_event_split_input(
            hearing_date="2026-09-22", ruling_text=self._HEADER + "body\n"
        )
        assert event["hearing_date"] == "2026-09-22"

    def test_issue_4667_header_hook_gates_format(self) -> None:
        from ingestion.raw_hearing_date import raw_hearing_date

        text = self._HEADER
        assert raw_hearing_date(_make_sc_event(content_format="html"), text) is None
        assert raw_hearing_date(_make_sc_event(), "") is None
        assert raw_hearing_date(_make_sc_event(), text) == "2026-09-23"
        prefix = _make_sc_event(scraper_id="reingest-ca-santa_clara", source_url="")
        assert raw_hearing_date(prefix, text) == "2026-09-23"

    def test_issue_4667_no_header_date_leaves_none(self) -> None:
        children = self._run(
            hearing_date=None,
            capture_timestamp=None,
            ruling_text="Department 12\nThe hearing on August 27, 2026.\n",
        )
        assert [c["hearing_date"] for c in children] == [None] * 3


def _make_cc_prefix_event(**overrides: object) -> dict:
    """Return a Contra Costa legacy-PDF event as prefix-mode reingest /
    rebuild_db build it: no source_url, no hearing_date (#4769)."""
    base: dict = {
        "document_id": "aaaaaaaa-0000-0000-0000-000000004769",
        "scraper_id": "reingest-ca-contra_costa",
        "state": "CA",
        "county": "Contra Costa",
        "court": "Superior Court",
        "source_url": "",
        "content_format": "pdf",
        "content_hash": "cc4769",
        "s3_key": None,
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "",
        "hearing_date": None,
        "capture_timestamp": "2026-04-09T13:16:10+00:00",
    }
    base.update(overrides)
    return base


class TestCcHeaderHearingDate:
    """#4769: Contra Costa events from prefix-mode reingest / rebuild_db carry
    no hearing_date and no source_url (so no filename date).  The live scraper
    hands the LLM the header date as authoritative metadata; without it, the
    rebuilt split children were left with NULL or body-text dates (138 dev
    rulings).  The worker must derive the date from the ``HEARING DATE:``
    header before the split, and never fall back to a body date."""

    _HEADER = (
        "SUPERIOR COURT OF CALIFORNIA, CONTRA COSTA COUNTY\n"
        "MARTINEZ, CA\n"
        "DEPARTMENT 10\n"
        "JUDICIAL OFFICER: JULIA CAMPINS\n"
        "HEARING DATE: 04/09/2026\n"
    )
    _BODY = "The hearing is continued to May 4, 2026 at 9:00 a.m.\n"

    def _split_input(self, **overrides: object) -> dict:
        worker, _ = _make_worker()
        seen: list[dict] = []

        def fake_split(event_data: dict, *_a: object, **_k: object) -> bool:
            seen.append(event_data)
            return True

        worker._llm_split_document = fake_split  # type: ignore[method-assign]
        worker.process_event(_make_cc_prefix_event(**overrides))
        assert len(seen) == 1
        return seen[0]

    def test_issue_4769_process_event_sets_header_date_before_llm_split(self) -> None:
        event = self._split_input(ruling_text=self._HEADER + self._BODY)
        assert event["hearing_date"] == "2026-04-09"

    def test_issue_4769_unpadded_header_date(self) -> None:
        event = self._split_input(
            ruling_text=self._HEADER.replace("04/09/2026", "3/5/2026") + self._BODY
        )
        assert event["hearing_date"] == "2026-03-05"

    def test_issue_4769_probate_calendar_header(self) -> None:
        event = self._split_input(
            ruling_text="COURT CALENDAR FOR MARCH 16, 2026\nN25-2307 IN THE MATTER OF: X\n"
        )
        assert event["hearing_date"] == "2026-03-16"

    def test_issue_4769_scraper_hearing_date_still_wins(self) -> None:
        event = self._split_input(hearing_date="2026-04-10", ruling_text=self._HEADER)
        assert event["hearing_date"] == "2026-04-10"

    def test_issue_4769_body_date_not_hearing_date(self) -> None:
        event = self._split_input(ruling_text="DEPARTMENT 10\n" + self._BODY)
        assert event["hearing_date"] is None

    def test_issue_4769_header_hook_gates_county_and_format(self) -> None:
        from ingestion.raw_hearing_date import raw_hearing_date

        text = self._HEADER
        assert raw_hearing_date(_make_cc_prefix_event(county="Nowhere"), text) is None
        assert raw_hearing_date(_make_cc_prefix_event(content_format="txt"), text) is None
        assert raw_hearing_date(_make_cc_prefix_event(), "") is None
        assert raw_hearing_date(_make_cc_prefix_event(), text) == "2026-04-09"


# ---------------------------------------------------------------------------
# San Francisco family-law PDF splitter — _try_sf_pdf_split (#4304)
# ---------------------------------------------------------------------------


class TestSfPdfSplit:
    """Verify the SF family-law PDF deterministic split path (#4304).

    These tests pin the same contract as the Riverside splitter (#3649):

    1. The county+format gate skips non-SF events and non-PDF SF events.
    2. Empty splitter results (no entry headers) and single-entry results
       fall through to the LLM path so per-field enrichment can fill
       motion_type/outcome/case_title.
    3. Multi-entry splitter results dispatch synthetic split events with
       ``_split_processed=True`` and **no** ``_llm_extracted`` flag, so
       per-field LLM enrichment runs against each entry individually
       (eliminating the cross-entry carry-forward window that the
       SAN_FRANCISCO_SYSTEM_PROMPT was failing to enforce on its own).
    4. Non-exhaustion exceptions in the dispatch callable propagate; only
       ``LlmEnrichmentExhaustedError`` is swallowed (with a CRITICAL log).
    """

    def test_sf_pdf_split_skips_non_sf_county(self) -> None:
        """_try_sf_pdf_split returns False (skips) when county != San Francisco."""
        from ingestion.worker import _try_sf_pdf_split

        # Riverside event with SF-shaped text — must be skipped because county is wrong.
        event = _make_riverside_event()
        result = _try_sf_pdf_split(
            event,
            event["document_id"],
            "SUPERIOR COURT OF CALIFORNIA\nCOUNTY OF SAN FRANCISCO\n"
            "Case Number: FPT-25-378624\nTENTATIVE RULING\n",
            lambda _e: None,
        )
        assert result is False

    def test_sf_pdf_split_skips_non_pdf_format(self) -> None:
        """_try_sf_pdf_split returns False (skips) when content_format != pdf.

        SF civil tentatives (``ca-sf-tentatives-civil``) are HTML and create
        one CapturedDocument per ruling at capture time — they never funnel
        through this path.  The gate is defense in depth.
        """
        from ingestion.worker import _try_sf_pdf_split

        event = _make_sf_event(content_format="html")
        result = _try_sf_pdf_split(
            event,
            event["document_id"],
            "SUPERIOR COURT OF CALIFORNIA\nCOUNTY OF SAN FRANCISCO\n"
            "Case Number: FPT-25-378624\nTENTATIVE RULING\n",
            lambda _e: None,
        )
        assert result is False

    def test_sf_pdf_split_falls_through_for_no_entry_headers(self) -> None:
        """_try_sf_pdf_split returns False when splitter finds no entry headers.

        Lets the LLM path run for malformed PDFs / non-multi-case shapes.
        """
        from ingestion.worker import _try_sf_pdf_split

        event = _make_sf_event()
        with patch("courts.ca.sf_tentatives._split_rulings", return_value=[]):
            result = _try_sf_pdf_split(
                event,
                event["document_id"],
                "Some text without entry headers",
                lambda _e: None,
            )
        assert result is False

    def test_sf_pdf_split_falls_through_for_single_ruling_pdf(self) -> None:
        """_try_sf_pdf_split returns False when splitter finds exactly 1 entry.

        The deterministic regex extraction does not populate
        motion_type/outcome/case_title.  Falling through to the LLM path
        lets the per-field enrichment fill those fields — and there is no
        carry-forward window with a single entry, so the LLM is safe.
        """
        from courts.ca.sf_tentatives import SplitRuling
        from ingestion.worker import _try_sf_pdf_split

        single_ruling = [
            SplitRuling(
                ruling_index=0,
                case_number="FPT-25-378624",
                ruling_text="Lone ruling text",
                department="403",
            )
        ]
        event = _make_sf_event()
        with patch("courts.ca.sf_tentatives._split_rulings", return_value=single_ruling):
            result = _try_sf_pdf_split(
                event,
                event["document_id"],
                event["ruling_text"],
                lambda _e: None,
            )
        assert result is False

    def test_sf_pdf_split_dispatches_with_split_metadata(self) -> None:
        """When the splitter finds multiple entries, each dispatched event carries
        the synthetic split metadata that ``IngestionWorker.process_event`` uses
        to short-circuit redundant LLM split attempts and route to per-field
        enrichment (#4304).

        This pins the dispatch contract: ``_split_processed=True`` (so
        the worker's outer split-attempt path is skipped on the recursive
        call), AND ``_llm_extracted`` is NOT set to True (so per-field LLM
        enrichment STILL runs against each child's ruling_text individually,
        eliminating the cross-entry carry-forward window).
        """
        from ingestion.worker import _try_sf_pdf_split

        captured: list[dict] = []

        def capture(event_data: dict) -> None:
            captured.append(event_data)

        fake_rulings = _make_fake_sf_rulings()
        with patch("courts.ca.sf_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_sf_event()
            result = _try_sf_pdf_split(event, event["document_id"], event["ruling_text"], capture)

        assert result is True
        assert len(captured) == 3
        for idx, child in enumerate(captured):
            assert child["_split_processed"] is True, (
                f"child {idx} missing _split_processed — would re-trigger split path"
            )
            # Critical contract: _llm_extracted must NOT be True so per-field
            # LLM enrichment runs for each child individually.  This is the
            # behavioural lock that prevents cross-entry carry-forward.
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
            # Each child carries its own department from the splitter.
            assert child["department"] == fake_rulings[idx].department

    def test_sf_pdf_split_reraises_non_exhaustion_exception(self) -> None:
        """_try_sf_pdf_split re-raises ValueError on non-exhaustion exception (#4304)."""
        fake_rulings = _make_fake_sf_rulings()  # must be >1 to pass single-ruling guard

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with patch("courts.ca.sf_tentatives._split_rulings", return_value=fake_rulings):
            event = _make_sf_event()
            from ingestion.worker import _try_sf_pdf_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_sf_pdf_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_sf_pdf_split_all_child_exhaustion_logs_and_succeeds(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SF PDF: all children exhausted → logged as critical, returns True (no re-raise)."""
        import logging

        fake_rulings = _make_fake_sf_rulings()

        def always_exhausted(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                raise LlmEnrichmentExhaustedError("all exhausted")

        with (
            patch("courts.ca.sf_tentatives._split_rulings", return_value=fake_rulings),
            caplog.at_level(logging.CRITICAL, logger="ingestion.worker"),
        ):
            event = _make_sf_event()
            from ingestion.worker import _try_sf_pdf_split

            result = _try_sf_pdf_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True
        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) == 3
        for record in critical_msgs:
            assert "per-child enrichment exhausted" in record.getMessage()


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


# ---------------------------------------------------------------------------
# #4907: an LLM-read case number must appear in the text the LLM read
# ---------------------------------------------------------------------------


def _sc_case_less_child(ruling_text: str) -> dict:
    """A Santa Clara split child the deterministic splitter left case-less."""
    return _make_sc_event(
        document_id="aaaaaaaa-0000-0000-0000-000000004907",
        ruling_text=ruling_text,
        case_number=None,
        case_title="Juan Allca-Taipe vs. Lyft",
        _split_processed=True,
        _original_document_id="aaaaaaaa-0000-0000-0000-000000000007",
        _split_index=1,
        _split_count=2,
        _case_number_from_source=False,
    )


def _process_with_llm_case_number(event: dict, llm_case_number: str) -> list:
    """Run *event* through ``process_event`` with the per-field LLM returning
    *llm_case_number*; return the ``_RulingWrite`` objects it would write."""
    from ingestion.llm_extract import LLMExtractionResult, LLMRulingResult

    worker, _ = _make_worker()
    worker._llm_client = MagicMock()
    mock_conn, _cur = _make_mock_conn()
    writes: list = []
    llm_result = LLMExtractionResult(
        case_count=1,
        rulings=[LLMRulingResult(case_number=llm_case_number, case_title="Allca-Taipe v. Lyft")],
    )
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.extract_fields_llm", return_value=llm_result),
        patch.object(
            IngestionWorker,
            "_write_one_ruling",
            autospec=True,
            side_effect=lambda _self, write: writes.append(write),
        ),
    ):
        mock_psycopg.connect.return_value = mock_conn
        worker.process_event(event)
    return writes


def test_llm_case_number_not_in_child_text_is_stored_as_unknown() -> None:
    """#4907: the per-field LLM invented ``23STCV01234`` for a Santa Clara
    child whose whole text was ``See below.``.  A number absent from the
    child's own text is discarded and the child keeps ``UNKNOWN-``."""
    event = _sc_case_less_child("See below.")
    writes = _process_with_llm_case_number(event, "23STCV01234")

    assert len(writes) == 1
    # No number: the write files it under ``UNKNOWN-<document_id>``.
    assert writes[0].case_number is None
    assert "case_number" not in writes[0].extraction_methods


def test_llm_case_number_in_child_text_is_kept() -> None:
    """A number the child's text carries, even formatted differently, is
    kept as the LLM read it."""
    event = _sc_case_less_child(
        "Case 24 CV 412345: the demurrer is OVERRULED.  Defendant shall answer within 10 days."
    )
    writes = _process_with_llm_case_number(event, "24CV412345")

    assert len(writes) == 1
    assert writes[0].case_number == "24CV412345"
    assert writes[0].extraction_methods["case_number"] == "llm"


_SPLIT_CHILD_TEXTS = [
    "Motion GRANTED for the moving party in full.",
    "Motion DENIED; the opposition is well taken and persuasive.",
    "Motion CONTINUED to allow supplemental briefing on the merits by both sides.",
]


def _llm_split_writes(
    document_text: str,
    case_numbers: list[str | None],
    texts: list[str] | None = None,
    **event_overrides: object,
) -> list:
    """Run the LLM split path on *document_text* with the extractor
    returning one ruling per entry of *case_numbers* (child text *texts*,
    the LLM's transcription); return the ``_RulingWrite`` objects of the
    split batch, after each child went through ``process_event`` and its
    case-number gate."""
    worker, _ = _make_worker()
    mock_conn, _cur = _make_mock_conn()
    texts = texts or _SPLIT_CHILD_TEXTS
    rulings = [
        ExtractedRuling(
            extracted_case_number=cn,
            extracted_case_title=f"Party{i} v. Other{i}",
            ruling_text=texts[i],
            outcome=ExtractionOutcome.GRANTED,
        )
        for i, cn in enumerate(case_numbers)
    ]
    mock_extractor = MagicMock()
    mock_extractor.extract.return_value = rulings
    worker._get_framework_extractor = lambda: mock_extractor  # type: ignore[method-assign]
    batches: list = []
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch.object(
            IngestionWorker,
            "_write_split_batch",
            autospec=True,
            side_effect=lambda _self, _event, _parent, batch: batches.append(batch),
        ),
    ):
        mock_psycopg.connect.return_value = mock_conn
        worker.process_event(_make_llm_event(ruling_text=document_text, **event_overrides))
    assert len(batches) == 1
    return sorted(batches[0].children, key=lambda w: w.event_data["_split_index"])


_OC_DOCUMENT_TEXT = (
    "SUPERIOR COURT OF CALIFORNIA, COUNTY OF ORANGE — TENTATIVE RULINGS\n"
    "1  Smith v. Jones            2024-01393434   Motion GRANTED for the moving party.\n"
    "2  Doe v. Roe                2023- 01329371  Motion DENIED; the opposition is well taken.\n"
    "6  Cheu v. RED SUN, LLC                      Motion CONTINUED for supplemental briefing.\n"
)


def test_llm_split_drops_case_number_absent_from_document_text() -> None:
    """#4907: the multimodal prompt's example number ``2025-00912345``
    came back for an Orange row that prints no case number.  A number the
    document's extracted text does not carry is dropped; numbers it does
    carry (even wrapped across a line) are kept."""
    writes = _llm_split_writes(
        _OC_DOCUMENT_TEXT, ["2024-01393434", "2023-01329371", "2025-00912345"]
    )
    assert [w.case_number for w in writes] == ["2024-01393434", "2023-01329371", None]
    assert all(w.case_number_evidence == (_OC_DOCUMENT_TEXT,) for w in writes)


def test_llm_split_keeps_case_number_when_document_has_no_text_layer() -> None:
    """A scanned PDF has no extracted text to check against, so the LLM's
    number stands."""
    writes = _llm_split_writes("Page 1", ["2024-01393434", "2025-00912345", None])
    assert [w.case_number for w in writes] == ["2024-01393434", "2025-00912345", None]
    assert all(w.case_number_evidence == () for w in writes)


_RIVERSIDE_DOCUMENT_TEXT = (
    "SUPERIOR COURT OF CALIFORNIA, COUNTY OF RIVERSIDE — TENTATIVE RULINGS, DEPT. 6\n"
    "1  CVRI2501831  Smith v. Jones   Motion to compel further responses.\n"
    "2  CVRI2500044  Doe v. Roe       Demurrer to the first amended complaint.\n"
    "The court has read and considered the moving, opposition and reply papers.\n"
)


def test_llm_split_regex_fallback_cannot_restore_refuted_number() -> None:
    """#4927: the LLM misread ``CVRI2501831`` as ``CVRI2501911`` and its
    transcription of the ruling repeats the misread.  The gate refuted the
    LLM's number, then the regex fallback read ``CVRI2501911`` straight
    back out of the transcription and nothing checked it.  Every candidate
    is now checked against the document's text, so the child is stored
    case-less (``UNKNOWN-``)."""
    transcription = (
        "Plaintiff's motion in CVRI2501911 is GRANTED. Defendant shall serve further "
        "responses within 20 days."
    )
    writes = _llm_split_writes(
        _RIVERSIDE_DOCUMENT_TEXT,
        ["CVRI2501911", "CVRI2500044"],
        [transcription, _SPLIT_CHILD_TEXTS[1]],
    )
    assert [w.case_number for w in writes] == [None, "CVRI2500044"]
    assert "case_number" not in writes[0].extraction_methods


def test_llm_split_regex_fallback_keeps_number_the_document_prints() -> None:
    """Control for #4927: a child the LLM gave no number keeps the regex
    fallback's number when the document prints it."""
    transcription = "Motion in CVRI2501831 is GRANTED; the responses are evasive."
    writes = _llm_split_writes(
        _RIVERSIDE_DOCUMENT_TEXT,
        [None, "CVRI2500044"],
        [transcription, _SPLIT_CHILD_TEXTS[1]],
    )
    assert [w.case_number for w in writes] == ["CVRI2501831", "CVRI2500044"]
    assert writes[0].extraction_methods["case_number"] == "regex_post_llm"


def test_single_ruling_llm_split_falls_back_to_parent_number() -> None:
    """A split into one ruling is the document: when the gate refutes the
    LLM's number, the parent event's number (which the link text carries)
    is next, as it was before the gate moved out of the split."""
    writes = _llm_split_writes(
        _RIVERSIDE_DOCUMENT_TEXT,
        ["CVRI2501911"],
        ["Plaintiff's motion in CVRI2501911 is GRANTED."],
        case_number="CVRI2509999",
        extra={"link_text": "CVRI2509999 Tentative Ruling"},
    )
    assert [w.case_number for w in writes] == ["CVRI2509999"]
    assert writes[0].extraction_methods["case_number"] == "split_parent"


@pytest.mark.parametrize("llm_number", [None, "CVRI2501911"])
def test_single_ruling_llm_split_trusts_parent_number(llm_number: str | None) -> None:
    """The parent event's number is the scraper's (a listing row, say), and
    a whole document keeps it unchecked when the split does not run.  A
    split into one ruling is the same document, so it keeps it too, even
    when neither the text nor the link text prints it."""
    writes = _llm_split_writes(
        _RIVERSIDE_DOCUMENT_TEXT,
        [llm_number],
        ["Plaintiff's motion is GRANTED."],
        case_number="CVRI2509999",
    )
    assert [w.case_number for w in writes] == ["CVRI2509999"]


def _whole_document_with_llm_number(text: str, llm_number: str) -> list:
    """A whole document (no split) whose per-field LLM returns *llm_number*."""
    event = _make_llm_event(
        document_id="aaaaaaaa-0000-0000-0000-000000004928", ruling_text=text, case_number=None
    )
    with patch.object(IngestionWorker, "_llm_split_document", return_value=False):
        return _process_with_llm_case_number(event, llm_number)


def test_short_whole_document_refutes_invented_llm_number() -> None:
    """#4907 on a whole document: the per-field LLM read only "Page 1", so
    a number it returns was invented.  The 200-character floor is for text
    judging numbers read elsewhere (page images, older runs), not for a
    number read from that same text."""
    writes = _whole_document_with_llm_number("Page 1 Smith v. Jones", "2025-00912345")
    assert len(writes) == 1
    assert writes[0].case_number is None
    assert writes[0].case_number_evidence == ()


def test_short_whole_document_keeps_llm_number_it_prints() -> None:
    writes = _whole_document_with_llm_number("Page 1 Case 2025-00912345", "2025-00912345")
    assert [w.case_number for w in writes] == ["2025-00912345"]
    assert writes[0].extraction_methods["case_number"] == "llm"


def test_regex_only_number_still_counts_as_bad_split_fragment() -> None:
    """#2571 is unchanged by the gate running the regex fallback earlier: a
    split child with no number and no caption of its own is a fragment and
    skips LLM enrichment, even when its text quotes a case number."""
    event = _sc_case_less_child("Consolidated with 24CV412345; the motion is DENIED.")
    event["case_title"] = None
    worker, _ = _make_worker()
    mock_conn, _cur = _make_mock_conn()
    writes: list = []
    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch.object(
            IngestionWorker,
            "_write_one_ruling",
            autospec=True,
            side_effect=lambda _self, write: writes.append(write),
        ),
    ):
        mock_psycopg.connect.return_value = mock_conn
        worker.process_event(event)
    assert len(writes) == 1
    assert writes[0].case_number == "24CV412345"
    assert writes[0].extraction_methods["motion_type"] == "skipped_bad_split_fragment"


def test_pre_split_llm_child_transcription_is_not_evidence() -> None:
    """A scraper that split the page with an LLM (CC / LA) emits children
    whose text is that LLM's transcription: no case-number evidence."""
    from ingestion.worker import _case_number_evidence

    llm_child = {"_split_processed": True, "extra": {"pre_split": True, "_llm_extracted": True}}
    deterministic_child = {"_split_processed": True, "_llm_extracted": True}
    assert _case_number_evidence(llm_child, "Motion in 23STCV00001 GRANTED.") == ()
    assert _case_number_evidence(deterministic_child, "See below.") == ("See below.",)
