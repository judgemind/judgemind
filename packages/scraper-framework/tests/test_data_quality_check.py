"""Tests for scripts/data-quality-check.py with mocked DB queries."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Add scripts/ to sys.path so we can import the module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Skip venv re-exec during tests.
import os

os.environ["_VENV_HELPER_SKIP"] = "1"

# Now import after setting the skip flag.
# ruff: noqa: E402
from importlib import import_module

# Import the script as a module (it has a hyphen in its name).
dqc = import_module("data-quality-check")

Alert = dqc.Alert
Baselines = dqc.Baselines
load_baselines = dqc.load_baselines
check_ingest_rates = dqc.check_ingest_rates
check_scraper_staleness = dqc.check_scraper_staleness
format_json = dqc.format_json
format_text = dqc.format_text

NOW = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)


def _make_baselines(
    counties: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Baselines]:
    """Create baselines dict from simple config."""
    if counties is None:
        counties = {
            "Los Angeles": {"expected_daily_rulings": 50, "schedule_type": "daily"},
            "Orange": {"expected_daily_rulings": 20, "schedule_type": "daily"},
        }
    return {
        name: Baselines(
            expected_daily_rulings=cfg.get("expected_daily_rulings", 0),
            schedule_type=cfg.get("schedule_type", "daily"),
        )
        for name, cfg in counties.items()
    }


class FakeCursor:
    """A mock cursor that returns predetermined results based on the query.

    Uses ordered key matching — the FIRST key found in the query wins.
    Keys are checked in insertion order, so put more specific keys first
    in the dict to disambiguate overlapping substrings.
    """

    def __init__(self, query_results: dict[str, list[tuple[Any, ...]]]) -> None:
        self._query_results = query_results
        self._results: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        """Match query to stored results by checking key substrings."""
        for key, results in self._query_results.items():
            if key in query:
                self._results = results
                return
        self._results = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        """Return stored results."""
        return self._results

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class FakeConnection:
    """A mock DB connection with configurable query results."""

    def __init__(self, query_results: dict[str, list[tuple[Any, ...]]]) -> None:
        self._query_results = query_results

    def cursor(self) -> FakeCursor:
        """Return a cursor with the same query results."""
        return FakeCursor(self._query_results)

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class TestLoadBaselines:
    """Tests for load_baselines function."""

    def test_load_valid_file(self, tmp_path: Path) -> None:
        """Loads baselines from a valid JSON file."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "Los Angeles": {
                            "expected_daily_rulings": 50,
                            "schedule_type": "daily",
                        },
                        "Orange": {
                            "expected_daily_rulings": 20,
                            "schedule_type": "frequent",
                        },
                    }
                }
            )
        )
        result = load_baselines(baselines_file)
        assert "Los Angeles" in result
        assert result["Los Angeles"].expected_daily_rulings == 50
        assert result["Los Angeles"].schedule_type == "daily"
        assert result["Orange"].schedule_type == "frequent"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty dict when baselines file does not exist."""
        result = load_baselines(tmp_path / "nonexistent.json")
        assert result == {}

    def test_defaults_for_missing_fields(self, tmp_path: Path) -> None:
        """Uses defaults when config fields are missing."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(json.dumps({"counties": {"Test": {}}}))
        result = load_baselines(baselines_file)
        assert result["Test"].expected_daily_rulings == 0
        assert result["Test"].schedule_type == "daily"


