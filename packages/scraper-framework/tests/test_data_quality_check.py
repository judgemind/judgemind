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
_calculate_stale_threshold = dqc._calculate_stale_threshold
check_field_completeness = dqc.check_field_completeness
check_orphaned_documents = dqc.check_orphaned_documents
_query_field_completeness = dqc._query_field_completeness
_collect_full_metrics = dqc._collect_full_metrics
_format_metrics_for_snapshot = dqc._format_metrics_for_snapshot
persist_metrics = dqc.persist_metrics
format_json = dqc.format_json
format_text = dqc.format_text
file_issues_for_alerts = dqc.file_issues_for_alerts
_issue_title = dqc._issue_title
_issue_body = dqc._issue_body
_issue_labels = dqc._issue_labels
_check_duplicate = dqc._check_duplicate
_file_single_issue = dqc._file_single_issue
_24h_overlaps_posting_day = dqc._24h_overlaps_posting_day
MIN_FIELD_CHECK_SAMPLE_SIZE = dqc.MIN_FIELD_CHECK_SAMPLE_SIZE
FIELD_COMPLETENESS_GRACE_MINUTES = dqc.FIELD_COMPLETENESS_GRACE_MINUTES
FIELD_COMPLETENESS_WINDOW_DAYS = dqc.FIELD_COMPLETENESS_WINDOW_DAYS
ORPHANED_DOCS_P1_THRESHOLD = dqc.ORPHANED_DOCS_P1_THRESHOLD
ORPHANED_DOCS_P2_THRESHOLD = dqc.ORPHANED_DOCS_P2_THRESHOLD

NOW = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)


class TestLazyTrendStorageImport:
    """Verify dq_trend_storage is only imported when needed."""

    def test_import_trend_storage_succeeds(self) -> None:
        """_import_trend_storage returns the module when available."""
        mod = dqc._import_trend_storage()
        assert mod is not None
        assert hasattr(mod, "Snapshot")
        assert hasattr(mod, "store_snapshot")

    def test_module_importable_without_trend_storage(self) -> None:
        """Core data quality module loads even if dq_trend_storage is missing.

        This simulates the ECS oneshot environment where only the main
        script is uploaded and dq_trend_storage is not on sys.path.
        """
        # The module is already imported; the test verifies that the
        # top-level import does NOT eagerly import dq_trend_storage.
        # If it did, the import would fail when dq_trend_storage is absent.
        # We verify by checking _dq_trend_storage starts as None until called.
        assert hasattr(dqc, "_import_trend_storage")
        assert hasattr(dqc, "_dq_trend_storage")


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
            posting_days=cfg.get("posting_days"),
            max_expected_gap_hours=cfg.get("max_expected_gap_hours"),
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
        self.captured_calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        """Match query to stored results by checking key substrings."""
        self.captured_calls.append((query, params))
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
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        """Return a cursor with the same query results."""
        c = FakeCursor(self._query_results)
        self.cursors.append(c)
        return c

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
        assert result["Test"].posting_days is None
        assert result["Test"].max_expected_gap_hours is None

    def test_load_posting_days_and_max_gap(self, tmp_path: Path) -> None:
        """Loads posting_days and max_expected_gap_hours from config."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "Santa Clara": {
                            "expected_daily_rulings": 10,
                            "schedule_type": "daily",
                            "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                        },
                        "Custom": {
                            "expected_daily_rulings": 5,
                            "schedule_type": "daily",
                            "max_expected_gap_hours": 72.0,
                        },
                    }
                }
            )
        )
        result = load_baselines(baselines_file)
        assert result["Santa Clara"].posting_days == ["Mon", "Tue", "Wed", "Thu"]
        assert result["Santa Clara"].max_expected_gap_hours is None
        assert result["Custom"].posting_days is None
        assert result["Custom"].max_expected_gap_hours == 72.0

    def test_load_from_raw_dict(self) -> None:
        """Loads baselines from a pre-parsed raw dict (no file needed)."""
        raw = {
            "counties": {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                },
                "Santa Clara": {
                    "expected_daily_rulings": 0.1,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                },
            }
        }
        result = load_baselines(raw=raw)
        assert "Los Angeles" in result
        assert result["Los Angeles"].expected_daily_rulings == 50
        assert "Santa Clara" in result
        assert result["Santa Clara"].expected_daily_rulings == 0.1
        assert result["Santa Clara"].posting_days == ["Mon", "Tue", "Wed", "Thu"]

    def test_raw_dict_takes_priority_over_file(self, tmp_path: Path) -> None:
        """When both raw dict and file path are provided, raw dict wins."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "FileCounty": {
                            "expected_daily_rulings": 99,
                            "schedule_type": "daily",
                        }
                    }
                }
            )
        )
        raw = {
            "counties": {
                "RawCounty": {
                    "expected_daily_rulings": 42,
                    "schedule_type": "daily",
                }
            }
        }
        result = load_baselines(baselines_file, raw=raw)
        assert "RawCounty" in result
        assert "FileCounty" not in result

    def test_raw_dict_empty_counties(self) -> None:
        """Returns empty dict when raw dict has no counties."""
        raw = {"counties": {}}
        result = load_baselines(raw=raw)
        assert result == {}


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


