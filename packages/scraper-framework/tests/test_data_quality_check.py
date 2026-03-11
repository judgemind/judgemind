"""Tests for scripts/data-quality-check.py with mocked DB queries."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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
FileIssuesResult = dqc.FileIssuesResult
load_baselines = dqc.load_baselines
load_field_baselines = dqc.load_field_baselines
save_field_baselines = dqc.save_field_baselines
check_ingest_rates = dqc.check_ingest_rates
check_scraper_staleness = dqc.check_scraper_staleness
check_field_completeness = dqc.check_field_completeness
_query_field_completeness = dqc._query_field_completeness
format_json = dqc.format_json
format_text = dqc.format_text
file_issues_for_alerts = dqc.file_issues_for_alerts
_issue_title = dqc._issue_title
_issue_body = dqc._issue_body
_issue_labels = dqc._issue_labels
_check_duplicate = dqc._check_duplicate
_file_single_issue = dqc._file_single_issue

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
                # "LEFT JOIN rulings" must come before "d.created_at >=" so
                # the field completeness query matches it first (both queries
                # contain "d.created_at >=" but only the field completeness
                # query contains "LEFT JOIN rulings").
                "LEFT JOIN rulings": [],
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
        original_field_load = dqc.load_field_baselines
        dqc.load_field_baselines = MagicMock(return_value={})
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            metrics = {a.metric for a in alerts}
            assert "zero_rulings" in metrics
            assert "scraper_stale" in metrics
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load
            dqc.load_field_baselines = original_field_load

    def test_run_checks_healthy(self) -> None:
        """run_checks returns empty list when everything is healthy."""
        recent = NOW - timedelta(hours=1)
        conn = FakeConnection(
            {
                # "LEFT JOIN rulings" must come first — see comment in test above.
                "LEFT JOIN rulings": [],
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
        original_field_load = dqc.load_field_baselines
        dqc.load_field_baselines = MagicMock(return_value={})
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            assert len(alerts) == 0
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load
            dqc.load_field_baselines = original_field_load


# ---------------------------------------------------------------------------
# Issue filing tests
# ---------------------------------------------------------------------------

_P1_ALERT = Alert(
    county="Los Angeles",
    metric="zero_rulings",
    severity="p1",
    expected=50,
    actual=0,
    message="Los Angeles: zero new rulings in 24h (expected ~50.0/day)",
)

_P2_ALERT = Alert(
    county="Orange",
    metric="ingest_rate",
    severity="p2",
    expected=20.0,
    actual=5,
    message="Orange: 5 rulings in 24h, 7-day avg is 20.0/day (>50% drop)",
)

_STALE_ALERT = Alert(
    county="Santa Clara",
    metric="scraper_stale",
    severity="p1",
    expected="<6h",
    actual="25.0h",
    message="Santa Clara: scraper stale for 25.0h (threshold: 6h, source: scraper_runs)",
)


class TestIssueTitle:
    """Tests for _issue_title helper."""

    def test_zero_rulings_title(self) -> None:
        """Generates correct title for zero_rulings metric."""
        title = _issue_title(_P1_ALERT)
        assert title == "[DQ] Los Angeles — zero rulings"

    def test_ingest_rate_title(self) -> None:
        """Generates correct title for ingest_rate metric."""
        title = _issue_title(_P2_ALERT)
        assert title == "[DQ] Orange — ingest rate drop"

    def test_scraper_stale_title(self) -> None:
        """Generates correct title for scraper_stale metric."""
        title = _issue_title(_STALE_ALERT)
        assert title == "[DQ] Santa Clara — scraper stale"

    def test_unknown_metric_uses_raw_name(self) -> None:
        """Falls back to raw metric name for unknown metrics."""
        alert = Alert(
            county="Test",
            metric="custom_metric",
            severity="p2",
            expected=1,
            actual=0,
            message="test",
        )
        assert _issue_title(alert) == "[DQ] Test — custom_metric"


class TestIssueBody:
    """Tests for _issue_body helper."""

    def test_contains_alert_details(self) -> None:
        """Issue body includes all alert fields."""
        body = _issue_body(_P1_ALERT)
        assert "Los Angeles" in body
        assert "zero_rulings" in body
        assert "p1" in body
        assert "50" in body
        assert "0" in body

    def test_contains_diagnostic_guidance(self) -> None:
        """Issue body includes diagnostic commands."""
        body = _issue_body(_P1_ALERT)
        assert "scraper_runs" in body
        assert "Los Angeles" in body
        assert "Diagnostic Guidance" in body

    def test_contains_parent_reference(self) -> None:
        """Issue body references the parent monitoring issue."""
        body = _issue_body(_P1_ALERT)
        assert "#739" in body

    def test_contains_auto_filed_note(self) -> None:
        """Issue body notes it was filed automatically."""
        body = _issue_body(_P1_ALERT)
        assert "data-quality-check.py --file-issues" in body


class TestIssueLabels:
    """Tests for _issue_labels helper."""

    def test_p1_labels(self) -> None:
        """P1 alerts get priority/p1 label."""
        labels = _issue_labels(_P1_ALERT)
        assert "priority/p1" in labels
        assert "type/bug" in labels
        assert "agent/ready" in labels
        assert "area/scraping" in labels
        assert "priority/p2" not in labels

    def test_p2_labels(self) -> None:
        """P2 alerts get priority/p2 label."""
        labels = _issue_labels(_P2_ALERT)
        assert "priority/p2" in labels
        assert "priority/p1" not in labels
        assert "type/bug" in labels
        assert "agent/ready" in labels


class TestCheckDuplicate:
    """Tests for _check_duplicate with mocked subprocess."""

    @patch("subprocess.run")
    def test_no_duplicates_found(self, mock_run: MagicMock) -> None:
        """Returns False when no matching issues exist."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        assert _check_duplicate(_P1_ALERT) is False
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "gh" in call_args
        assert "issue" in call_args
        assert "list" in call_args

    @patch("subprocess.run")
    def test_duplicate_exists(self, mock_run: MagicMock) -> None:
        """Returns True when an exact title match exists."""
        existing = [{"number": 100, "title": "[DQ] Los Angeles — zero rulings"}]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(existing), stderr=""
        )
        assert _check_duplicate(_P1_ALERT) is True

    @patch("subprocess.run")
    def test_partial_match_not_duplicate(self, mock_run: MagicMock) -> None:
        """Returns False when title partially matches but is not exact."""
        existing = [{"number": 100, "title": "[DQ] Los Angeles — ingest rate drop"}]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(existing), stderr=""
        )
        assert _check_duplicate(_P1_ALERT) is False

    @patch("subprocess.run")
    def test_gh_failure_returns_false(self, mock_run: MagicMock) -> None:
        """Returns False (not duplicate) when gh command fails."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="auth error"
        )
        assert _check_duplicate(_P1_ALERT) is False

    @patch("subprocess.run")
    def test_invalid_json_returns_false(self, mock_run: MagicMock) -> None:
        """Returns False when gh output is not valid JSON."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )
        assert _check_duplicate(_P1_ALERT) is False

    @patch("subprocess.run")
    def test_uses_correct_repo(self, mock_run: MagicMock) -> None:
        """Passes the correct repo to gh."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        _check_duplicate(_P1_ALERT, repo="custom/repo")
        call_args = mock_run.call_args[0][0]
        repo_idx = call_args.index("--repo")
        assert call_args[repo_idx + 1] == "custom/repo"

    @patch("subprocess.run")
    def test_searches_open_issues_only(self, mock_run: MagicMock) -> None:
        """Only searches open issues (not closed)."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="[]", stderr=""
        )
        _check_duplicate(_P1_ALERT)
        call_args = mock_run.call_args[0][0]
        state_idx = call_args.index("--state")
        assert call_args[state_idx + 1] == "open"


