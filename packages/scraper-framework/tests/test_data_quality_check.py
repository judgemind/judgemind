"""Tests for scripts/data_quality_check.py — collection health monitoring."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The script lives in scripts/ and uses _venv_helper. For tests we need to
# import its functions directly, so add scripts/ to sys.path and skip the
# venv helper.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
os.environ["_VENV_HELPER_SKIP"] = "1"

from data_quality_check import (  # noqa: E402
    Alert,
    CheckResult,
    CountyStats,
    _compute_county_stats,
    check_ingest_rates,
    check_scraper_staleness,
    load_baselines,
    result_to_dict,
    run_check,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
TODAY = NOW.date()
YESTERDAY = (NOW - timedelta(days=1)).date()


@pytest.fixture()
def baselines_file(tmp_path: Path) -> Path:
    """Create a test baselines file."""
    baselines = {
        "scrapers": {
            "ca-la-tentatives-civil": {
                "county": "Los Angeles",
                "schedule": "daily",
                "stale_threshold_hours": 26,
            },
            "ca-oc-tentatives": {
                "county": "Orange",
                "schedule": "daily",
                "stale_threshold_hours": 26,
            },
        },
    }
    p = tmp_path / "baselines.json"
    p.write_text(json.dumps(baselines), encoding="utf-8")
    return p


@pytest.fixture()
def baselines(baselines_file: Path) -> dict:
    """Load test baselines."""
    return load_baselines(baselines_file)


# ---------------------------------------------------------------------------
# load_baselines tests
# ---------------------------------------------------------------------------


class TestLoadBaselines:
    """Tests for load_baselines."""

    def test_loads_valid_file(self, baselines_file: Path) -> None:
        result = load_baselines(baselines_file)
        assert "scrapers" in result
        assert "ca-la-tentatives-civil" in result["scrapers"]

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        result = load_baselines(tmp_path / "nonexistent.json")
        assert result == {"scrapers": {}}


# ---------------------------------------------------------------------------
# _compute_county_stats tests
# ---------------------------------------------------------------------------


class TestComputeCountyStats:
    """Tests for _compute_county_stats."""

    def test_basic_counts(self) -> None:
        """County with data across multiple days computes correctly."""
        daily_counts = [
            ("Los Angeles", TODAY, 10),
            ("Los Angeles", YESTERDAY, 5),
            ("Los Angeles", TODAY - timedelta(days=2), 8),
            ("Los Angeles", TODAY - timedelta(days=3), 12),
            ("Los Angeles", TODAY - timedelta(days=4), 7),
            ("Los Angeles", TODAY - timedelta(days=5), 9),
            ("Los Angeles", TODAY - timedelta(days=6), 11),
            ("Los Angeles", TODAY - timedelta(days=7), 6),
        ]
        stats = _compute_county_stats(daily_counts, NOW)
        assert "Los Angeles" in stats
        la = stats["Los Angeles"]
        # last_24h = today only = 10
        assert la.last_24h == 10
        # 7-day avg = previous 7 days (yesterday through day-7):
        # (5+8+12+7+9+11+6) / 7 = 58/7 = 8.3
        assert la.seven_day_avg == 8.3

    def test_zero_today(self) -> None:
        """County with no documents today."""
        daily_counts = [
            ("Orange", YESTERDAY, 5),
            ("Orange", TODAY - timedelta(days=2), 8),
            ("Orange", TODAY - timedelta(days=3), 12),
        ]
        stats = _compute_county_stats(daily_counts, NOW)
        orange = stats["Orange"]
        # last_24h = 0 (no data today)
        assert orange.last_24h == 0
        # 7-day avg = previous 7 days: (5+8+12+0+0+0+0) / 7 = 25/7 = 3.6
        assert orange.seven_day_avg == 3.6

    def test_empty_data(self) -> None:
        """No data produces empty stats."""
        stats = _compute_county_stats([], NOW)
        assert stats == {}

    def test_multiple_counties(self) -> None:
        """Multiple counties computed independently."""
        daily_counts = [
            ("Los Angeles", TODAY, 10),
            ("Orange", TODAY, 5),
        ]
        stats = _compute_county_stats(daily_counts, NOW)
        assert len(stats) == 2
        assert stats["Los Angeles"].last_24h == 10
        assert stats["Orange"].last_24h == 5


# ---------------------------------------------------------------------------
# check_ingest_rates tests
# ---------------------------------------------------------------------------


class TestCheckIngestRates:
    """Tests for check_ingest_rates."""

    def test_zero_rulings_alert(self, baselines: dict) -> None:
        """County with zero docs in 24h but positive avg triggers p1 alert."""
        county_stats = {
            "Los Angeles": CountyStats(last_24h=0, seven_day_avg=10.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "Los Angeles"
        assert alerts[0].actual == 0
        assert alerts[0].expected == 10.0

    def test_rate_drop_alert(self, baselines: dict) -> None:
        """County with >50% drop triggers p2 alert."""
        county_stats = {
            "Los Angeles": CountyStats(last_24h=3, seven_day_avg=10.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate_drop"
        assert alerts[0].severity == "p2"

    def test_no_alert_when_healthy(self, baselines: dict) -> None:
        """County with normal ingest rate produces no alerts."""
        county_stats = {
            "Los Angeles": CountyStats(last_24h=8, seven_day_avg=10.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 0

    def test_no_alert_when_zero_avg(self, baselines: dict) -> None:
        """County with zero average and zero today produces no alert."""
        county_stats = {
            "Los Angeles": CountyStats(last_24h=0, seven_day_avg=0.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 0

    def test_exactly_50_percent_no_alert(self, baselines: dict) -> None:
        """County at exactly 50% of average does not trigger alert."""
        county_stats = {
            "Los Angeles": CountyStats(last_24h=5, seven_day_avg=10.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 0

    def test_unknown_county_gets_unknown_scraper_id(self) -> None:
        """County not in baselines still gets an alert with 'unknown' scraper_id."""
        baselines: dict = {"scrapers": {}}
        county_stats = {
            "Fresno": CountyStats(last_24h=0, seven_day_avg=5.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 1
        assert alerts[0].scraper_id == "unknown"

    def test_multi_scraper_county_lists_all_scrapers(self) -> None:
        """County with multiple scrapers lists all of them in alert."""
        baselines: dict = {
            "scrapers": {
                "ca-oc-tentatives": {"county": "Orange"},
                "ca-oc-tentatives-family-law": {"county": "Orange"},
                "ca-oc-tentatives-probate": {"county": "Orange"},
            },
        }
        county_stats = {
            "Orange": CountyStats(last_24h=0, seven_day_avg=10.0),
        }
        alerts = check_ingest_rates(county_stats, baselines)
        assert len(alerts) == 1
        # All three scraper IDs should appear in the comma-separated string
        scraper_ids = alerts[0].scraper_id.split(",")
        assert len(scraper_ids) == 3
        assert "ca-oc-tentatives" in scraper_ids
        assert "ca-oc-tentatives-family-law" in scraper_ids
        assert "ca-oc-tentatives-probate" in scraper_ids


# ---------------------------------------------------------------------------
# check_scraper_staleness tests
# ---------------------------------------------------------------------------


class TestCheckScraperStaleness:
    """Tests for check_scraper_staleness."""

    def test_stale_scraper_p2(self, baselines: dict) -> None:
        """Scraper past threshold but within 2x triggers p2."""
        last_seen = {
            "ca-la-tentatives-civil": NOW - timedelta(hours=30),
            "ca-oc-tentatives": NOW - timedelta(hours=10),
        }
        alerts = check_scraper_staleness(last_seen, baselines, NOW)
        assert len(alerts) == 1
        assert alerts[0].scraper_id == "ca-la-tentatives-civil"
        assert alerts[0].severity == "p2"
        assert alerts[0].metric == "scraper_stale"

    def test_very_stale_scraper_p1(self, baselines: dict) -> None:
        """Scraper past 2x threshold triggers p1."""
        last_seen = {
            "ca-la-tentatives-civil": NOW - timedelta(hours=60),
            "ca-oc-tentatives": NOW - timedelta(hours=10),
        }
        alerts = check_scraper_staleness(last_seen, baselines, NOW)
        assert len(alerts) == 1
        assert alerts[0].severity == "p1"

    def test_never_run_scraper(self, baselines: dict) -> None:
        """Scraper that has never run triggers p1."""
        last_seen: dict[str, datetime] = {
            "ca-oc-tentatives": NOW - timedelta(hours=10),
        }
        alerts = check_scraper_staleness(last_seen, baselines, NOW)
        assert len(alerts) == 1
        assert alerts[0].scraper_id == "ca-la-tentatives-civil"
        assert alerts[0].severity == "p1"
        assert alerts[0].actual == -1

    def test_all_healthy(self, baselines: dict) -> None:
        """All scrapers within threshold produce no alerts."""
        last_seen = {
            "ca-la-tentatives-civil": NOW - timedelta(hours=10),
            "ca-oc-tentatives": NOW - timedelta(hours=10),
        }
        alerts = check_scraper_staleness(last_seen, baselines, NOW)
        assert len(alerts) == 0


# ---------------------------------------------------------------------------
# run_check integration test (mocked DB)
# ---------------------------------------------------------------------------


class TestRunCheck:
    """Integration test for run_check with mocked database."""

    def test_run_check_healthy(self, baselines_file: Path) -> None:
        """Healthy system produces no alerts."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        # Set up query results in order:
        # 1. Daily counts (healthy: 10 docs each day for LA)
        daily_rows = [
            (
                "Los Angeles",
                TODAY - timedelta(days=i),
                10,
            )
            for i in range(7)
        ]

        # 2. Latest scraper runs (recent success)
        run_rows = [
            ("ca-la-tentatives-civil", "Los Angeles", NOW - timedelta(hours=10)),
            ("ca-oc-tentatives", "Orange", NOW - timedelta(hours=10)),
        ]

        # 3. Latest captures (fallback)
        capture_rows = [
            ("ca-la-tentatives-civil", "Los Angeles", NOW - timedelta(hours=12)),
        ]

        mock_cursor.fetchall.side_effect = [daily_rows, run_rows, capture_rows]

        with patch("data_quality_check.psycopg") as mock_psycopg:
            mock_psycopg.connect.return_value = mock_conn
            result = run_check(
                "postgresql://test:test@localhost/test",
                baselines_path=baselines_file,
                now=NOW,
            )

        assert len(result.alerts) == 0
        assert "Los Angeles" in result.county_stats

    def test_run_check_with_alerts(self, baselines_file: Path) -> None:
        """Unhealthy system produces alerts."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cursor

        # LA has zero docs today but had data before
        daily_rows = [
            (
                "Los Angeles",
                TODAY - timedelta(days=i),
                10,
            )
            for i in range(2, 7)
        ]  # Only days 2-6, nothing today/yesterday

        # Scraper last ran 30 hours ago (stale)
        run_rows = [
            ("ca-la-tentatives-civil", "Los Angeles", NOW - timedelta(hours=30)),
            ("ca-oc-tentatives", "Orange", NOW - timedelta(hours=10)),
        ]
        capture_rows: list[tuple] = []

        mock_cursor.fetchall.side_effect = [daily_rows, run_rows, capture_rows]

        with patch("data_quality_check.psycopg") as mock_psycopg:
            mock_psycopg.connect.return_value = mock_conn
            result = run_check(
                "postgresql://test:test@localhost/test",
                baselines_path=baselines_file,
                now=NOW,
            )

        # Should have a zero_rulings alert + a scraper_stale alert for LA
        metrics = {a.metric for a in result.alerts}
        assert "zero_rulings" in metrics
        assert "scraper_stale" in metrics


# ---------------------------------------------------------------------------
# result_to_dict tests
# ---------------------------------------------------------------------------


class TestResultToDict:
    """Tests for result_to_dict serialization."""

    def test_serializes_correctly(self) -> None:
        """CheckResult serializes to JSON-compatible dict."""
        result = CheckResult(
            timestamp="2026-03-11T12:00:00+00:00",
            alerts=[
                Alert(
                    county="Los Angeles",
                    scraper_id="ca-la-tentatives-civil",
                    metric="zero_rulings",
                    severity="p1",
                    expected=10.0,
                    actual=0,
                    message="Zero rulings",
                ),
            ],
            county_stats={
                "Los Angeles": CountyStats(last_24h=0, seven_day_avg=10.0),
            },
        )
        d = result_to_dict(result)
        # Should be JSON-serializable
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert len(parsed["alerts"]) == 1
        assert parsed["alerts"][0]["metric"] == "zero_rulings"
        assert parsed["county_stats"]["Los Angeles"]["last_24h"] == 0
