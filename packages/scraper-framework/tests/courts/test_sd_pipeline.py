"""Tests for San Diego County pipeline scraper (Phase 1 -> Phase 2 integration).

The pipeline scraper chains the calendar scraper (Phase 1) with the tentative
rulings scraper (Phase 2):
1. Phase 1 enumerates case numbers from the civil calendar
2. Phase 2 fetches tentative rulings for those case numbers via Odyssey ROA

Fixtures are reused from the Phase 1 and Phase 2 test suites.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from courts.ca.sd_calendar import CALENDAR_BASE_URL
from courts.ca.sd_pipeline import (
    SDPipelineScraper,
    default_config,
)
from framework.events import EventBus
from framework.models import CapturedDocument, ContentFormat, ScraperConfig
from framework.storage import S3Archiver

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _make_config() -> ScraperConfig:
    return ScraperConfig(
        scraper_id="ca-sd-pipeline-test",
        state="CA",
        county="San Diego",
        court="Superior Court",
        target_urls=[CALENDAR_BASE_URL],
        request_delay_seconds=0.0,
    )


def _mock_phase2_capturing(
    captured: list[str],
) -> Any:
    """Return a mock _create_phase2_scraper that captures case numbers."""

    def mock_phase2(
        self_inner: Any,
        case_numbers: list[str],
        archiver: S3Archiver | None,
        event_bus: EventBus | None,
    ) -> MagicMock:
        captured.extend(case_numbers)
        mock_scraper = MagicMock()
        mock_scraper.fetch_documents.return_value = []
        return mock_scraper

    return mock_phase2


def _mock_phase2_returning(
    docs: list[CapturedDocument],
) -> Any:
    """Return a mock _create_phase2_scraper that returns given docs."""

    def mock_phase2(
        self_inner: Any,
        case_numbers: list[str],
        archiver: S3Archiver | None,
        event_bus: EventBus | None,
    ) -> MagicMock:
        mock_scraper = MagicMock()
        mock_scraper.fetch_documents.return_value = docs
        return mock_scraper

    return mock_phase2


def _mock_phase2_tracking() -> tuple[Any, list[bool]]:
    """Return a mock _create_phase2_scraper and a list tracking whether it was called."""
    called: list[bool] = []

    def mock_phase2(
        self_inner: Any,
        case_numbers: list[str],
        archiver: S3Archiver | None,
        event_bus: EventBus | None,
    ) -> MagicMock:
        called.append(True)
        mock_scraper = MagicMock()
        mock_scraper.fetch_documents.return_value = []
        return mock_scraper

    return mock_phase2, called


def _mock_calendar_urls_day1(
    central_html: str,
    empty_html: str,
) -> None:
    """Set up respx mocks for day-1 calendar URLs across all 4 divisions."""
    respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(
        return_value=httpx.Response(200, text=central_html)
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(
        return_value=httpx.Response(200, text=empty_html)
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(
        return_value=httpx.Response(200, text=empty_html)
    )
    respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(
        return_value=httpx.Response(200, text=empty_html)
    )


# ---------------------------------------------------------------------------
# default_config — factory test
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    """Tests for the pipeline default configuration factory."""

    def test_scraper_id(self) -> None:
        config = default_config()
        assert config.scraper_id == "ca-sd-pipeline"

    def test_state_and_county(self) -> None:
        config = default_config()
        assert config.state == "CA"
        assert config.county == "San Diego"

    def test_schedule_windows(self) -> None:
        config = default_config()
        assert len(config.schedule_windows) == 2
        # Primary window: 4:15 PM (after court's 4:00 PM posting deadline)
        assert config.schedule_windows[0].start.hour == 16
        assert config.schedule_windows[0].start.minute == 15

    def test_s3_bucket_parameter(self) -> None:
        config = default_config(s3_bucket="my-bucket")
        assert config.s3_bucket == "my-bucket"

    def test_poll_interval(self) -> None:
        config = default_config()
        assert config.poll_interval_seconds == 86400  # daily


# ---------------------------------------------------------------------------
# Phase 1 -> Phase 2 handoff contract
# ---------------------------------------------------------------------------


class TestPhase1ToPhase2Handoff:
    """Tests that case numbers flow correctly from Phase 1 to Phase 2."""

    @respx.mock
    def test_case_numbers_passed_to_phase2(self) -> None:
        """Phase 1 case numbers are extracted and passed to Phase 2."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")
        _mock_calendar_urls_day1(central_html, empty_html)

        captured_case_numbers: list[str] = []
        mock_fn = _mock_phase2_capturing(captured_case_numbers)

        with patch.object(type(scraper), "_create_phase2_scraper", mock_fn):
            scraper.fetch_documents()

        # Central fixture has 7 motion hearings with these unique case numbers:
        # 24CU016153C, 25CU003887C, 23CU005421C, 22CU008901C,
        # 24CU017234C, 25CU004112C, 23CU009876C
        assert len(captured_case_numbers) == 7
        assert "24CU016153C" in captured_case_numbers
        assert "25CU003887C" in captured_case_numbers

    @respx.mock
    def test_case_numbers_are_deduplicated(self) -> None:
        """Same case number across multiple pages is only passed once."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1, 2])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")

        # Serve the same central calendar for both day 1 and day 2
        for day in [1, 2]:
            respx.get(f"{CALENDAR_BASE_URL}/f_svcal{day}.html").mock(
                return_value=httpx.Response(200, text=central_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )
            respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL{day}.html").mock(
                return_value=httpx.Response(200, text=empty_html)
            )

        captured_case_numbers: list[str] = []
        mock_fn = _mock_phase2_capturing(captured_case_numbers)

        with patch.object(type(scraper), "_create_phase2_scraper", mock_fn):
            scraper.fetch_documents()

        # Same central page served twice — case numbers should be deduplicated
        assert len(captured_case_numbers) == len(set(captured_case_numbers))
        assert len(captured_case_numbers) == 7  # 7 unique motion cases

    @respx.mock
    def test_empty_calendar_skips_phase2(self) -> None:
        """When no motion hearings found, Phase 2 is not invoked."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1])

        empty_html = _load_html("sd_calendar_empty.html")
        _mock_calendar_urls_day1(empty_html, empty_html)

        mock_fn, called = _mock_phase2_tracking()

        with patch.object(type(scraper), "_create_phase2_scraper", mock_fn):
            docs = scraper.fetch_documents()

        assert docs == []
        assert len(called) == 0