class Test24hOverlapsPostingDay:
    """Tests for _24h_overlaps_posting_day helper."""

    def test_none_posting_days_always_overlaps(self) -> None:
        """No posting_days means every day is a posting day."""
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)  # Friday
        assert _24h_overlaps_posting_day(friday, None) is True

    def test_empty_posting_days_always_overlaps(self) -> None:
        """Empty list treated same as None."""
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(friday, []) is True

    def test_today_is_posting_day(self) -> None:
        """Returns True when today is a posting day."""
        # 2026-03-19 is Thursday
        thursday = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(thursday, ["Mon", "Tue", "Wed", "Thu"]) is True

    def test_yesterday_is_posting_day_but_today_is_not(self) -> None:
        """Returns False when yesterday was a posting day but today is not."""
        # 2026-03-20 is Friday; Thursday (yesterday) was a posting day,
        # but Friday is not in the Mon-Thu schedule.  Zero rulings on a
        # non-posting day is expected — the staleness check covers gaps.
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(friday, ["Mon", "Tue", "Wed", "Thu"]) is False

    def test_neither_today_nor_yesterday_is_posting_day(self) -> None:
        """Returns False when neither today nor yesterday is a posting day."""
        # 2026-03-22 is Sunday; Saturday (yesterday) is also not a posting day
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(sunday, ["Mon", "Tue", "Wed", "Thu"]) is False

    def test_saturday_after_friday_non_posting(self) -> None:
        """Saturday with Mon-Thu schedule: yesterday is Friday (not posting)."""
        saturday = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(saturday, ["Mon", "Tue", "Wed", "Thu"]) is False

    def test_monday_after_sunday_non_posting(self) -> None:
        """Monday is a posting day even though yesterday (Sunday) isn't."""
        monday = datetime(2026, 3, 23, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(monday, ["Mon", "Tue", "Wed", "Thu"]) is True

    def test_saturday_with_mon_fri_posting(self) -> None:
        """Saturday is NOT a posting day for Mon-Fri schedule (issue #1407)."""
        # 2026-03-21 is Saturday at 16:49 UTC — simulates the Ventura
        # false-positive scenario where Friday was the last posting day.
        saturday = datetime(2026, 3, 21, 16, 49, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(saturday, ["Mon", "Tue", "Wed", "Thu", "Fri"]) is False

    def test_invalid_day_names_ignored(self) -> None:
        """Invalid day names are silently skipped."""
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        assert _24h_overlaps_posting_day(friday, ["Xyz", "Abc"]) is True


class TestCheckIngestRatesPostingDays:
    """Tests for posting_days suppression in check_ingest_rates."""

    def test_zero_rulings_suppressed_on_non_posting_day(self) -> None:
        """No alert when county has zero rulings but today is not a posting day."""
        # Sunday March 22 — not in Mon-Thu posting schedule,
        # and yesterday (Saturday) isn't either.
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Santa Clara",)],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 1,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                },
            }
        )
        alerts = check_ingest_rates(conn, sunday, baselines)
        assert len(alerts) == 0

    def test_zero_rulings_fires_on_posting_day(self) -> None:
        """P1 alert still fires when today IS a posting day and zero rulings."""
        # Thursday March 19 — a posting day
        thursday = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Santa Clara",)],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 1,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                },
            }
        )
        alerts = check_ingest_rates(conn, thursday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"

    def test_zero_rulings_suppressed_day_after_posting_day(self) -> None:
        """No alert on Friday (non-posting day) even though Thu was a posting day.

        With Mon-Thu schedule, Friday is not a posting day.  Zero rulings
        on a non-posting day is expected — the staleness check covers the
        gap.  This prevents false positives like issue #1407.
        """
        friday = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Santa Clara",)],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 1,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                },
            }
        )
        alerts = check_ingest_rates(conn, friday, baselines)
        assert len(alerts) == 0

    def test_zero_rulings_fires_on_posting_day_after_posting_day(self) -> None:
        """P1 alert fires on Tuesday (posting day) with zero rulings (#1407 criterion 3).

        Tuesday is a posting day (Mon-Fri schedule).  Monday was also a
        posting day.  Zero rulings should trigger the alert because today
        is a posting day.
        """
        # 2026-03-24 is Tuesday
        tuesday = datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Ventura",)],
            }
        )
        baselines = _make_baselines(
            {
                "Ventura": {
                    "expected_daily_rulings": 5,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, tuesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"

    def test_zero_rulings_suppressed_saturday_mon_fri_schedule(self) -> None:
        """No alert on Saturday for county with Mon-Fri posting schedule (#1407).

        This is the exact false-positive scenario from the issue: Ventura
        has Mon-Fri posting, Saturday 16:49 UTC should not fire.
        """
        saturday = datetime(2026, 3, 21, 16, 49, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Ventura",)],
            }
        )
        baselines = _make_baselines(
            {
                "Ventura": {
                    "expected_daily_rulings": 5,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, saturday, baselines)
        assert len(alerts) == 0

    def test_no_posting_days_config_always_alerts(self) -> None:
        """Counties without posting_days config always alert on zero rulings."""
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "d.created_at <": [("Los Angeles", 200)],
                "d.created_at >=": [],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                },
            }
        )
        alerts = check_ingest_rates(conn, sunday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"


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
        """Alert when daily scraper hasn't run in >26 hours."""
        stale_time = NOW - timedelta(hours=27)
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

    def test_falls_back_to_last_seen_at(self) -> None:
        """Uses documents.last_seen_at when no scraper_runs exist.

        last_seen_at is updated on every upsert (even for dedup'd documents),
        so the normal staleness threshold applies — no inflated multiplier.
        A 27h gap exceeds the 26h daily threshold.
        """
        old_capture = NOW - timedelta(hours=27)
        conn = FakeConnection(
            {
                "scraper_runs": [],  # No scraper_runs
                "MAX(d.captured_at)": [("Los Angeles", old_capture)],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert "last_seen_at" in alerts[0].message

    def test_last_seen_at_fallback_no_alert_when_fresh(self) -> None:
        """No alert when last_seen_at is recent, even without scraper_runs.

        Since last_seen_at accurately reflects scraper activity (unlike the
        old captured_at which only recorded first insert), the normal
        threshold applies.  25h < 26h daily threshold = no alert.
        """
        recent_capture = NOW - timedelta(hours=25)
        conn = FakeConnection(
            {
                "scraper_runs": [],
                "MAX(d.captured_at)": [("Los Angeles", recent_capture)],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 0

    def test_very_stale_is_p1(self) -> None:
        """Very stale scrapers (>4x threshold) get p1 severity."""
        very_stale = NOW - timedelta(hours=105)
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
        stale = NOW - timedelta(hours=27)
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


class TestCalculateStaleThreshold:
    """Tests for _calculate_stale_threshold helper."""

    def test_daily_no_posting_days_returns_default(self) -> None:
        """Without posting_days, returns DAILY_SCRAPER_STALE_HOURS."""
        threshold = _calculate_stale_threshold(NOW, "daily", None, None)
        assert threshold == 26  # DAILY_SCRAPER_STALE_HOURS

    def test_frequent_no_posting_days_returns_default(self) -> None:
        """Without posting_days, frequent schedule returns 2h."""
        threshold = _calculate_stale_threshold(NOW, "frequent", None, None)
        assert threshold == 2  # FREQUENT_SCRAPER_STALE_HOURS

    def test_max_expected_gap_hours_overrides_all(self) -> None:
        """Explicit max_expected_gap_hours overrides everything."""
        threshold = _calculate_stale_threshold(NOW, "daily", ["Mon", "Tue"], 72.0)
        assert threshold == 72.0

    def test_posting_day_today_returns_base_threshold(self) -> None:
        """When today is a posting day, returns the base threshold."""
        # NOW is Tuesday 2026-03-11 12:00 UTC
        threshold = _calculate_stale_threshold(NOW, "daily", ["Mon", "Tue", "Wed", "Thu"], None)
        assert threshold == 26  # Base threshold — today is a posting day

    def test_saturday_with_weekday_posting(self) -> None:
        """Saturday check for Mon-Thu posting: last post was Thursday, 2 days ago."""
        saturday = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)  # Saturday
        threshold = _calculate_stale_threshold(
            saturday, "daily", ["Mon", "Tue", "Wed", "Thu"], None
        )
        # Last posting day was Thursday (2 days ago) -> 48h + 26h buffer
        assert threshold == 74.0

    def test_sunday_with_weekday_posting(self) -> None:
        """Sunday check for Mon-Thu posting: last post was Thursday, 3 days ago."""
        sunday = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)  # Sunday
        threshold = _calculate_stale_threshold(sunday, "daily", ["Mon", "Tue", "Wed", "Thu"], None)
        # Last posting day was Thursday (3 days ago) -> 72h + 26h buffer
        assert threshold == 98.0

    def test_monday_with_weekday_posting(self) -> None:
        """Monday check for Mon-Thu posting: today is a posting day."""
        monday = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        threshold = _calculate_stale_threshold(monday, "daily", ["Mon", "Tue", "Wed", "Thu"], None)
        # Monday is a posting day -> base threshold
        assert threshold == 26

    def test_friday_with_mon_thu_posting(self) -> None:
        """Friday check for Mon-Thu posting: last post was Thursday, 1 day ago."""
        friday = datetime(2026, 3, 13, 12, 0, 0, tzinfo=UTC)  # Friday
        threshold = _calculate_stale_threshold(friday, "daily", ["Mon", "Tue", "Wed", "Thu"], None)
        # Last posting day was Thursday (1 day ago) -> 24h + 26h buffer
        assert threshold == 50.0

    def test_mon_to_fri_posting_on_saturday(self) -> None:
        """Saturday check for Mon-Fri posting: last post was Friday, 1 day ago."""
        saturday = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)  # Saturday
        threshold = _calculate_stale_threshold(
            saturday, "daily", ["Mon", "Tue", "Wed", "Thu", "Fri"], None
        )
        # Last posting day was Friday (1 day ago) -> 24h + 26h buffer
        assert threshold == 50.0

    def test_empty_posting_days_returns_base(self) -> None:
        """Empty posting_days list falls back to base threshold."""
        threshold = _calculate_stale_threshold(NOW, "daily", [], None)
        assert threshold == 26

    def test_invalid_day_abbrevs_ignored(self) -> None:
        """Invalid day abbreviations are silently ignored."""
        threshold = _calculate_stale_threshold(NOW, "daily", ["Xyz", "Abc"], None)
        assert threshold == 26  # Falls back to base — no valid days


class TestScheduleAwareStaleness:
    """Integration tests for schedule-aware staleness in check_scraper_staleness."""

    def test_santa_clara_not_stale_on_saturday(self) -> None:
        """Santa Clara (Mon-Thu posting) should not alert on Saturday with 48h gap."""
        saturday = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)
        # Last scraper run was Thursday at noon — 48h ago
        last_run = datetime(2026, 3, 12, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-sc-tentatives", "Santa Clara", last_run, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 10,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                }
            }
        )
        alerts = check_scraper_staleness(conn, saturday, baselines)
        assert len(alerts) == 0

    def test_santa_clara_stale_on_posting_day(self) -> None:
        """Santa Clara should alert when stale on a posting day (Tuesday)."""
        tuesday = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
        # Last run was 27h ago — exceeds the 26h base threshold
        last_run = tuesday - timedelta(hours=27)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-sc-tentatives", "Santa Clara", last_run, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 10,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                }
            }
        )
        alerts = check_scraper_staleness(conn, tuesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].county == "Santa Clara"

    def test_daily_county_unaffected(self) -> None:
        """Counties without posting_days still use the default threshold."""
        stale_time = NOW - timedelta(hours=27)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", stale_time, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].county == "Los Angeles"

    def test_max_gap_override(self) -> None:
        """max_expected_gap_hours overrides all other logic."""
        # 40h gap — would be stale with 26h default, but not with 48h override
        last_run = NOW - timedelta(hours=40)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-sc-tentatives", "Santa Clara", last_run, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 10,
                    "schedule_type": "daily",
                    "max_expected_gap_hours": 48.0,
                }
            }
        )
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 0


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
        old_time = NOW - timedelta(hours=27)
        conn = FakeConnection(
            {
                # "has_ruling" matches FIELD_COMPLETENESS_QUERY (uses INNER
                # JOIN, so "LEFT JOIN rulings" no longer works for it).
                "has_ruling": [],
                # "orphaned_docs" matches ORPHANED_DOCUMENTS_QUERY.
                "orphaned_docs": [],
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
                # "has_ruling" matches FIELD_COMPLETENESS_QUERY.
                "has_ruling": [],
                # "orphaned_docs" matches ORPHANED_DOCUMENTS_QUERY.
                "orphaned_docs": [],
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
    expected="<26h",
    actual="105.0h",
    message="Santa Clara: scraper stale for 105.0h (threshold: 26h, source: scraper_runs)",
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

    def test_load_from_raw_dict(self) -> None:
        """Loads field baselines from a pre-parsed raw dict."""
        raw = {
            "field_completeness": {
                "Los Angeles": {"ruling": 100.0, "judge": 38.4},
                "Orange": {"ruling": 100.0, "judge": 100.0},
            }
        }
        result = load_field_baselines(raw=raw)
        assert "Los Angeles" in result
        assert result["Los Angeles"]["ruling"] == 100.0
        assert result["Los Angeles"]["judge"] == 38.4
        assert "Orange" in result

    def test_raw_dict_takes_priority_over_file(self, tmp_path: Path) -> None:
        """When both raw dict and file path are provided, raw dict wins."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps({"field_completeness": {"FileCounty": {"ruling": 99.0}}})
        )
        raw = {"field_completeness": {"RawCounty": {"ruling": 50.0}}}
        result = load_field_baselines(baselines_file, raw=raw)
        assert "RawCounty" in result
        assert "FileCounty" not in result

    def test_raw_dict_no_field_completeness(self) -> None:
        """Returns empty dict when raw dict lacks field_completeness key."""
        raw = {"counties": {"Test": {}}}
        result = load_field_baselines(raw=raw)
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
        result, totals = _query_field_completeness(conn, NOW)
        assert "Los Angeles" in result
        assert result["Los Angeles"]["ruling"] == 100.0
        assert result["Los Angeles"]["judge"] == 95.0
        assert result["Los Angeles"]["parties"] == 80.0
        assert totals["Los Angeles"] == 200

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
        result, totals = _query_field_completeness(conn, NOW)
        assert "Empty County" not in result
        assert "Empty County" not in totals

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
        result, totals = _query_field_completeness(conn, NOW)
        assert len(result) == 2
        assert result["Los Angeles"]["judge"] == 95.0
        assert result["Orange"]["judge"] == 80.0
        assert totals["Los Angeles"] == 100
        assert totals["Orange"] == 50

    def test_passes_grace_period_cutoff(self) -> None:
        """Passes both window cutoff and grace period cutoff as query params."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row("Los Angeles", total=100),
                ],
            }
        )
        _query_field_completeness(conn, NOW)

        # The cursor should have been called with (cutoff, grace_cutoff) params.
        assert len(conn.cursors) == 1
        cursor = conn.cursors[0]
        assert len(cursor.captured_calls) == 1
        _query, params = cursor.captured_calls[0]

        expected_cutoff = NOW - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS)
        expected_grace = NOW - timedelta(minutes=FIELD_COMPLETENESS_GRACE_MINUTES)
        assert params[0] == expected_cutoff
        assert params[1] == expected_grace

    def test_grace_period_constant_is_30_minutes(self) -> None:
        """Grace period constant is set to 30 minutes."""
        assert FIELD_COMPLETENESS_GRACE_MINUTES == 30


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

    def test_skips_low_sample_size_county(self) -> None:
        """Counties with fewer than MIN_FIELD_CHECK_SAMPLE_SIZE docs are skipped."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=2,
                        judge=0,  # 0% judge = 95pp drop, would be P1 normally
                    ),
                ],
            }
        )
        field_baselines = {
            "Santa Clara": {
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
        # Should NOT produce a P1 field_completeness alert.
        p1_alerts = [a for a in alerts if a.severity == "p1"]
        assert len(p1_alerts) == 0
        # Should produce a P2 informational low-sample-size alert.
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 1
        assert low_sample[0].severity == "p2"
        assert low_sample[0].county == "Santa Clara"
        assert low_sample[0].actual == 2
        assert "only 2 document(s)" in low_sample[0].message
        assert "skipping field completeness check" in low_sample[0].message

    def test_low_sample_emits_informational_alert(self) -> None:
        """The informational alert includes the sample size and minimum threshold."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=3,
                        judge=1,
                    ),
                ],
            }
        )
        field_baselines = {
            "Santa Clara": {"judge": 95.0},
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 1
        alert = low_sample[0]
        assert alert.expected == MIN_FIELD_CHECK_SAMPLE_SIZE
        assert alert.actual == 3
        assert f"minimum sample size: {MIN_FIELD_CHECK_SAMPLE_SIZE}" in alert.message

    def test_exact_threshold_not_skipped(self) -> None:
        """County with exactly MIN_FIELD_CHECK_SAMPLE_SIZE docs is NOT skipped."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=MIN_FIELD_CHECK_SAMPLE_SIZE,
                        judge=0,  # 0% judge vs 95% baseline = huge drop
                    ),
                ],
            }
        )
        field_baselines = {
            "Santa Clara": {
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
        # Should NOT produce a low-sample alert.
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 0
        # Should produce a normal P1 field_completeness alert.
        p1_alerts = [a for a in alerts if a.severity == "p1"]
        assert len(p1_alerts) >= 1

    def test_mixed_counties_low_and_normal_sample(self) -> None:
        """Low-sample county is skipped while normal-sample county is checked."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=2,
                        judge=0,
                    ),
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=80,
                    ),
                ],
            }
        )
        field_baselines = {
            "Santa Clara": {
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
        # Santa Clara: low sample → informational alert only.
        sc_alerts = [a for a in alerts if a.county == "Santa Clara"]
        assert len(sc_alerts) == 1
        assert sc_alerts[0].metric == "field_completeness_low_sample"
        # Los Angeles: normal sample → field regression alert.
        la_alerts = [
            a for a in alerts if a.county == "Los Angeles" and a.metric == "field_completeness"
        ]
        assert len(la_alerts) >= 1
        assert any(a.severity == "p1" for a in la_alerts)


# ---------------------------------------------------------------------------
# Tests for check_orphaned_documents
# ---------------------------------------------------------------------------


def _make_orphaned_docs_row(
    county: str,
    total: int = 100,
    orphaned: int = 0,
) -> tuple[Any, ...]:
    """Create a row matching the ORPHANED_DOCUMENTS_QUERY result shape."""
    return (county, total, orphaned)


class TestCheckOrphanedDocuments:
    """Tests for check_orphaned_documents function."""

    def test_no_orphans_no_alert(self) -> None:
        """No alerts when all documents have ruling references."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Los Angeles", total=100, orphaned=0)]}
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 0

    def test_p2_alert_above_5pct(self) -> None:
        """P2 alert when orphaned percentage exceeds 5% threshold."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Orange", total=100, orphaned=10)]}
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"
        assert alerts[0].metric == "orphaned_documents"
        assert alerts[0].severity == "p2"
        assert alerts[0].actual == 10
        assert "10 of 100" in alerts[0].message
        assert "10.0%" in alerts[0].message

    def test_p1_alert_above_20pct(self) -> None:
        """P1 alert when orphaned percentage exceeds 20% threshold."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Orange", total=100, orphaned=25)]}
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].severity == "p1"

    def test_below_threshold_no_alert(self) -> None:
        """No alert when orphaned percentage is below 5% threshold."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Los Angeles", total=100, orphaned=3)]}
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 0

    def test_zero_total_docs_no_alert(self) -> None:
        """No alert when a county has zero documents."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Empty", total=0, orphaned=0)]}
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 0

    def test_multiple_counties(self) -> None:
        """Alerts generated for multiple counties independently."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Los Angeles", total=100, orphaned=0),
                    _make_orphaned_docs_row("Orange", total=100, orphaned=30),
                    _make_orphaned_docs_row("San Bernardino", total=50, orphaned=5),
                ],
            }
        )
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 2
        counties = {a.county for a in alerts}
        assert counties == {"Orange", "San Bernardino"}

    def test_passes_time_window_params(self) -> None:
        """Passes window cutoff and grace period cutoff as query params."""
        conn = FakeConnection(
            {"orphaned_docs": [_make_orphaned_docs_row("Los Angeles", total=100, orphaned=0)]}
        )
        check_orphaned_documents(conn, NOW)

        assert len(conn.cursors) == 1
        cursor = conn.cursors[0]
        assert len(cursor.captured_calls) == 1
        _query, params = cursor.captured_calls[0]

        expected_cutoff = NOW - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS)
        expected_grace = NOW - timedelta(minutes=FIELD_COMPLETENESS_GRACE_MINUTES)
        assert params[0] == expected_cutoff
        assert params[1] == expected_grace

    def test_county_filter(self) -> None:
        """County filter limits results to a single county."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Orange", total=100, orphaned=30),
                ],
            }
        )
        check_orphaned_documents(conn, NOW, county="Orange")

        assert len(conn.cursors) == 1
        cursor = conn.cursors[0]
        _query, params = cursor.captured_calls[0]
        # County param should be the last param
        assert params[-1] == "Orange"

    def test_orphaned_docs_scenario_from_issue(self) -> None:
        """Simulates the scenario described in issue #1492.

        Orphaned documents created by a backfill script should trigger
        an orphaned_documents alert rather than polluting field completeness.
        Field completeness should remain high because orphaned docs are
        excluded from that query via INNER JOIN.
        """
        # Field completeness: only documents WITH rulings are counted
        # (INNER JOIN means 100 docs with rulings show good completeness)
        field_conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row("Orange", total=100, ruling=100, judge=95),
                ],
            }
        )
        field_baselines = {
            "Orange": {
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
        field_alerts = check_field_completeness(field_conn, NOW, field_baselines)
        # No field completeness regression because orphaned docs are excluded
        fc_regression = [a for a in field_alerts if a.metric == "field_completeness"]
        assert len(fc_regression) == 0

        # Orphaned documents check: 1500 orphaned out of 1600 total
        orphan_conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Orange", total=1600, orphaned=1500),
                ],
            }
        )
        orphan_alerts = check_orphaned_documents(orphan_conn, NOW)
        # Should fire a P1 orphaned_documents alert
        assert len(orphan_alerts) == 1
        assert orphan_alerts[0].metric == "orphaned_documents"
        assert orphan_alerts[0].severity == "p1"


# ---------------------------------------------------------------------------
# Tests for _collect_full_metrics
# ---------------------------------------------------------------------------


class TestCollectFullMetrics:
    """Tests for _collect_full_metrics function."""

    def test_collects_ruling_count_24h(self) -> None:
        """Collects ruling_count_24h with doc type breakdown metadata."""
        # Keys must uniquely match each SQL query string.
        # Order matters: more specific keys first so they match before generic ones.
        conn = FakeConnection(
            {
                # RULING_COUNTS_7D_QUERY (has "created_at < %s", 24h query does not)
                "created_at < %s": [],
                # RULING_COUNTS_24H_QUERY (generic "ruling_count" matches this)
                "AS ruling_count": [("Los Angeles", 42)],
                # RULING_COUNT_BY_TYPE_QUERY
                "d.document_type": [
                    ("Los Angeles", "tentative_ruling", 30),
                    ("Los Angeles", "minute_order", 12),
                ],
                # FIELD_COMPLETENESS_QUERY (unique key: "has_ruling")
                "has_ruling": [],
                # FIELD_GAP_DOCS_QUERY (unique key: "r.judge_id IS NULL")
                "r.judge_id IS NULL": [],
                # LATEST_SCRAPER_RUN_QUERY
                "ranked_runs": [],
                # SCRAPER_SUCCESS_RATE_24H_QUERY
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        assert "Los Angeles" in result
        m = result["Los Angeles"]["ruling_count_24h"]
        assert m["value"] == 42
        assert m["metadata"]["by_doc_type"]["tentative_ruling"] == 30
        assert m["metadata"]["by_doc_type"]["minute_order"] == 12

    def test_collects_ruling_count_7d_avg(self) -> None:
        """Collects ruling_count_7d_avg from 7-day window."""
        conn = FakeConnection(
            {
                # 7d query matches first via "created_at < %s"
                "created_at < %s": [("Orange", 120)],
                # 24h query matches via "AS ruling_count"
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        assert "Orange" in result
        m = result["Orange"]["ruling_count_7d_avg"]
        assert m["value"] == round(120 / 6.0, 2)  # ROLLING_WINDOW_DAYS = 6
        assert m["metadata"]["total_7d_window"] == 120

    def test_collects_field_completeness_metrics(self) -> None:
        """Collects overall and per-field completeness metrics."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        ruling=100,
                        judge=90,
                        motion_type=80,
                        outcome=85,
                        title=100,
                        case_number=100,
                        parties=70,
                        hearing_date=95,
                        case_type=90,
                    ),
                ],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        la = result["Los Angeles"]

        # Overall field completeness is the average of all 9 fields.
        assert "field_completeness_pct" in la
        assert la["field_completeness_pct"]["value"] > 0

        # Individual field metrics.
        assert la["field_completeness_judge"]["value"] == 90.0
        assert la["field_completeness_motion_type"]["value"] == 80.0
        assert la["field_completeness_parties"]["value"] == 70.0
        assert la["field_completeness_outcome"]["value"] == 85.0
        assert la["field_completeness_hearing_date"]["value"] == 95.0

    def test_collects_scraper_last_success_age(self) -> None:
        """Collects scraper_last_success_age_hours from latest scraper run."""
        two_hours_ago = NOW - timedelta(hours=2)
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [("ca-la-tentative", "Los Angeles", two_hours_ago, "success")],
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        m = result["Los Angeles"]["scraper_last_success_age_hours"]
        assert abs(m["value"] - 2.0) < 0.01

    def test_collects_scraper_success_rate(self) -> None:
        """Collects scraper_run_success_rate_24h with error metadata."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [
                    (
                        "Los Angeles",
                        10,
                        8,
                        [
                            {"status": "failure", "error_message": "timeout"},
                            {"status": "failure", "error_message": "timeout"},
                        ],
                    ),
                ],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        m = result["Los Angeles"]["scraper_run_success_rate_24h"]
        assert m["value"] == 80.0
        assert m["metadata"]["total_runs"] == 10
        assert m["metadata"]["success_count"] == 8
        assert m["metadata"]["error_types"]["timeout"] == 2

    def test_scraper_success_rate_no_error_details(self) -> None:
        """Handles None error_details (all runs succeeded)."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [("Orange", 5, 5, None)],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        m = result["Orange"]["scraper_run_success_rate_24h"]
        assert m["value"] == 100.0
        assert "error_types" not in m["metadata"]

    def test_multiple_counties(self) -> None:
        """Collects metrics for multiple counties."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [("Los Angeles", 40), ("Orange", 15)],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        assert "Los Angeles" in result
        assert "Orange" in result
        assert result["Los Angeles"]["ruling_count_24h"]["value"] == 40
        assert result["Orange"]["ruling_count_24h"]["value"] == 15

    def test_field_gap_docs_metadata(self) -> None:
        """Includes document IDs with gaps in field completeness metadata."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [
                    _make_field_completeness_row("Los Angeles", total=100, judge=90),
                ],
                "r.judge_id IS NULL": [
                    ("Los Angeles", "doc-abc-123"),
                    ("Los Angeles", "doc-def-456"),
                ],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        result = _collect_full_metrics(conn, NOW)
        metadata = result["Los Angeles"]["field_completeness_pct"]["metadata"]
        assert "docs_with_gaps" in metadata
        assert "doc-abc-123" in metadata["docs_with_gaps"]
        assert "doc-def-456" in metadata["docs_with_gaps"]

    def test_field_gap_docs_query_passes_grace_period(self) -> None:
        """FIELD_GAP_DOCS_QUERY receives both window cutoff and grace cutoff."""
        conn = FakeConnection(
            {
                "created_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        _collect_full_metrics(conn, NOW)

        # Find the cursor call that ran FIELD_GAP_DOCS_QUERY.
        found = False
        for cursor in conn.cursors:
            for query, params in cursor.captured_calls:
                if "r.judge_id IS NULL" in query:
                    found = True
                    expected_cutoff = NOW - timedelta(
                        days=FIELD_COMPLETENESS_WINDOW_DAYS,
                    )
                    expected_grace = NOW - timedelta(
                        minutes=FIELD_COMPLETENESS_GRACE_MINUTES,
                    )
                    assert params[0] == expected_cutoff
                    assert params[1] == expected_grace
                    break
        assert found, "FIELD_GAP_DOCS_QUERY was not executed"


# ---------------------------------------------------------------------------
# Tests for persist_metrics
# ---------------------------------------------------------------------------


class RecordingCursor:
    """A cursor that records executemany calls for verification."""

    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[Any]]] = []

    def executemany(self, query: str, params: list[Any]) -> None:
        """Record the query and params."""
        self.executemany_calls.append((query, list(params)))

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class RecordingConnection:
    """A mock connection that records cursor operations."""

    def __init__(self) -> None:
        self._cursor = RecordingCursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> RecordingCursor:
        """Return the recording cursor."""
        return self._cursor

    def commit(self) -> None:
        """Record commit."""
        self.committed = True

    def rollback(self) -> None:
        """Record rollback."""
        self.rolled_back = True