class TestFileSingleIssue:
    """Tests for _file_single_issue with mocked subprocess."""

    @patch("subprocess.run")
    def test_successful_filing(self, mock_run: MagicMock) -> None:
        """Returns issue number on successful creation."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/issues/42\n",
            stderr="",
        )
        result = _file_single_issue(_P1_ALERT)
        assert result == 42

    @patch("subprocess.run")
    def test_gh_failure_returns_none(self, mock_run: MagicMock) -> None:
        """Returns None when gh command fails."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied"
        )
        result = _file_single_issue(_P1_ALERT)
        assert result is None

    @patch("subprocess.run")
    def test_correct_labels_for_p1(self, mock_run: MagicMock) -> None:
        """P1 alerts include priority/p1 label in gh command."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/issues/1\n",
            stderr="",
        )
        _file_single_issue(_P1_ALERT)
        call_args = mock_run.call_args[0][0]
        # Check that --label priority/p1 is in the args
        label_indices = [i for i, x in enumerate(call_args) if x == "--label"]
        labels = [call_args[i + 1] for i in label_indices]
        assert "priority/p1" in labels
        assert "type/bug" in labels
        assert "agent/ready" in labels

    @patch("subprocess.run")
    def test_correct_labels_for_p2(self, mock_run: MagicMock) -> None:
        """P2 alerts include priority/p2 label in gh command."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/issues/1\n",
            stderr="",
        )
        _file_single_issue(_P2_ALERT)
        call_args = mock_run.call_args[0][0]
        label_indices = [i for i, x in enumerate(call_args) if x == "--label"]
        labels = [call_args[i + 1] for i in label_indices]
        assert "priority/p2" in labels
        assert "priority/p1" not in labels

    @patch("subprocess.run")
    def test_uses_body_file(self, mock_run: MagicMock) -> None:
        """Uses --body-file instead of inline --body."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/issues/1\n",
            stderr="",
        )
        _file_single_issue(_P1_ALERT)
        call_args = mock_run.call_args[0][0]
        assert "--body-file" in call_args
        assert "--body" not in call_args

    @patch("subprocess.run")
    def test_correct_title(self, mock_run: MagicMock) -> None:
        """Uses the dedup-friendly title format."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/judgemind/judgemind/issues/1\n",
            stderr="",
        )
        _file_single_issue(_P1_ALERT)
        call_args = mock_run.call_args[0][0]
        title_idx = call_args.index("--title")
        assert call_args[title_idx + 1] == "[DQ] Los Angeles — zero rulings"