class TestCheckIngestRates:
    """Tests for check_ingest_rates function."""

    def test_healthy_county_no_alerts(self) -> None:
        """No alerts when 24h count is above 50% of 7-day average."""
        # Use unique keys: the 7d query contains "AND d.created_at <" which
        # is NOT in the 24h query. The 24h query uses a unique substring.
        conn = FakeConnection(
            {
                # 7d query — key matches "d.created_at <" (unique to 7d query)
                "d.created_at <": [("Los Angeles", 200)],
                # 24h query — key matches "d.created_at >=" (in both, but 7d matched first)
                "d.created_at >=": [("Los Angeles", 30)],
                # Active counties
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines)
        assert len(alerts) == 0

    def test_zero_rulings_p1_alert(self) -> None:
        """P1 alert when county has zero rulings in 24h but expects some."""
        conn = FakeConnection(
            {
                "d.created_at <": [("Los Angeles", 200)],
                "d.created_at >=": [],  # 0 rulings in 24h
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "Los Angeles"
        assert alerts[0].actual == 0

    def test_ingest_rate_drop_p2_alert(self) -> None:
        """P2 alert when 24h count is below 50% of 7-day average."""
        conn = FakeConnection(
            {
                "d.created_at <": [("Los Angeles", 200)],
                # 5 rulings in 24h — well below 50% of ~33/day avg
                "d.created_at >=": [("Los Angeles", 5)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate"
        assert alerts[0].severity == "p2"
        assert alerts[0].actual == 5

    def test_county_filter(self) -> None:
        """Only checks the specified county when filter is provided."""
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines, county="Orange")
        # Orange has 0 rulings but expected 20 -> zero_rulings alert
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"

    def test_no_baseline_uses_avg(self) -> None:
        """Uses 7-day average when no baseline exists for the county."""
        conn = FakeConnection(
            {
                "d.created_at <": [("Unknown County", 100)],
                "d.created_at >=": [],  # 0 rulings
                "DISTINCT ct.county": [("Unknown County",)],
            }
        )
        # No baselines for "Unknown County" — should use daily_avg from 7d
        alerts = check_ingest_rates(conn, NOW, {})
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"

    def test_multiple_counties(self) -> None:
        """Checks multiple counties and generates appropriate alerts."""
        conn = FakeConnection(
            {
                "d.created_at <": [
                    ("Los Angeles", 200),
                    ("Orange", 100),
                ],
                "d.created_at >=": [("Los Angeles", 30), ("Orange", 0)],
                "DISTINCT ct.county": [("Los Angeles",), ("Orange",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines)
        # LA is healthy (30 vs ~33 avg), Orange has zero -> 1 alert
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"


class TestCheckScraperStaleness:
    """Tests for check_scraper_staleness function."""

    def test_fresh_scraper_no_alert(self) -> None:
        """No alert when scraper ran recently."""
        recent = NOW - timedelta(hours=1)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", recent, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 0

    def test_stale_daily_scraper_alert(self) -> None:
        """Alert when daily scraper hasn't run in >6 hours."""
        stale_time = NOW - timedelta(hours=8)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", stale_time, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "scraper_stale"
        assert alerts[0].county == "Los Angeles"

    def test_stale_frequent_scraper_alert(self) -> None:
        """Alert when frequent scraper hasn't run in >2 hours."""
        stale_time = NOW - timedelta(hours=3)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-oc-tentatives-civil", "Orange", stale_time, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {"Orange": {"expected_daily_rulings": 20, "schedule_type": "frequent"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"

    def test_falls_back_to_captured_at(self) -> None:
        """Uses documents.captured_at when no scraper_runs exist."""
        old_capture = NOW - timedelta(hours=10)
        conn = FakeConnection(
            {
                "scraper_runs": [],  # No scraper_runs
                "MAX(d.captured_at)": [("Los Angeles", old_capture)],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert "captured_at" in alerts[0].message

    def test_very_stale_is_p1(self) -> None:
        """Very stale scrapers (>4x threshold) get p1 severity."""
        very_stale = NOW - timedelta(hours=25)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", very_stale, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].severity == "p1"

    def test_moderately_stale_is_p2(self) -> None:
        """Moderately stale scrapers get p2 severity."""
        stale = NOW - timedelta(hours=8)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", stale, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].severity == "p2"


class TestFormatOutput:
    """Tests for output formatting functions."""

    def test_format_json_healthy(self) -> None:
        """JSON output for healthy state."""
        result = json.loads(format_json([]))
        assert result["healthy"] is True
        assert result["alert_count"] == 0
        assert result["alerts"] == []

    def test_format_json_with_alerts(self) -> None:
        """JSON output includes all alert fields."""
        alerts = [
            Alert(
                county="Los Angeles",
                metric="zero_rulings",
                severity="p1",
                expected=50,
                actual=0,
                message="LA has zero rulings",
            )
        ]
        result = json.loads(format_json(alerts))
        assert result["healthy"] is False
        assert result["alert_count"] == 1
        assert result["alerts"][0]["county"] == "Los Angeles"
        assert result["alerts"][0]["metric"] == "zero_rulings"
        assert result["alerts"][0]["severity"] == "p1"

    def test_format_text_healthy(self) -> None:
        """Text output for healthy state."""
        result = format_text([])
        assert "healthy" in result.lower()

    def test_format_text_with_alerts(self) -> None:
        """Text output includes severity markers and messages."""
        alerts = [
            Alert(
                county="LA",
                metric="zero_rulings",
                severity="p1",
                expected=50,
                actual=0,
                message="LA zero rulings",
            ),
            Alert(
                county="OC",
                metric="ingest_rate",
                severity="p2",
                expected=20,
                actual=5,
                message="OC low ingest",
            ),
        ]
        result = format_text(alerts)
        assert "[P1]" in result
        assert "[P2]" in result
        assert "2 alert(s)" in result


class TestRunChecks:
    """Integration tests for run_checks by directly calling with mocked connections."""

    def test_run_checks_returns_alerts(self) -> None:
        """run_checks combines ingest rate and staleness alerts."""
        old_time = NOW - timedelta(hours=10)
        conn = FakeConnection(
            {
                "d.created_at <": [("Los Angeles", 200)],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Los Angeles",)],
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", old_time, "success")],
                "MAX(d.captured_at)": [],
            }
        )

        baselines = _make_baselines()

        # Patch psycopg.connect on the module object directly
        original_connect = dqc.psycopg.connect
        dqc.psycopg.connect = MagicMock(return_value=conn)
        original_load = dqc.load_baselines
        dqc.load_baselines = MagicMock(return_value=baselines)
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            metrics = {a.metric for a in alerts}
            assert "zero_rulings" in metrics
            assert "scraper_stale" in metrics
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load

    def test_run_checks_healthy(self) -> None:
        """run_checks returns empty list when everything is healthy."""
        recent = NOW - timedelta(hours=1)
        conn = FakeConnection(
            {
                "d.created_at <": [
                    ("Los Angeles", 200),
                    ("Orange", 100),
                ],
                "d.created_at >=": [("Los Angeles", 40), ("Orange", 15)],
                "DISTINCT ct.county": [("Los Angeles",), ("Orange",)],
                "scraper_runs": [
                    ("ca-la-tentatives-civil", "Los Angeles", recent, "success"),
                    ("ca-oc-tentatives-civil", "Orange", recent, "success"),
                ],
                "MAX(d.captured_at)": [],
            }
        )

        baselines = _make_baselines()

        original_connect = dqc.psycopg.connect
        dqc.psycopg.connect = MagicMock(return_value=conn)
        original_load = dqc.load_baselines
        dqc.load_baselines = MagicMock(return_value=baselines)
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            assert len(alerts) == 0
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load