# ---------------------------------------------------------------------------
# Pipeline end-to-end (both phases mocked)
# ---------------------------------------------------------------------------


class TestPipelineEndToEnd:
    """End-to-end tests with Phase 1 mocked via respx and Phase 2 mocked."""

    @respx.mock
    def test_returns_phase2_documents(self) -> None:
        """Pipeline returns the documents from Phase 2."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1])

        central_html = _load_html("sd_calendar_central.html")
        empty_html = _load_html("sd_calendar_empty.html")
        _mock_calendar_urls_day1(central_html, empty_html)

        # Create a mock Phase 2 doc
        mock_doc = CapturedDocument(
            scraper_id="ca-sd-pipeline-test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://odyroa.sdcourt.ca.gov/portal/Home/CaseDetail/123",
            capture_timestamp=datetime(2026, 3, 13),
            content_format=ContentFormat.HTML,
            raw_content=b"<html>ruling</html>",
            content_hash="abc123",
            case_number="24CU016153C",
            judge_name="Matthew C. Braner",
            ruling_text="The motion is GRANTED.",
        )

        mock_fn = _mock_phase2_returning([mock_doc])

        with patch.object(type(scraper), "_create_phase2_scraper", mock_fn):
            docs = scraper.fetch_documents()

        assert len(docs) == 1
        assert docs[0].case_number == "24CU016153C"
        assert docs[0].judge_name == "Matthew C. Braner"

    @respx.mock
    def test_health_event_from_run(self) -> None:
        """Pipeline run() returns a proper health event."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1])

        empty_html = _load_html("sd_calendar_empty.html")
        _mock_calendar_urls_day1(empty_html, empty_html)

        health = scraper.run()
        assert health.success is True
        assert health.scraper_id == "ca-sd-pipeline-test"
        assert health.response_time_seconds > 0

    @respx.mock
    def test_phase1_failure_reports_health(self) -> None:
        """If Phase 1 fails, the pipeline still returns a health event."""
        config = _make_config()
        scraper = SDPipelineScraper(config, day_numbers=[1])

        # All calendar pages return 500
        respx.get(f"{CALENDAR_BASE_URL}/f_svcal1.html").mock(return_value=httpx.Response(500))
        respx.get(f"{CALENDAR_BASE_URL}/F_VVCAL1.html").mock(return_value=httpx.Response(500))
        respx.get(f"{CALENDAR_BASE_URL}/F_EVCAL1.html").mock(return_value=httpx.Response(500))
        respx.get(f"{CALENDAR_BASE_URL}/F_BVCAL1.html").mock(return_value=httpx.Response(500))

        # Phase 1 handles errors gracefully and returns empty docs
        # so the pipeline should succeed with 0 records
        health = scraper.run()
        assert health.success is True
        assert health.records_captured == 0


# ---------------------------------------------------------------------------
# parse_document delegation
# ---------------------------------------------------------------------------


class TestParseDocumentDelegation:
    """Tests that parse_document delegates to Phase 2 parsing."""

    def test_parse_document_enriches_from_roa_html(self) -> None:
        """parse_document should parse ROA HTML and populate fields."""
        config = _make_config()
        scraper = SDPipelineScraper(config)

        html = _load_html("sd_roa_case_detail.html")
        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 12),
            content_format=ContentFormat.HTML,
            raw_content=html.encode("utf-8"),
            content_hash="abc123",
        )

        result = scraper.parse_document(doc)
        assert result.case_number == "24CU016153C"
        assert result.judge_name == "Matthew C. Braner"
        assert result.department == "C-60"

    def test_parse_document_handles_invalid_html(self) -> None:
        """parse_document should not crash on non-ROA HTML."""
        config = _make_config()
        scraper = SDPipelineScraper(config)

        doc = CapturedDocument(
            scraper_id="test",
            state="CA",
            county="San Diego",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=datetime(2026, 3, 12),
            content_format=ContentFormat.HTML,
            raw_content=b"<html><body>not ROA</body></html>",
            content_hash="abc123",
        )

        result = scraper.parse_document(doc)
        assert result.case_number is None


# ---------------------------------------------------------------------------
# Runner registration
# ---------------------------------------------------------------------------


class TestRunnerRegistration:
    """Tests that SD scrapers are registered in the runner."""

    def test_sd_calendar_registered(self) -> None:
        from framework.runner import get_scraper_ids

        ids = get_scraper_ids()
        assert "ca-sd-calendar" in ids

    def test_sd_pipeline_registered(self) -> None:
        from framework.runner import get_scraper_ids

        ids = get_scraper_ids()
        assert "ca-sd-pipeline" in ids

    def test_sd_tentatives_still_registered(self) -> None:
        """Phase 2 standalone scraper should remain registered."""
        from framework.runner import get_scraper_ids

        ids = get_scraper_ids()
        assert "ca-sd-tentatives" in ids