class TestFileIssuesForAlerts:
    """Tests for the top-level file_issues_for_alerts function."""

    @patch("subprocess.run")
    def test_files_issues_for_all_alerts(self, mock_run: MagicMock) -> None:
        """Files an issue for each alert when no duplicates exist."""
        # First call = dedup check (no results), second = create
        mock_run.side_effect = [
            # dedup check for alert 1
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
            # create alert 1
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/judgemind/judgemind/issues/10\n",
                stderr="",
            ),
            # dedup check for alert 2
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
            # create alert 2
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/judgemind/judgemind/issues/11\n",
                stderr="",
            ),
        ]
        result = file_issues_for_alerts([_P1_ALERT, _P2_ALERT])
        assert result.filed == [10, 11]
        assert result.skipped_duplicate == []
        assert result.failed == []

    @patch("subprocess.run")
    def test_skips_duplicates(self, mock_run: MagicMock) -> None:
        """Skips filing when a duplicate open issue exists."""
        existing = [{"number": 100, "title": "[DQ] Los Angeles — zero rulings"}]
        mock_run.side_effect = [
            # dedup check for alert 1 — duplicate found
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(existing),
                stderr="",
            ),
            # dedup check for alert 2 — no duplicate
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
            # create alert 2
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/judgemind/judgemind/issues/11\n",
                stderr="",
            ),
        ]
        result = file_issues_for_alerts([_P1_ALERT, _P2_ALERT])
        assert result.filed == [11]
        assert len(result.skipped_duplicate) == 1
        assert "[DQ] Los Angeles" in result.skipped_duplicate[0]

    @patch("subprocess.run")
    def test_tracks_failures(self, mock_run: MagicMock) -> None:
        """Records failed filings in the result."""
        mock_run.side_effect = [
            # dedup check — no duplicate
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
            # create fails
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth error"),
        ]
        result = file_issues_for_alerts([_P1_ALERT])
        assert result.filed == []
        assert len(result.failed) == 1

    @patch("subprocess.run")
    def test_empty_alerts_no_calls(self, mock_run: MagicMock) -> None:
        """Does nothing when no alerts are provided."""
        result = file_issues_for_alerts([])
        assert result.filed == []
        assert result.skipped_duplicate == []
        assert result.failed == []
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_custom_repo(self, mock_run: MagicMock) -> None:
        """Passes custom repo to gh commands."""
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/custom/repo/issues/1\n",
                stderr="",
            ),
        ]
        file_issues_for_alerts([_P1_ALERT], repo="custom/repo")
        # Check that both calls used the custom repo
        for call in mock_run.call_args_list:
            call_args = call[0][0]
            repo_idx = call_args.index("--repo")
            assert call_args[repo_idx + 1] == "custom/repo"


def _make_field_completeness_row(
    county: str,
    total: int = 100,
    ruling: int = 100,
    judge: int = 95,
    motion_type: int = 90,
    outcome: int = 90,
    title: int = 100,
    case_number: int = 100,
    parties: int = 80,
    hearing_date: int = 100,
    case_type: int = 85,
) -> tuple[Any, ...]:
    """Create a row matching the FIELD_COMPLETENESS_QUERY result shape."""
    return (
        county,
        total,
        ruling,
        judge,
        motion_type,
        outcome,
        title,
        case_number,
        parties,
        hearing_date,
        case_type,
    )