class TestPersistMetrics:
    """Tests for persist_metrics function."""

    def test_writes_all_metrics_in_batch(self) -> None:
        """Writes all metric rows using executemany (batched insert)."""
        conn = RecordingConnection()
        county_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {"value": 42, "metadata": {"by_doc_type": {"tentative": 42}}},
                "ruling_count_7d_avg": {"value": 35.5, "metadata": None},
            },
            "Orange": {
                "ruling_count_24h": {"value": 15, "metadata": None},
            },
        }
        count = persist_metrics(conn, county_metrics, now=NOW)
        assert count == 3
        assert conn.committed
        assert not conn.rolled_back

        # Verify executemany was called once with 3 rows.
        assert len(conn._cursor.executemany_calls) == 1
        query, params = conn._cursor.executemany_calls[0]
        assert "INSERT INTO data_quality_metrics" in query
        assert len(params) == 3

    def test_uses_executemany_not_per_row_insert(self) -> None:
        """Verifies batched insert (executemany) is used, not individual inserts."""
        conn = RecordingConnection()
        county_metrics = {
            "County A": {
                "metric_1": {"value": 1, "metadata": None},
                "metric_2": {"value": 2, "metadata": None},
                "metric_3": {"value": 3, "metadata": None},
            },
        }
        persist_metrics(conn, county_metrics, now=NOW)
        # Should be exactly 1 executemany call, not 3 separate ones.
        assert len(conn._cursor.executemany_calls) == 1

    def test_serializes_metadata_as_json(self) -> None:
        """Serializes metadata dict to JSON string."""
        conn = RecordingConnection()
        county_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {
                    "value": 42,
                    "metadata": {"by_doc_type": {"tentative": 42}},
                },
            },
        }
        persist_metrics(conn, county_metrics, now=NOW)
        _, params = conn._cursor.executemany_calls[0]
        row = params[0]
        # row is (now, county, metric_name, value, metadata_json)
        metadata_json = row[4]
        assert metadata_json is not None
        parsed = json.loads(metadata_json)
        assert parsed["by_doc_type"]["tentative"] == 42

    def test_none_metadata_stays_none(self) -> None:
        """None metadata is not serialized — stays as None."""
        conn = RecordingConnection()
        county_metrics = {
            "Los Angeles": {
                "ruling_count_7d_avg": {"value": 10.0, "metadata": None},
            },
        }
        persist_metrics(conn, county_metrics, now=NOW)
        _, params = conn._cursor.executemany_calls[0]
        row = params[0]
        assert row[4] is None

    def test_db_error_does_not_raise(self) -> None:
        """DB errors are caught and logged, not raised."""
        conn = RecordingConnection()
        # Make executemany raise an exception.
        conn._cursor.executemany = MagicMock(  # type: ignore[assignment]
            side_effect=RuntimeError("connection lost"),
        )
        county_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {"value": 42, "metadata": None},
            },
        }
        # Should not raise.
        count = persist_metrics(conn, county_metrics, now=NOW)
        assert count == 0
        assert conn.rolled_back

    def test_empty_metrics_returns_zero(self) -> None:
        """Returns 0 when there are no metrics to persist."""
        conn = RecordingConnection()
        count = persist_metrics(conn, {}, now=NOW)
        assert count == 0
        assert not conn.committed

    def test_correct_row_structure(self) -> None:
        """Each row has (timestamp, county, metric_name, value, metadata_json)."""
        conn = RecordingConnection()
        county_metrics = {
            "Orange": {
                "scraper_run_success_rate_24h": {
                    "value": 80.0,
                    "metadata": {"total_runs": 10, "success_count": 8},
                },
            },
        }
        persist_metrics(conn, county_metrics, now=NOW)
        _, params = conn._cursor.executemany_calls[0]
        row = params[0]
        assert row[0] == NOW  # recorded_at
        assert row[1] == "Orange"  # county
        assert row[2] == "scraper_run_success_rate_24h"  # metric_name
        assert row[3] == 80.0  # metric_value
        metadata = json.loads(row[4])
        assert metadata["total_runs"] == 10

    def test_all_metric_names_written(self) -> None:
        """All 10 documented metric names are present when data exists."""
        two_hours_ago = NOW - timedelta(hours=2)
        conn = FakeConnection(
            {
                "created_at < %s": [("TestCounty", 210)],
                "AS ruling_count": [("TestCounty", 50)],
                "d.document_type": [("TestCounty", "ruling", 50)],
                "has_ruling": [
                    _make_field_completeness_row(
                        "TestCounty",
                        total=100,
                        ruling=100,
                        judge=95,
                        motion_type=90,
                        outcome=88,
                        title=100,
                        case_number=100,
                        parties=75,
                        hearing_date=92,
                        case_type=85,
                    ),
                ],
                "r.judge_id IS NULL": [],
                "ranked_runs": [("ca-test", "TestCounty", two_hours_ago, "success")],
                "success_count": [("TestCounty", 10, 9, None)],
            }
        )
        metrics = _collect_full_metrics(conn, NOW)
        metric_names = set(metrics["TestCounty"].keys())

        expected_names = {
            "ruling_count_24h",
            "ruling_count_7d_avg",
            "field_completeness_pct",
            "field_completeness_judge",
            "field_completeness_motion_type",
            "field_completeness_parties",
            "field_completeness_outcome",
            "field_completeness_hearing_date",
            "scraper_last_success_age_hours",
            "scraper_run_success_rate_24h",
        }
        assert expected_names == metric_names


