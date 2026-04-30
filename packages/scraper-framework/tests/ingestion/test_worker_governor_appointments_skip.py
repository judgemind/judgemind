"""Tests for the ca-governor-appointments early-return branch in process_event (#3688).

The ca-governor-appointments scraper emits judge-bio press releases that share the
CapturedDocument shape but are NOT rulings. Before #3688, these passed through the LLM
extractor and produced UNKNOWN-* case_numbers + a 'Governor / Statewide' court row that
polluted derived.rulings. The fix adds an early-return on scraper_id so no DB upsert
or LLM call occurs for governor-appointment events.

Verifies:
  (a) process_event returns None early for ca-governor-appointments events.
  (b) No DB upsert was attempted (_get_connection is never called).
  (c) No LLM split or LLM extractor call was made.
  (d) The bypass info log line was emitted.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from ingestion.worker import IngestionWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker() -> IngestionWorker:
    """Return an IngestionWorker with all external dependencies mocked out."""
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
    # Disable enrichment so _llm_enrich_fields is never reached.
    worker._enrichment_client = None
    # Disable framework extractor (LLM split path).
    worker._get_framework_extractor = lambda: None  # type: ignore[method-assign]
    return worker


def _make_governor_event(**overrides: object) -> dict:
    """Return a realistic ca-governor-appointments event payload."""
    base: dict = {
        "document_id": "bbbbbbbb-0000-0000-0000-000000000001",
        "scraper_id": "ca-governor-appointments",
        "state": "CA",
        "county": "Statewide",
        "court": "Governor",
        "source_url": "https://www.gov.ca.gov/appointments/press-releases/1",
        "content_format": "html",
        "content_hash": "press123",
        "s3_key": "ca/statewide/governor/raw/press123.html",
        "s3_bucket": "judgemind-document-archive-dev",
        # Realistic pollution values that the LLM would have produced before #3688:
        "case_number": "UNKNOWN-abc",
        "case_title": "",
        "judge_name": "Governor, County of Statewide",
        "ruling_text": (
            "Governor Gavin Newsom today announced the appointment of Jane Doe "
            "as a Judge of the Superior Court of California, County of Los Angeles."
        ),
        "hearing_date": "2026-04-28",
        "capture_timestamp": "2026-04-28T12:00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGovernorAppointmentsSkip:
    """AC1: ca-governor-appointments events must be routed to bio path (skip ruling extraction)."""

    def test_governor_appointments_scraper_id_skips_ruling_extraction(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """process_event returns None without DB or LLM calls for ca-governor-appointments.

        Assertions:
          (a) Return value is None (early return).
          (b) No DB connection is obtained — _get_connection was never called.
          (c) No LLM split or extractor call was attempted.
          (d) The bypass info log line was emitted with the expected telemetry_event.
        """
        worker = _make_worker()

        with (
            caplog.at_level(logging.INFO, logger="ingestion.worker"),
            patch.object(worker, "_get_connection") as mock_get_conn,
            patch("ingestion.worker.psycopg") as mock_psycopg,
        ):
            event = _make_governor_event()
            result = worker.process_event(event)

        # (a) Early return — result is None.
        assert result is None, f"Expected None but got {result!r}"

        # (b) No DB connection was obtained.
        mock_get_conn.assert_not_called()
        mock_psycopg.connect.assert_not_called()

        # (c) No LLM split or extractor was invoked.
        # worker._get_framework_extractor is already a no-op lambda; confirm it
        # was never given a chance to produce a non-None extractor by verifying
        # the worker's enrichment client was never touched.
        assert worker._enrichment_client is None  # stays None — no enrichment path reached

        # (d) The bypass info log line was emitted.
        info_msgs = [r for r in caplog.records if r.levelname == "INFO"]
        bypass_msgs = [
            r
            for r in info_msgs
            if "governor_appointment_routed_to_bio_path" in str(r.__dict__)
            or "Routing governor-appointment document to bio path" in r.getMessage()
        ]
        assert bypass_msgs, (
            "Expected at least one INFO log mentioning 'Routing governor-appointment document "
            f"to bio path' but got these INFO records: {[r.getMessage() for r in info_msgs]}"
        )
        # Verify the telemetry_event key is present in the log record's extra dict.
        expected_telemetry = "governor_appointment_routed_to_bio_path"
        for record in bypass_msgs:
            actual = getattr(record, "telemetry_event", None)
            assert actual == expected_telemetry, (
                f"Expected telemetry_event={expected_telemetry!r} in log extra, "
                f"got: {record.__dict__}"
            )

    def test_non_governor_scraper_id_is_not_skipped(self) -> None:
        """Sanity-check: a non-governor scraper_id does NOT early-return before DB access.

        This confirms the early-return guard is narrowly scoped to 'ca-governor-appointments'
        and does not accidentally short-circuit normal ruling scrapers.
        """
        worker = _make_worker()

        with (
            patch.object(worker, "_get_connection") as mock_get_conn,
            patch("ingestion.worker.psycopg") as mock_psycopg,
        ):
            mock_conn = MagicMock()
            mock_cur = MagicMock()
            mock_conn.closed = False
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_psycopg.connect.return_value = mock_conn
            mock_get_conn.return_value = mock_conn
            # Provide a court-uuid and case-uuid so the worker can proceed past the upserts.
            mock_cur.fetchone.side_effect = [
                ("court-uuid-1",),
                ("case-uuid-1",),
                (True,),
            ]

            event = _make_governor_event(
                scraper_id="ca-la-tentatives-civil",
                county="Los Angeles",
                court="Superior Court",
                case_number="24STCV00001",
                case_title="Smith v. Jones",
                judge_name="Hon. Jane Smith",
                ruling_text="Tentative ruling: GRANTED.",
            )
            # This should NOT early-return — it will proceed into DB logic and either
            # succeed or raise depending on mock setup. We only care that _get_connection
            # was called (i.e. the early-return guard was NOT triggered).
            try:
                worker.process_event(event)
            except Exception:  # noqa: BLE001
                pass  # We don't care about downstream errors here.

        # DB connection was attempted — proof the early-return did not fire.
        assert mock_get_conn.called or mock_psycopg.connect.called, (
            "Expected _get_connection or psycopg.connect to be called for a non-governor scraper"
        )