class TestLoadFieldBaselines:
    """Tests for load_field_baselines function."""

    def test_load_valid_file(self, tmp_path: Path) -> None:
        """Loads field completeness baselines from JSON."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {
                        "Los Angeles": {
                            "ruling": 99.5,
                            "judge": 95.0,
                        }
                    },
                }
            )
        )
        result = load_field_baselines(baselines_file)
        assert "Los Angeles" in result
        assert result["Los Angeles"]["ruling"] == 99.5
        assert result["Los Angeles"]["judge"] == 95.0

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty dict when baselines file does not exist."""
        result = load_field_baselines(tmp_path / "nonexistent.json")
        assert result == {}

    def test_missing_section_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty dict when field_completeness section is absent."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(json.dumps({"counties": {}}))
        result = load_field_baselines(baselines_file)
        assert result == {}


class TestSaveFieldBaselines:
    """Tests for save_field_baselines function."""

    def test_creates_new_file(self, tmp_path: Path) -> None:
        """Creates baselines file when it does not exist."""
        baselines_file = tmp_path / "baselines.json"
        current = {"Los Angeles": {"ruling": 99.5, "judge": 95.0}}
        save_field_baselines(current, baselines_file)

        with open(baselines_file) as f:
            raw = json.load(f)
        assert raw["field_completeness"]["Los Angeles"]["ruling"] == 99.5
        assert raw["field_completeness"]["Los Angeles"]["judge"] == 95.0

    def test_ratchet_up_only(self, tmp_path: Path) -> None:
        """Only updates baselines when current value is higher."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {"Los Angeles": {"ruling": 99.5, "judge": 95.0}},
                }
            )
        )
        # Try to save lower values — should not decrease.
        current = {"Los Angeles": {"ruling": 90.0, "judge": 97.0}}
        save_field_baselines(current, baselines_file)

        with open(baselines_file) as f:
            raw = json.load(f)
        fc = raw["field_completeness"]["Los Angeles"]
        assert fc["ruling"] == 99.5  # Kept old higher value
        assert fc["judge"] == 97.0  # Updated to new higher value

    def test_preserves_existing_counties_section(self, tmp_path: Path) -> None:
        """Preserves the existing counties section when updating."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "Los Angeles": {
                            "expected_daily_rulings": 50,
                            "schedule_type": "daily",
                        }
                    },
                    "field_completeness": {},
                }
            )
        )
        current = {"Los Angeles": {"ruling": 99.0}}
        save_field_baselines(current, baselines_file)

        with open(baselines_file) as f:
            raw = json.load(f)
        # Counties section should be untouched.
        assert raw["counties"]["Los Angeles"]["expected_daily_rulings"] == 50
        assert raw["field_completeness"]["Los Angeles"]["ruling"] == 99.0

    def test_adds_new_county(self, tmp_path: Path) -> None:
        """Adds a new county to existing baselines."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {"Los Angeles": {"ruling": 99.0}},
                }
            )
        )
        current = {"Orange": {"ruling": 98.0}}
        save_field_baselines(current, baselines_file)

        with open(baselines_file) as f:
            raw = json.load(f)
        assert raw["field_completeness"]["Los Angeles"]["ruling"] == 99.0
        assert raw["field_completeness"]["Orange"]["ruling"] == 98.0


class TestQueryFieldCompleteness:
    """Tests for _query_field_completeness function."""

    def test_returns_percentages(self) -> None:
        """Returns per-field percentages for each county."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=200,
                        ruling=200,
                        judge=190,
                        motion_type=180,
                        outcome=180,
                        title=200,
                        case_number=200,
                        parties=160,
                        hearing_date=200,
                        case_type=170,
                    ),
                ],
            }
        )
        result = _query_field_completeness(conn, NOW)
        assert "Los Angeles" in result
        assert result["Los Angeles"]["ruling"] == 100.0
        assert result["Los Angeles"]["judge"] == 95.0
        assert result["Los Angeles"]["parties"] == 80.0

    def test_skips_zero_total_counties(self) -> None:
        """Skips counties with zero total documents."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Empty County",
                        total=0,
                        ruling=0,
                        judge=0,
                        motion_type=0,
                        outcome=0,
                        title=0,
                        case_number=0,
                        parties=0,
                        hearing_date=0,
                        case_type=0,
                    ),
                ],
            }
        )
        result = _query_field_completeness(conn, NOW)
        assert "Empty County" not in result

    def test_multiple_counties(self) -> None:
        """Returns results for multiple counties."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row("Los Angeles", total=100, judge=95),
                    _make_field_completeness_row("Orange", total=50, judge=40),
                ],
            }
        )
        result = _query_field_completeness(conn, NOW)
        assert len(result) == 2
        assert result["Los Angeles"]["judge"] == 95.0
        assert result["Orange"]["judge"] == 80.0