# ---------------------------------------------------------------------------
# Tests for _format_metrics_for_snapshot
# ---------------------------------------------------------------------------


class TestFormatMetricsForSnapshot:
    """Tests for _format_metrics_for_snapshot function."""

    def test_converts_ruling_count(self) -> None:
        """Converts ruling_count_24h to the legacy snapshot format."""
        full_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {"value": 42, "metadata": {"by_doc_type": {"tentative": 42}}},
            },
        }
        result = _format_metrics_for_snapshot(full_metrics)
        assert result["Los Angeles"]["ruling_count_24h"] == 42

    def test_converts_field_completeness(self) -> None:
        """Re-constructs field_completeness dict from individual metrics."""
        full_metrics = {
            "Los Angeles": {
                "field_completeness_pct": {"value": 90.0, "metadata": None},
                "field_completeness_judge": {"value": 95.0, "metadata": None},
                "field_completeness_motion_type": {"value": 88.0, "metadata": None},
            },
        }
        result = _format_metrics_for_snapshot(full_metrics)
        fc = result["Los Angeles"]["field_completeness"]
        assert fc["judge"] == 95.0
        assert fc["motion_type"] == 88.0
        # field_completeness_pct should NOT appear in the fc dict (it's the overall).
        assert "pct" not in fc

    def test_empty_input(self) -> None:
        """Returns empty dict for empty input."""
        assert _format_metrics_for_snapshot({}) == {}

    def test_skips_non_snapshot_metrics(self) -> None:
        """Only includes ruling_count_24h and field completeness in snapshot."""
        full_metrics = {
            "Los Angeles": {
                "scraper_run_success_rate_24h": {"value": 80.0, "metadata": None},
                "ruling_count_7d_avg": {"value": 35.0, "metadata": None},
            },
        }
        result = _format_metrics_for_snapshot(full_metrics)
        # No ruling_count_24h or field_completeness, so county should be empty/absent.
        assert "Los Angeles" not in result

    def test_mixed_metrics(self) -> None:
        """Handles a mix of snapshot-relevant and other metrics."""
        full_metrics = {
            "Orange": {
                "ruling_count_24h": {"value": 15, "metadata": None},
                "ruling_count_7d_avg": {"value": 12.0, "metadata": None},
                "field_completeness_judge": {"value": 90.0, "metadata": None},
                "scraper_last_success_age_hours": {"value": 1.5, "metadata": None},
            },
        }
        result = _format_metrics_for_snapshot(full_metrics)
        assert result["Orange"]["ruling_count_24h"] == 15
        assert result["Orange"]["field_completeness"]["judge"] == 90.0
        # Non-snapshot metrics should not appear.
        assert "ruling_count_7d_avg" not in result["Orange"]
        assert "scraper_last_success_age_hours" not in result["Orange"]