class TestCheckFieldCompleteness:
    """Tests for check_field_completeness function."""

    def test_no_alerts_when_above_baseline(self) -> None:
        """No alerts when all fields are at or above baseline."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=96,
                    ),
                ],
            }
        )
        field_baselines = {
            "Los Angeles": {"judge": 95.0, "ruling": 100.0},
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        assert len(alerts) == 0

    def test_p1_alert_for_large_drop(self) -> None:
        """P1 alert when field drops more than 10 percentage points."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=80,
                    ),
                ],
            }
        )
        field_baselines = {
            "Los Angeles": {
                "judge": 95.0,
                "ruling": 100.0,
                "motion_type": 90.0,
                "outcome": 90.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 80.0,
                "hearing_date": 100.0,
                "case_type": 85.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        judge_alerts = [a for a in alerts if "judge" in a.message]
        assert len(judge_alerts) == 1
        assert judge_alerts[0].severity == "p1"
        assert judge_alerts[0].metric == "field_completeness"
        assert judge_alerts[0].expected == 95.0
        assert judge_alerts[0].actual == 80.0

    def test_p2_alert_for_moderate_drop(self) -> None:
        """P2 alert when field drops 5-10 percentage points."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=88,
                    ),
                ],
            }
        )
        field_baselines = {
            "Los Angeles": {
                "judge": 95.0,
                "ruling": 100.0,
                "motion_type": 90.0,
                "outcome": 90.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 80.0,
                "hearing_date": 100.0,
                "case_type": 85.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        judge_alerts = [a for a in alerts if "judge" in a.message]
        assert len(judge_alerts) == 1
        assert judge_alerts[0].severity == "p2"

    def test_ignores_zero_baseline_fields(self) -> None:
        """Does not alert on fields with 0% baseline."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        parties=0,
                    ),
                ],
            }
        )
        # parties baseline is 0% — should not alert even though current is also 0%.
        field_baselines = {
            "Los Angeles": {
                "ruling": 100.0,
                "judge": 95.0,
                "motion_type": 90.0,
                "outcome": 90.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 0.0,
                "hearing_date": 100.0,
                "case_type": 85.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        parties_alerts = [a for a in alerts if "parties" in a.message]
        assert len(parties_alerts) == 0

    def test_no_alerts_without_baselines(self) -> None:
        """No alerts when no field baselines exist for a county."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Unknown County",
                        total=100,
                        judge=50,
                    ),
                ],
            }
        )
        alerts = check_field_completeness(conn, NOW, {})
        assert len(alerts) == 0

    def test_multiple_fields_multiple_severities(self) -> None:
        """Generates alerts for multiple fields with different severities."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=80,  # 95 -> 80 = 15pp drop (p1)
                        motion_type=83,  # 90 -> 83 = 7pp drop (p2)
                    ),
                ],
            }
        )
        field_baselines = {
            "Los Angeles": {
                "ruling": 100.0,
                "judge": 95.0,
                "motion_type": 90.0,
                "outcome": 90.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 80.0,
                "hearing_date": 100.0,
                "case_type": 85.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        severities = {a.severity for a in alerts}
        assert "p1" in severities
        assert "p2" in severities
        assert len(alerts) >= 2

    def test_alert_output_format(self) -> None:
        """Alert has correct metric, county, expected, actual fields."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        judge=80,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": {
                "judge": 95.0,
                "ruling": 100.0,
                "motion_type": 90.0,
                "outcome": 90.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 80.0,
                "hearing_date": 100.0,
                "case_type": 85.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        judge_alerts = [a for a in alerts if "judge" in a.message]
        assert len(judge_alerts) == 1
        alert = judge_alerts[0]
        assert alert.county == "Orange"
        assert alert.metric == "field_completeness"
        assert alert.expected == 95.0
        assert alert.actual == 80.0
        assert "15.0pp drop" in alert.message