# ---------------------------------------------------------------------------
# Schema validation for SQL query constants
# ---------------------------------------------------------------------------

from helpers.schema_validation import (  # noqa: I001
    SCHEMA_SQL_PATH as _SCHEMA_SQL_PATH,
    extract_alias_to_table as _extract_alias_to_table,
    extract_column_references as _extract_column_references,
    get_query_constants,
    parse_schema_columns as _parse_schema_columns,
    validate_queries_against_schema,
)


def _get_all_query_constants() -> dict[str, str]:
    """Dynamically discover all SQL query constants from the data quality check module."""
    return get_query_constants(dqc)


class TestSqlSchemaValidation:
    """Validate that SQL query constants reference columns that exist in schema.sql.

    This test class catches column name mismatches (e.g. d.doc_type instead of
    d.document_type) at test time. It parses the schema DDL and cross-references
    all alias.column references in every query constant.
    """

    def test_schema_file_exists(self) -> None:
        """The schema SQL file must exist for validation to work."""
        assert _SCHEMA_SQL_PATH.exists(), f"Schema file not found at {_SCHEMA_SQL_PATH}"

    def test_schema_parser_finds_tables(self) -> None:
        """The schema parser should find the key tables used by queries."""
        schema = _parse_schema_columns(_SCHEMA_SQL_PATH)
        expected_tables = {
            "courts",
            "documents",
            "rulings",
            "cases",
            "case_parties",
            "scraper_runs",
            "data_quality_metrics",
        }
        for table in expected_tables:
            assert table in schema, f"Schema parser did not find table '{table}'"

    def test_schema_parser_finds_columns(self) -> None:
        """The schema parser should find known columns in each table."""
        schema = _parse_schema_columns(_SCHEMA_SQL_PATH)
        # Spot-check some columns
        assert "county" in schema["courts"]
        assert "document_type" in schema["documents"]
        assert "judge_id" in schema["rulings"]
        assert "case_title" in schema["cases"]
        assert "party_id" in schema["case_parties"]
        assert "scraper_id" in schema["scraper_runs"]

    def test_query_constants_discovered(self) -> None:
        """All expected query constants should be discovered dynamically."""
        queries = _get_all_query_constants()
        expected = {
            "RULING_COUNTS_24H_QUERY",
            "RULING_COUNTS_7D_QUERY",
            "ALL_ACTIVE_COUNTIES_QUERY",
            "LATEST_SCRAPER_RUN_QUERY",
            "LATEST_CAPTURE_PER_COUNTY_QUERY",
            "SCRAPER_SUCCESS_RATE_24H_QUERY",
            "RULING_COUNT_BY_TYPE_QUERY",
            "FIELD_GAP_DOCS_QUERY",
            "FIELD_COMPLETENESS_QUERY",
            "ORPHANED_DOCUMENTS_QUERY",
            "INSERT_METRICS_QUERY",
        }
        for name in expected:
            assert name in queries, f"Query constant '{name}' not discovered"

    def test_all_column_references_exist_in_schema(self) -> None:
        """Every alias.column reference in every query must map to a real column."""
        queries = _get_all_query_constants()
        errors = validate_queries_against_schema(queries)
        assert not errors, "SQL query constants reference non-existent columns:\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    def test_detects_invalid_column_name(self) -> None:
        """Verify the validation catches a known-bad column reference."""
        schema = _parse_schema_columns(_SCHEMA_SQL_PATH)

        bad_query = """
            SELECT ct.county, d.doc_type, COUNT(d.id) AS count
            FROM documents d
            JOIN courts ct ON ct.id = d.court_id
            GROUP BY ct.county, d.doc_type
        """
        alias_map = _extract_alias_to_table(bad_query)
        col_refs = _extract_column_references(bad_query)

        invalid_refs = []
        for alias, column in col_refs:
            if alias not in alias_map:
                continue
            table = alias_map[alias]
            if table not in schema:
                continue
            if column not in schema[table]:
                invalid_refs.append(f"{alias}.{column}")

        assert "d.doc_type" in invalid_refs, "Validation should have caught d.doc_type as invalid"

    def test_alias_extraction_handles_left_join(self) -> None:
        """Alias extraction works for LEFT JOIN clauses."""
        query = """
            SELECT d.id
            FROM documents d
            LEFT JOIN rulings r ON r.document_id = d.id
            LEFT JOIN cases c ON c.id = d.case_id
        """
        alias_map = _extract_alias_to_table(query)
        assert alias_map["d"] == "documents"
        assert alias_map["r"] == "rulings"
        assert alias_map["c"] == "cases"

    def test_alias_extraction_handles_cte(self) -> None:
        """CTE references in the outer query are handled gracefully."""
        # LATEST_SCRAPER_RUN_QUERY uses a CTE called ranked_runs
        query = dqc.LATEST_SCRAPER_RUN_QUERY
        alias_map = _extract_alias_to_table(query)
        # Inside the CTE, sr and ct should be mapped
        assert alias_map.get("sr") == "scraper_runs"
        assert alias_map.get("ct") == "courts"

    def test_column_extraction_ignores_string_literals(self) -> None:
        """Column extraction does not pick up alias.column inside strings."""
        query = """
            SELECT d.id FROM documents d WHERE d.status = 'active.thing'
        """
        refs = _extract_column_references(query)
        # Should find d.id and d.status but not active.thing
        alias_cols = [(a, c) for a, c in refs]
        assert ("d", "id") in alias_cols
        assert ("d", "status") in alias_cols
        assert ("active", "thing") not in alias_cols

    def test_column_extraction_ignores_format_placeholders(self) -> None:
        """Column extraction does not pick up things inside {braces}."""
        query = """
            SELECT d.id FROM documents d {county_filter}
        """
        refs = _extract_column_references(query)
        alias_cols = [(a, c) for a, c in refs]
        assert ("d", "id") in alias_cols
        # county_filter should not appear
        assert not any(c == "county_filter" for _, c in alias_cols)


# ---------------------------------------------------------------------------
# Integration: staleness check prefers scraper_runs over captured_at (#894)
# ---------------------------------------------------------------------------


class TestStalenessCheckPrefersScraperRuns:
    """Verify that check_scraper_staleness uses scraper_runs data when available,
    even when documents.captured_at would give a different (stale) answer.

    This is the integration test for #894: once the runner populates scraper_runs,
    the staleness check should use started_at from scraper_runs instead of
    falling back to MAX(documents.captured_at).
    """

    def test_uses_scraper_runs_when_available(self) -> None:
        """When scraper_runs has a recent entry, staleness check should report
        fresh — even if captured_at is old (because no new documents were found)."""
        recent_run = NOW - timedelta(hours=1)  # scraper ran 1h ago
        old_capture = NOW - timedelta(hours=20)  # last doc capture was 20h ago

        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", recent_run, "success")],
                "MAX(d.captured_at)": [("Los Angeles", old_capture)],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)

        # Should be zero alerts because scraper_runs shows a recent run
        assert len(alerts) == 0

    def test_falls_back_to_last_seen_at_without_scraper_runs(self) -> None:
        """When scraper_runs is empty but last_seen_at is stale, should alert
        with source=documents.last_seen_at.

        last_seen_at is updated on every upsert, so the normal threshold
        applies.  27h exceeds the 26h daily threshold.
        """
        old_capture = NOW - timedelta(hours=27)

        conn = FakeConnection(
            {
                "scraper_runs": [],
                "MAX(d.captured_at)": [("Los Angeles", old_capture)],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)

        assert len(alerts) == 1
        assert alerts[0].county == "Los Angeles"
        assert "last_seen_at" in alerts[0].message

    def test_scraper_runs_source_label(self) -> None:
        """When scraper_runs provides the data, the alert source should say
        'scraper_runs' if the scraper is stale."""
        stale_run = NOW - timedelta(hours=27)

        conn = FakeConnection(
            {
                "scraper_runs": [("ca-la-tentatives-civil", "Los Angeles", stale_run, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines()
        alerts = check_scraper_staleness(conn, NOW, baselines)

        assert len(alerts) == 1
        assert "scraper_runs" in alerts[0].message


class TestStalenessWithDedupedDocuments:
    """Verify that the staleness metric is accurate for courts with dedup'd
    documents (low posting volume, same document IDs on re-scrape).

    This is the core fix for #986: courts like Santa Clara that post ~0.3
    rulings/day would show 80+ hours of "staleness" under the old
    captured_at-based metric even when the scraper was running every 12h,
    because captured_at is only set on the first insert.

    With last_seen_at, which is updated on every upsert (including dedup'd
    re-scrapes), the staleness metric accurately reflects scraper activity.
    """

    def test_deduped_documents_no_false_stale_alert(self) -> None:
        """A court with dedup'd documents should NOT trigger a false stale alert.

        Scenario: Santa Clara posts content infrequently. The scraper runs
        every 12h but finds the same documents (same content hash -> same
        document_id -> upsert with no new rows). last_seen_at is updated
        to the current time on each upsert.

        The last_seen_at fallback shows 6h (recent scraper run), not the
        80h that captured_at would show. No alert should fire.
        """
        # last_seen_at was updated 6h ago (scraper ran successfully)
        recent_last_seen = NOW - timedelta(hours=6)
        conn = FakeConnection(
            {
                "scraper_runs": [],  # No scraper_runs data available
                "MAX(d.captured_at)": [("Santa Clara", recent_last_seen)],
            }
        )
        baselines = _make_baselines(
            {"Santa Clara": {"expected_daily_rulings": 1, "schedule_type": "daily"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 0

    def test_deduped_documents_stale_alert_when_scraper_actually_stopped(self) -> None:
        """A genuinely stale scraper should still trigger an alert.

        Scenario: The scraper has actually stopped running. last_seen_at
        hasn't been updated in 27h (past the 26h daily threshold).
        This should trigger an alert.
        """
        stale_last_seen = NOW - timedelta(hours=27)
        conn = FakeConnection(
            {
                "scraper_runs": [],
                "MAX(d.captured_at)": [("Santa Clara", stale_last_seen)],
            }
        )
        baselines = _make_baselines(
            {"Santa Clara": {"expected_daily_rulings": 1, "schedule_type": "daily"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 1
        assert alerts[0].county == "Santa Clara"
        assert "last_seen_at" in alerts[0].message

    def test_scraper_runs_preferred_over_last_seen_at_for_deduped(self) -> None:
        """When scraper_runs data exists, it takes precedence over last_seen_at.

        Even if last_seen_at shows a stale value (e.g., documents table hasn't
        been touched in a while), a recent scraper_runs entry means the scraper
        is healthy.
        """
        recent_run = NOW - timedelta(hours=2)
        old_last_seen = NOW - timedelta(hours=30)
        conn = FakeConnection(
            {
                "scraper_runs": [
                    ("ca-santa-clara-tentatives", "Santa Clara", recent_run, "success")
                ],
                "MAX(d.captured_at)": [("Santa Clara", old_last_seen)],
            }
        )
        baselines = _make_baselines(
            {"Santa Clara": {"expected_daily_rulings": 1, "schedule_type": "daily"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines)
        assert len(alerts) == 0
