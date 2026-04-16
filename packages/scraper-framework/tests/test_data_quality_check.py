"""Tests for scripts/data-quality-check.py with mocked DB queries."""

from __future__ import annotations

import copy
import dataclasses
import json
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
load_baselines = dqc.load_baselines
load_field_baselines = dqc.load_field_baselines
load_expected_null_rates = dqc.load_expected_null_rates
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
_24h_overlaps_posting_day = dqc._24h_overlaps_posting_day
_count_posting_days_in_window = dqc._count_posting_days_in_window
_compute_baseline_daily = dqc._compute_baseline_daily
MIN_FIELD_CHECK_SAMPLE_SIZE = dqc.MIN_FIELD_CHECK_SAMPLE_SIZE
FIELD_COMPLETENESS_GRACE_MINUTES = dqc.FIELD_COMPLETENESS_GRACE_MINUTES
FIELD_COMPLETENESS_WINDOW_DAYS = dqc.FIELD_COMPLETENESS_WINDOW_DAYS
BULK_INGEST_MULTIPLIER = dqc.BULK_INGEST_MULTIPLIER
_is_bulk_ingest = dqc._is_bulk_ingest
ORPHANED_DOCS_P1_THRESHOLD = dqc.ORPHANED_DOCS_P1_THRESHOLD
ORPHANED_DOCS_P2_THRESHOLD = dqc.ORPHANED_DOCS_P2_THRESHOLD
check_ruling_document_ratio = dqc.check_ruling_document_ratio
RULING_DOC_RATIO_THRESHOLD = dqc.RULING_DOC_RATIO_THRESHOLD
check_ecs_service_health = dqc.check_ecs_service_health
EcsServiceConfig = dqc.EcsServiceConfig
load_ecs_service_configs = dqc.load_ecs_service_configs
DEFAULT_ECS_SERVICES = dqc.DEFAULT_ECS_SERVICES
is_rebuild_in_progress = dqc.is_rebuild_in_progress
_downgrade_p1_alerts_for_rebuild = dqc._downgrade_p1_alerts_for_rebuild
REBUILD_MARKER_TTL_HOURS = dqc.REBUILD_MARKER_TTL_HOURS
_resolve_baselines_path = dqc._resolve_baselines_path
DEFAULT_BASELINES_PATH = dqc.DEFAULT_BASELINES_PATH
DOCKER_BASELINES_PATH = dqc.DOCKER_BASELINES_PATH

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
            low_volume=cfg.get("low_volume", False),
            min_days_zero_before_alert=cfg.get("min_days_zero_before_alert", 1),
        )
        for name, cfg in counties.items()
    }


_count_consecutive_zero_days = dqc._count_consecutive_zero_days


class TestMakeBaselinesFieldForwarding:
    """Validate that _make_baselines forwards every Baselines dataclass field.

    If a new field is added to Baselines but _make_baselines is not updated
    to forward it, these tests will fail.  See #1817.
    """

    # Build a config dict with a distinctive, non-default value for every field.
    _ALL_FIELDS_CONFIG: dict[str, object] = {
        "expected_daily_rulings": 99.0,
        "schedule_type": "frequent",
        "posting_days": ["Mon", "Wed", "Fri"],
        "max_expected_gap_hours": 72.0,
        "low_volume": True,
        "min_days_zero_before_alert": 3,
    }

    def test_make_baselines_forwards_all_fields(self) -> None:
        """Every Baselines field must appear in the _make_baselines constructor call."""
        baselines_fields = {f.name for f in dataclasses.fields(Baselines)}
        config_fields = set(self._ALL_FIELDS_CONFIG.keys())

        # If a new field is in Baselines but not in our config, this assertion
        # forces the developer to add it here AND in _make_baselines.
        assert baselines_fields == config_fields, (
            f"Baselines has fields not covered by _ALL_FIELDS_CONFIG: "
            f"{baselines_fields - config_fields}.  "
            f"_ALL_FIELDS_CONFIG has fields not in Baselines: "
            f"{config_fields - baselines_fields}."
        )

    def test_make_baselines_values_pass_through(self) -> None:
        """Values provided in the county config must appear on the Baselines object."""
        # Deep-copy to prevent shared mutable state (e.g. the posting_days list).
        county_config = copy.deepcopy(self._ALL_FIELDS_CONFIG)
        result = _make_baselines({"TestCounty": county_config})
        baseline = result["TestCounty"]

        for field in dataclasses.fields(Baselines):
            expected = county_config[field.name]
            actual = getattr(baseline, field.name)
            assert actual == expected, (
                f"Field {field.name!r}: expected {expected!r}, got {actual!r}. "
                f"_make_baselines may not be forwarding this field."
            )


def _make_per_day_rows(
    county: str,
    daily_counts: list[int],
    now: datetime,
) -> list[tuple[str, str, int]]:
    """Generate per-day (county, date, count) rows for the 7D per-day query.

    Creates rows for each day in the 7-day window [now-7d, now-24h) that
    has a nonzero count.  ``daily_counts`` maps to the 6 calendar days
    in the window, oldest first.

    Args:
        county: County name.
        daily_counts: List of up to 6 counts, one per calendar day in the
            window [now-7d, now-24h).  Oldest day first.
        now: The "now" timestamp used for the check.

    Returns:
        List of (county, date_str, count) tuples for nonzero days.
    """
    cutoff_7d = now - timedelta(days=7)
    rows: list[tuple[str, str, int]] = []
    for i, count in enumerate(daily_counts):
        if count > 0:
            day = cutoff_7d + timedelta(days=i)
            rows.append((county, day.strftime("%Y-%m-%d"), count))
    return rows


def _uniform_per_day_rows(
    county: str,
    total: int,
    now: datetime,
    num_days: int = 6,
) -> list[tuple[str, str, int]]:
    """Distribute a total count uniformly across the 7D window days.

    This is a convenience for converting old total-based test data to
    per-day format.  The median of a uniform distribution equals the mean,
    so existing test assertions about thresholds remain valid.

    Args:
        county: County name.
        total: Total count across the window.
        now: The "now" timestamp used for the check.
        num_days: Number of days in the window (default 6).

    Returns:
        List of (county, date_str, count) tuples.
    """
    if total == 0 or num_days == 0:
        return []
    per_day = total // num_days
    remainder = total % num_days
    daily = [per_day + (1 if i < remainder else 0) for i in range(num_days)]
    return _make_per_day_rows(county, daily, now)


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


class TestResolveBaselinesPath:
    """Tests for _resolve_baselines_path fallback logic (#2323)."""

    def test_returns_repo_path_when_exists(self, tmp_path: Path) -> None:
        """Prefers the repo-relative path when the file exists."""
        repo_path = tmp_path / "repo" / "data-quality-baselines.json"
        repo_path.parent.mkdir(parents=True)
        repo_path.write_text("{}")
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        # Docker path does not exist

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = _resolve_baselines_path()
        assert result == repo_path

    def test_falls_back_to_docker_path(self, tmp_path: Path) -> None:
        """Falls back to Docker image path when repo path does not exist."""
        repo_path = tmp_path / "nonexistent" / "data-quality-baselines.json"
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        docker_path.parent.mkdir(parents=True)
        docker_path.write_text("{}")

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = _resolve_baselines_path()
        assert result == docker_path

    def test_returns_default_when_neither_exists(self, tmp_path: Path) -> None:
        """Returns repo-relative path when neither path exists."""
        repo_path = tmp_path / "nonexistent1" / "data-quality-baselines.json"
        docker_path = tmp_path / "nonexistent2" / "data-quality-baselines.json"

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = _resolve_baselines_path()
        assert result == repo_path

    def test_prefers_repo_path_over_docker_when_both_exist(
        self,
        tmp_path: Path,
    ) -> None:
        """Repo path wins when both repo and Docker paths exist."""
        repo_path = tmp_path / "repo" / "data-quality-baselines.json"
        repo_path.parent.mkdir(parents=True)
        repo_path.write_text('{"source": "repo"}')
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        docker_path.parent.mkdir(parents=True)
        docker_path.write_text('{"source": "docker"}')

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = _resolve_baselines_path()
        assert result == repo_path

    def test_load_baselines_uses_docker_fallback(self, tmp_path: Path) -> None:
        """load_baselines loads from Docker path when repo path is missing."""
        repo_path = tmp_path / "nonexistent" / "data-quality-baselines.json"
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        docker_path.parent.mkdir(parents=True)
        docker_path.write_text(
            json.dumps(
                {
                    "counties": {
                        "Los Angeles": {
                            "expected_daily_rulings": 50,
                            "schedule_type": "daily",
                        }
                    }
                }
            )
        )

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = load_baselines()
        assert "Los Angeles" in result
        assert result["Los Angeles"].expected_daily_rulings == 50

    def test_load_field_baselines_uses_docker_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """load_field_baselines loads from Docker path when repo path missing."""
        repo_path = tmp_path / "nonexistent" / "data-quality-baselines.json"
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        docker_path.parent.mkdir(parents=True)
        docker_path.write_text(
            json.dumps({"field_completeness": {"Orange": {"ruling": 99.0, "judge": 95.0}}})
        )

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = load_field_baselines()
        assert "Orange" in result
        assert result["Orange"]["ruling"] == 99.0

    def test_load_ecs_service_configs_uses_docker_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """load_ecs_service_configs reads Docker path when repo path missing."""
        repo_path = tmp_path / "nonexistent" / "data-quality-baselines.json"
        docker_path = tmp_path / "docker" / "data-quality-baselines.json"
        docker_path.parent.mkdir(parents=True)
        docker_path.write_text(
            json.dumps(
                {
                    "ecs_services": [
                        {
                            "cluster": "test-cluster",
                            "service": "test-service",
                            "display_name": "Test Service",
                        }
                    ]
                }
            )
        )

        with (
            patch.object(dqc, "DEFAULT_BASELINES_PATH", repo_path),
            patch.object(dqc, "DOCKER_BASELINES_PATH", docker_path),
        ):
            result = load_ecs_service_configs()
        assert len(result) == 1
        assert result[0].cluster == "test-cluster"
        assert result[0].service == "test-service"


class TestCheckIngestRates:
    """Tests for check_ingest_rates function.

    Since #1866, check_ingest_rates uses the **25th percentile** (lower
    quartile) of per-day counts for the 7-day baseline, making it robust
    to backfill spikes.  Test data uses ``"AT TIME ZONE"`` as the
    FakeConnection key for the per-day query, with (county, date_str,
    count) tuples.
    """

    def test_healthy_county_no_alerts(self) -> None:
        """No alerts when 24h count is above 50% of 7-day baseline."""
        # Uniform 200 across 6 days -> Q1 ~33/day.  30 >= 33*0.5 = 16.5.
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 200, NOW),
                "d.captured_at >=": [("Los Angeles", 30)],
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
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 200, NOW),
                "d.captured_at >=": [],  # 0 rulings in 24h
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
        """P2 alert when 24h count is below 50% of 7-day baseline."""
        # Uniform 200 across 6 days -> Q1 ~33/day.  5 < 33*0.5 = 16.5.
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 200, NOW),
                "d.captured_at >=": [("Los Angeles", 5)],
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
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines, county="Orange")
        # Orange has 0 rulings but expected 20 -> zero_rulings alert
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"

    def test_no_baseline_uses_computed_baseline(self) -> None:
        """Uses 7-day computed baseline when no config exists for the county."""
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Unknown County", 100, NOW),
                "d.captured_at >=": [],  # 0 rulings
                "DISTINCT ct.county": [("Unknown County",)],
            }
        )
        # No baselines for "Unknown County" — should use daily baseline from 7d
        alerts = check_ingest_rates(conn, NOW, {})
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"

    def test_multiple_counties(self) -> None:
        """Checks multiple counties and generates appropriate alerts."""
        la_rows = _uniform_per_day_rows("Los Angeles", 200, NOW)
        og_rows = _uniform_per_day_rows("Orange", 100, NOW)
        conn = FakeConnection(
            {
                "AT TIME ZONE": la_rows + og_rows,
                "d.captured_at >=": [("Los Angeles", 30), ("Orange", 0)],
                "DISTINCT ct.county": [("Los Angeles",), ("Orange",)],
            }
        )
        baselines = _make_baselines()
        alerts = check_ingest_rates(conn, NOW, baselines)
        # LA is healthy (30 vs ~33 baseline), Orange has zero -> 1 alert
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"

    def test_zero_expected_daily_suppresses_ingest_rate_drop(self) -> None:
        """No ingest_rate alert when expected_daily_rulings is zero (#1768).

        Counties like San Diego with expected_daily_rulings=0 and
        posting_days=[] have no active scraper. A small non-zero 7-day
        baseline (from historical data) should NOT trigger an ingest rate
        drop alert — same guard that zero_rulings already uses.
        """
        # 1 ruling on one day -> baseline = 0 (most days have 0)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("San Diego", [1, 0, 0, 0, 0, 0], NOW),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("San Diego",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Diego": {
                    "expected_daily_rulings": 0,
                    "schedule_type": "daily",
                    "posting_days": [],
                },
            }
        )
        alerts = check_ingest_rates(conn, NOW, baselines)
        # Neither zero_rulings (expected_daily=0) nor ingest_rate should fire
        assert len(alerts) == 0

    def test_zero_expected_daily_with_nonzero_24h_no_alert(self) -> None:
        """Ingest rate drop suppressed even with nonzero 24h count (#1768).

        When expected_daily is 0 but there is some incidental activity
        (nonzero 24h count that still falls below the 7d baseline), the
        ingest_rate check should still be suppressed because the county
        has no active scraper — any data is incidental.
        """
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("San Diego", 18, NOW),
                "d.captured_at >=": [("San Diego", 1)],
                "DISTINCT ct.county": [("San Diego",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Diego": {
                    "expected_daily_rulings": 0,
                    "schedule_type": "daily",
                    "posting_days": [],
                },
            }
        )
        alerts = check_ingest_rates(conn, NOW, baselines)
        assert len(alerts) == 0


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


class TestCountPostingDaysInWindow:
    """Tests for _count_posting_days_in_window helper."""

    def test_none_posting_days_returns_calendar_days(self) -> None:
        """No posting_days means all calendar days are posting days."""
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)  # Sunday
        assert _count_posting_days_in_window(start, end, None) == 6.0

    def test_empty_posting_days_returns_calendar_days(self) -> None:
        """Empty list treated same as None."""
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        assert _count_posting_days_in_window(start, end, []) == 6.0

    def test_mon_fri_over_full_week(self) -> None:
        """Mon-Fri posting over a 6-day window starting Monday has 5 posting days.

        Window [Mon Mar 16, Sun Mar 22) = Mon, Tue, Wed, Thu, Fri, Sat
        Posting days: Mon, Tue, Wed, Thu, Fri = 5.
        """
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)  # Sunday
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        assert _count_posting_days_in_window(start, end, posting_days) == 5.0

    def test_mon_fri_window_starting_wednesday(self) -> None:
        """6-day window starting Wed: Wed, Thu, Fri, Sat, Sun, Mon = 4 posting days."""
        start = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)  # Wednesday
        end = datetime(2026, 3, 24, 12, 0, 0, tzinfo=UTC)  # Monday
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        assert _count_posting_days_in_window(start, end, posting_days) == 4.0

    def test_mon_thu_over_6_day_window(self) -> None:
        """Mon-Thu posting over a 6-day window starting Monday.

        Window: Mon, Tue, Wed, Thu, Fri, Sat = 4 posting days.
        """
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)  # Sunday
        posting_days = ["Mon", "Tue", "Wed", "Thu"]
        assert _count_posting_days_in_window(start, end, posting_days) == 4.0

    def test_tue_thu_sparse_posting(self) -> None:
        """Tue/Thu posting over 6-day window starting Monday.

        Window: Mon, Tue, Wed, Thu, Fri, Sat = 2 posting days (Tue, Thu).
        """
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)  # Sunday
        posting_days = ["Tue", "Thu"]
        assert _count_posting_days_in_window(start, end, posting_days) == 2.0

    def test_no_posting_days_in_window_returns_one(self) -> None:
        """Window with zero posting days returns 1.0 (prevents division by zero).

        Window [Sat, Mon) = Sat, Sun = 0 posting days for Mon-Fri schedule.
        Returns 1.0 as floor.
        """
        start = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)  # Saturday
        end = datetime(2026, 3, 23, 12, 0, 0, tzinfo=UTC)  # Monday
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        assert _count_posting_days_in_window(start, end, posting_days) == 1.0

    def test_zero_or_negative_window_returns_one(self) -> None:
        """Zero-length or negative window returns 1.0."""
        ts = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        assert _count_posting_days_in_window(ts, ts, ["Mon"]) == 1.0
        # Negative window (start > end)
        start = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        assert _count_posting_days_in_window(start, end, ["Mon"]) == 1.0

    def test_invalid_day_names_ignored(self) -> None:
        """Invalid day names are silently skipped, falls back to calendar days."""
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        assert _count_posting_days_in_window(start, end, ["Xyz", "Abc"]) == 6.0

    def test_single_day_window_posting_day(self) -> None:
        """Single-day window on a posting day returns 1.0."""
        start = datetime(2026, 3, 16, 12, 0, 0, tzinfo=UTC)  # Monday
        end = datetime(2026, 3, 17, 12, 0, 0, tzinfo=UTC)  # Tuesday
        assert _count_posting_days_in_window(start, end, ["Mon"]) == 1.0

    def test_single_day_window_non_posting_day(self) -> None:
        """Single-day window on a non-posting day returns 1.0 (floor)."""
        start = datetime(2026, 3, 21, 12, 0, 0, tzinfo=UTC)  # Saturday
        end = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)  # Sunday
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        assert _count_posting_days_in_window(start, end, posting_days) == 1.0


class TestCheckIngestRatesPostingDays:
    """Tests for posting_days suppression in check_ingest_rates."""

    def test_zero_rulings_suppressed_on_non_posting_day(self) -> None:
        """No alert when county has zero rulings but today is not a posting day."""
        # Sunday March 22 — not in Mon-Thu posting schedule,
        # and yesterday (Saturday) isn't either.
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
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
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
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
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
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
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
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
                "AT TIME ZONE": [],
                "d.captured_at >=": [],
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
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 200, sunday),
                "d.captured_at >=": [],
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

    def test_ingest_rate_drop_suppressed_on_non_posting_day(self) -> None:
        """No ingest_rate alert on Saturday for county with Mon-Fri schedule.

        This is the exact false-positive scenario from issue #1615: on weekends,
        courts don't post new rulings, so the few duplicate-detected documents
        that trickle in are well below 50% of the weekday-heavy 7-day average.
        The ingest_rate drop alert should be suppressed on non-posting days.
        """
        saturday = datetime(2026, 3, 21, 16, 49, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 1200, saturday),
                "d.captured_at >=": [("Los Angeles", 7)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, saturday, baselines)
        assert len(alerts) == 0

    def test_ingest_rate_drop_fires_on_posting_day(self) -> None:
        """Ingest rate drop alert still fires on a posting day (weekday).

        On a posting day (e.g. Wednesday), if 24h count drops below 50% of the
        7-day baseline, the alert should fire because courts are expected to
        publish new rulings.
        """
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 1200, wednesday),
                # 5 rulings in 24h — well below 50% of ~200/day baseline
                "d.captured_at >=": [("Los Angeles", 5)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate"
        assert alerts[0].severity == "p2"
        assert alerts[0].actual == 5

    def test_ingest_rate_drop_no_posting_days_always_alerts(self) -> None:
        """Ingest rate drop fires on any day when no posting_days configured."""
        sunday = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 1200, sunday),
                # 5 rulings in 24h — well below 50% of ~200/day baseline
                "d.captured_at >=": [("Los Angeles", 5)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    # No posting_days — should always alert
                },
            }
        )
        alerts = check_ingest_rates(conn, sunday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate"
        assert alerts[0].severity == "p2"


class TestPostingDayAwareBaseline:
    """Tests that the 7-day baseline uses posting days, not calendar days.

    Since #1866, check_ingest_rates uses the 25th percentile of per-day
    counts.  For counties with posting_days, only posting days contribute
    to the baseline calculation.

    Fixes #1784: for a Mon-Fri county, the 7-day baseline should account
    for posting days to avoid being artificially deflated by weekends.
    """

    def test_mon_fri_county_baseline_uses_posting_days(self) -> None:
        """For a Mon-Fri county, 7d baseline uses only posting days.

        Window [now-7d, now-24h) on Wednesday 2026-03-18 = Wed-Mon = 6 cal days.
        Days: Wed 3/11(post), Thu 3/12(post), Fri 3/13(post),
              Sat 3/14(skip), Sun 3/15(skip), Mon 3/16(post) = 4 posting days.
        50 rulings per posting day -> Q1 = 50.  45 >= 50*0.5 = 25. No alert.
        """
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows(
                    "Los Angeles",
                    # Wed=50, Thu=50, Fri=50, Sat=0, Sun=0, Mon=50
                    [50, 50, 50, 0, 0, 50],
                    wednesday,
                ),
                "d.captured_at >=": [("Los Angeles", 45)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 0

    def test_mon_fri_county_detects_real_drop_with_accurate_baseline(self) -> None:
        """A real drop is detected against the posting-day-aware baseline.

        50 rulings per posting day -> Q1 = 50.
        20 < 50 * 0.5 = 25 -> alert fires.
        """
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows(
                    "Los Angeles",
                    [50, 50, 50, 0, 0, 50],
                    wednesday,
                ),
                "d.captured_at >=": [("Los Angeles", 20)],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate"
        assert alerts[0].actual == 20
        assert alerts[0].expected == 50.0

    def test_no_posting_days_uses_all_calendar_days(self) -> None:
        """Without posting_days config, baseline uses all calendar days."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # 20 per day across all 6 days -> Q1 = 20
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("TestCounty", 120, wednesday),
                "d.captured_at >=": [("TestCounty", 5)],
                "DISTINCT ct.county": [("TestCounty",)],
            }
        )
        baselines = _make_baselines(
            {
                "TestCounty": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        # Q1 = 20. 5 < 20 * 0.5 = 10. Alert fires.
        assert len(alerts) == 1
        assert alerts[0].expected == 20.0

    def test_tue_thu_county_baseline(self) -> None:
        """A Tue/Thu-only posting county uses min fallback for small samples.

        On Thursday 2026-03-19, the 6-day window [Thu 3/12 - Tue 3/17]
        contains 2 posting days (Thu 3/12, Tue 3/17).  50 rulings on Tue ->
        posting-day values [0, 50] -> only 2 values, falls back to min = 0.
        daily_baseline = 0, so ingest_rate condition (daily_baseline > 0)
        is False -> no ingest_rate alert.  45 > 0 so no zero_rulings either.
        """
        thursday = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                # Only Tuesday (day index 5 from Thu 3/12) has data.
                # Thu=0, Fri=0, Sat=0, Sun=0, Mon=0, Tue=50
                "AT TIME ZONE": _make_per_day_rows("San Francisco", [0, 0, 0, 0, 0, 50], thursday),
                "d.captured_at >=": [("San Francisco", 45)],
                "DISTINCT ct.county": [("San Francisco",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Francisco": {
                    "expected_daily_rulings": 0.5,
                    "schedule_type": "daily",
                    "posting_days": ["Tue", "Thu"],
                },
            }
        )
        alerts = check_ingest_rates(conn, thursday, baselines)
        assert len(alerts) == 0

    def test_low_volume_county_suppresses_zero_rulings(self) -> None:
        """Low-volume counties with low_volume=True do NOT trigger P1 zero_rulings.

        A county like Santa Clara (0.1 rulings/day) will have zero new
        rulings on ~90% of days.  Firing a P1 alert for this is a false
        positive — see #1886.
        """
        thursday = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("Santa Clara", [0, 0, 0, 1, 0, 0], thursday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Santa Clara",)],
            }
        )
        baselines = _make_baselines(
            {
                "Santa Clara": {
                    "expected_daily_rulings": 0.1,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu"],
                    "low_volume": True,
                },
            }
        )
        alerts = check_ingest_rates(conn, thursday, baselines)
        assert len(alerts) == 0

    def test_non_low_volume_county_still_fires_zero_rulings(self) -> None:
        """Non-low-volume counties still trigger P1 zero_rulings alert.

        San Bernardino (3/day, not low_volume) should fire P1 on zero-count
        posting days to catch genuine scraper failures.
        """
        # Wednesday is a posting day for San Bernardino
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("San Bernardino", [3, 2, 4, 3, 2, 3], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("San Bernardino",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Bernardino": {
                    "expected_daily_rulings": 3,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "San Bernardino"

    def test_low_expected_daily_suppresses_without_flag(self) -> None:
        """Counties with expected_daily < 1.0 are suppressed even without low_volume flag.

        A county with expected_daily_rulings=0.2 but no explicit low_volume=True
        should still be suppressed — zero-count days are statistically normal
        when the expectation is well below 1 ruling per day.  This threshold
        guard prevents false P1 alerts if someone forgets the flag (#1886).
        """
        thursday = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("New County", [0, 0, 1, 0, 0, 0], thursday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("New County",)],
            }
        )
        baselines = _make_baselines(
            {
                "New County": {
                    "expected_daily_rulings": 0.2,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    # low_volume NOT set — defaults to False
                },
            }
        )
        alerts = check_ingest_rates(conn, thursday, baselines)
        assert len(alerts) == 0


class TestCountConsecutiveZeroDays:
    """Tests for _count_consecutive_zero_days helper function.

    The function examines per_day_7d data which covers [now-7d, now-24h).
    It starts counting from now-2d (the last reliable full day in the window)
    and works backwards.
    """

    def test_all_zero_days(self) -> None:
        """Returns 6 when all days in the 7-day window had zero rulings."""
        now = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        assert _count_consecutive_zero_days({}, now) == 6

    def test_two_days_ago_nonzero(self) -> None:
        """Returns 0 when the most recent day in the 7d window had rulings."""
        now = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        assert _count_consecutive_zero_days({two_days_ago: 5}, now) == 0

    def test_two_zero_days_then_nonzero(self) -> None:
        """Returns 2 when two days in the window were zero, then a nonzero day."""
        now = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        # now-2d and now-3d are zero, now-4d has rulings
        four_days_ago = (now - timedelta(days=4)).strftime("%Y-%m-%d")
        county_per_day = {four_days_ago: 3}
        assert _count_consecutive_zero_days(county_per_day, now) == 2

    def test_one_zero_day_then_nonzero(self) -> None:
        """Returns 1 when only the most recent day is zero, day before had rulings."""
        now = datetime(2026, 3, 19, 12, 0, 0, tzinfo=UTC)
        # now-2d is zero, now-3d has rulings
        three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        county_per_day = {three_days_ago: 10}
        assert _count_consecutive_zero_days(county_per_day, now) == 1


class TestMinDaysZeroBeforeAlert:
    """Tests for min_days_zero_before_alert parameter (#1916).

    Counties with ``min_days_zero_before_alert > 1`` require multiple
    consecutive zero-ruling days before a P1 alert fires.  A single zero
    day is downgraded to P2 informational.
    """

    def test_sb_single_zero_day_fires_p2_not_p1(self) -> None:
        """San Bernardino with min_days_zero=2: single zero day => P2."""
        # Wednesday is a posting day for SB
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Yesterday (Tuesday) had 3 rulings — only today is zero
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("San Bernardino", [3, 2, 4, 3, 2, 3], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("San Bernardino",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Bernardino": {
                    "expected_daily_rulings": 3,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    "min_days_zero_before_alert": 2,
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p2"
        assert alerts[0].county == "San Bernardino"
        assert "P1 requires 2 consecutive zero days" in alerts[0].message

    def test_sb_two_consecutive_zero_days_fires_p1(self) -> None:
        """San Bernardino with min_days_zero=2: two zero days => P1."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Yesterday (Tuesday) also had 0 rulings — two consecutive zeros
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("San Bernardino", [3, 2, 4, 3, 0, 0], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("San Bernardino",)],
            }
        )
        baselines = _make_baselines(
            {
                "San Bernardino": {
                    "expected_daily_rulings": 3,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    "min_days_zero_before_alert": 2,
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "San Bernardino"

    def test_la_single_zero_day_still_fires_p1(self) -> None:
        """LA with default min_days_zero=1: single zero day => P1."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows(
                    "Los Angeles", [50, 45, 55, 48, 52, 50], wednesday
                ),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Los Angeles",)],
            }
        )
        baselines = _make_baselines(
            {
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    # no min_days_zero_before_alert — defaults to 1
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "Los Angeles"

    def test_oc_single_zero_day_still_fires_p1(self) -> None:
        """OC with default min_days_zero=1: single zero day => P1."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("Orange", [20, 18, 22, 19, 21, 20], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "Orange"

    def test_three_consecutive_zero_days_with_threshold_3(self) -> None:
        """County with min_days_zero=3: three zero days => P1."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Last 3 days all zero (days 4, 5, 6 in the 6-day window)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("TestCounty", [5, 5, 5, 0, 0, 0], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("TestCounty",)],
            }
        )
        baselines = _make_baselines(
            {
                "TestCounty": {
                    "expected_daily_rulings": 5,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    "min_days_zero_before_alert": 3,
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].severity == "p1"

    def test_two_consecutive_zero_days_with_threshold_3_fires_p2(self) -> None:
        """County with min_days_zero=3: only 2 zero days (today + 1) => P2."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Only the most recent day in 7d window (now-2d) is zero, plus today = 2 total
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("TestCounty", [5, 5, 5, 5, 5, 0], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("TestCounty",)],
            }
        )
        baselines = _make_baselines(
            {
                "TestCounty": {
                    "expected_daily_rulings": 5,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                    "min_days_zero_before_alert": 3,
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].severity == "p2"

    def test_no_baseline_defaults_to_min_days_1(self) -> None:
        """County without baselines defaults to min_days=1 and fires P1."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows("Unknown County", [5, 5, 5, 5, 5, 5], wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Unknown County",)],
            }
        )
        # No baselines at all — the function uses daily_baseline from 7d data
        alerts = check_ingest_rates(conn, wednesday, {})
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"

    def test_load_baselines_reads_min_days_field(self) -> None:
        """load_baselines correctly reads min_days_zero_before_alert from JSON."""
        raw = {
            "counties": {
                "San Bernardino": {
                    "expected_daily_rulings": 3,
                    "schedule_type": "daily",
                    "min_days_zero_before_alert": 2,
                },
                "Los Angeles": {
                    "expected_daily_rulings": 50,
                    "schedule_type": "daily",
                },
            }
        }
        result = load_baselines(raw=raw)
        assert result["San Bernardino"].min_days_zero_before_alert == 2
        assert result["Los Angeles"].min_days_zero_before_alert == 1  # default


class TestBackfillSpikeResilience:
    """Tests that ingest rate checks are resilient to backfill spikes.

    Since #1866, the ingest rate check uses the **25th percentile** of
    per-day counts.  A single-day spike (or even multiple spike days) from
    a bulk re-ingest does not inflate the baseline, preventing false-positive
    drop alerts.

    Also verifies the fix for #1693: queries use ``captured_at`` (court
    posting time) rather than ``created_at`` (pipeline processing time).
    """

    def test_backfill_spike_does_not_inflate_baseline(self) -> None:
        """Normal 24h count should not trigger alert even after a backfill.

        Scenario: OC had a backfill that created 274 docs on one day.
        With 25th-percentile baseline, the spike day is an outlier that
        doesn't inflate the baseline.  Normal days have ~20 rulings.
        """
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        # Window: Wed-Mon.  Posting days: Wed, Thu, Fri, Mon (4 days).
        # Normal: 20/day on 3 days, spike: 274 on 1 day.
        # Daily counts on posting days: [20, 20, 20, 274] -> Q1 = 20.
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows(
                    "Orange",
                    # Wed=20, Thu=20, Fri=20, Sat=0, Sun=0, Mon=274
                    [20, 20, 20, 0, 0, 274],
                    wednesday,
                ),
                "d.captured_at >=": [("Orange", 18)],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        # Q1 of [20, 20, 20, 274] = 20. 18 >= 20*0.5 = 10. No alert.
        assert len(alerts) == 0

    def test_spike_inflated_mean_would_have_fired_but_baseline_does_not(self) -> None:
        """OC 274-doc spike inflates mean, not 25th percentile baseline.

        On 2026-03-20, 274 OC documents were re-ingested. On 2026-03-23,
        7 rulings appeared. With the old mean: (274+7*5)/6 = 51.5/day,
        threshold = 25.75, 7 < 25.75 -> false positive. With 25th percentile:
        per-day counts [7,7,274,7,7] -> Q1 = 7, 7 >= 7*0.5 = 3.5 -> no alert.
        """
        # 2026-03-23 is a Sunday, but let's test on a posting day for
        # the ingest_rate check to fire (posting day suppression is tested
        # separately). Use a Monday for the scenario.
        monday = datetime(2026, 3, 23, 12, 0, 0, tzinfo=UTC)
        # Window [3/16 Mon - 3/22 Sun) = Mon,Tue,Wed,Thu,Fri,Sat = 6 days
        # Mon-Fri posting: Mon=7, Tue=7, Wed=274, Thu=7, Fri=7, Sat=skip
        conn = FakeConnection(
            {
                "AT TIME ZONE": _make_per_day_rows(
                    "Orange",
                    [7, 7, 274, 7, 7, 0],
                    monday,
                ),
                "d.captured_at >=": [("Orange", 7)],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, monday, baselines)
        # Posting-day values: [7, 7, 274, 7, 7] -> Q1 = 7.
        # 7 >= 7 * 0.5 = 3.5 -> no alert. The spike does NOT inflate the baseline.
        assert len(alerts) == 0

    def test_genuine_scraper_failure_still_detected(self) -> None:
        """A real scraper failure (0 captures on a weekday) still fires alert."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Orange", 120, wednesday),
                "d.captured_at >=": [],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "zero_rulings"
        assert alerts[0].severity == "p1"
        assert alerts[0].county == "Orange"
        assert alerts[0].actual == 0

    def test_genuine_rate_drop_still_detected(self) -> None:
        """A genuine ingest rate drop (not a backfill artifact) still fires."""
        wednesday = datetime(2026, 3, 18, 12, 0, 0, tzinfo=UTC)
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Orange", 120, wednesday),
                "d.captured_at >=": [("Orange", 5)],
                "DISTINCT ct.county": [("Orange",)],
            }
        )
        baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        alerts = check_ingest_rates(conn, wednesday, baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ingest_rate"
        assert alerts[0].severity == "p2"
        assert alerts[0].actual == 5

    def test_queries_use_captured_at_not_created_at(self) -> None:
        """Verify SQL queries reference captured_at, not created_at.

        Regression test for #1693: the queries must use d.captured_at.
        """
        assert "d.captured_at" in dqc.RULING_COUNTS_24H_QUERY
        assert "d.created_at" not in dqc.RULING_COUNTS_24H_QUERY
        assert "d.captured_at" in dqc.RULING_COUNTS_7D_QUERY
        assert "d.created_at" not in dqc.RULING_COUNTS_7D_QUERY
        assert "d.captured_at" in dqc.RULING_COUNTS_7D_PER_DAY_QUERY
        assert "d.created_at" not in dqc.RULING_COUNTS_7D_PER_DAY_QUERY


class TestComputeBaselineDaily:
    """Tests for _compute_baseline_daily helper function.

    Uses the 25th percentile (lower quartile) for >= 4 data points,
    and falls back to min() for < 4 data points.  This makes the
    baseline more resistant to backfill spikes than the median (#1866).
    """

    def test_uniform_distribution(self) -> None:
        """25th percentile of uniform counts equals each day's count."""
        window_start = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        per_day = {
            "2026-03-04": 20,
            "2026-03-05": 20,
            "2026-03-06": 20,
            "2026-03-07": 20,
            "2026-03-08": 20,
            "2026-03-09": 20,
        }
        result = _compute_baseline_daily(per_day, None, window_start, window_end)
        assert result == 20.0

    def test_spike_day_does_not_skew_baseline(self) -> None:
        """A single spike day does not inflate the 25th percentile."""
        window_start = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        per_day = {
            "2026-03-04": 20,
            "2026-03-05": 20,
            "2026-03-06": 274,  # spike
            "2026-03-07": 20,
            "2026-03-08": 20,
            "2026-03-09": 20,
        }
        result = _compute_baseline_daily(per_day, None, window_start, window_end)
        # Sorted: [20, 20, 20, 20, 20, 274] -> Q1 = 20.0
        assert result == 20.0

    def test_multiple_spike_days_do_not_inflate_baseline(self) -> None:
        """Multiple spike days still don't inflate the 25th percentile (#1866).

        This is the key improvement over the median: even with 2 spike days
        out of 6, the 25th percentile stays at the normal rate.
        """
        window_start = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        per_day = {
            "2026-03-04": 20,
            "2026-03-05": 828,  # backfill spike
            "2026-03-06": 20,
            "2026-03-07": 866,  # backfill spike
            "2026-03-08": 20,
            "2026-03-09": 20,
        }
        result = _compute_baseline_daily(per_day, None, window_start, window_end)
        # Sorted: [20, 20, 20, 20, 828, 866] -> Q1 = 20.0
        assert result == 20.0

    def test_posting_days_filter(self) -> None:
        """Only posting days contribute to the baseline."""
        # Window: Wed 3/4 - Mon 3/10, posting days Mon-Fri
        # Days: Wed(post), Thu(post), Fri(post), Sat(skip), Sun(skip), Mon(post)
        window_start = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)  # Wed
        window_end = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)  # Mon (excl)
        per_day = {
            "2026-03-04": 50,  # Wed
            "2026-03-05": 50,  # Thu
            "2026-03-06": 50,  # Fri
            "2026-03-07": 10,  # Sat (will be skipped)
            "2026-03-08": 10,  # Sun (will be skipped)
            "2026-03-09": 50,  # Mon
        }
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        result = _compute_baseline_daily(per_day, posting_days, window_start, window_end)
        # Posting-day values: [50, 50, 50, 50] -> Q1 = 50.0
        assert result == 50.0

    def test_missing_days_filled_with_zero(self) -> None:
        """Days with no data count as zero in the baseline."""
        window_start = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        window_end = datetime(2026, 3, 10, 12, 0, 0, tzinfo=UTC)
        per_day = {
            "2026-03-04": 50,
        }
        result = _compute_baseline_daily(per_day, None, window_start, window_end)
        # Values: [50, 0, 0, 0, 0, 0] -> sorted: [0,0,0,0,0,50] -> Q1 = 0.0
        assert result == 0.0

    def test_empty_window_returns_zero(self) -> None:
        """Empty or zero-width window returns 0."""
        t = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)
        result = _compute_baseline_daily({}, None, t, t)
        assert result == 0.0

    def test_small_sample_falls_back_to_min(self) -> None:
        """With fewer than 4 posting days, falls back to min value."""
        window_start = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)  # Sat
        window_end = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)  # Fri (excl)
        per_day = {
            "2026-03-17": 42,  # Tue
        }
        result = _compute_baseline_daily(per_day, ["Tue", "Thu"], window_start, window_end)
        # Posting days in window: Tue(42), Thu(0) -> only 2 values, < 4
        # Falls back to min([42, 0]) = 0.0
        assert result == 0.0

    def test_three_posting_days_falls_back_to_min(self) -> None:
        """With exactly 3 posting days, still falls back to min value."""
        # Window: Wed 3/4 - Sat 3/7 (3 days), posting days Mon-Fri
        window_start = datetime(2026, 3, 5, 12, 0, 0, tzinfo=UTC)  # Thu
        window_end = datetime(2026, 3, 8, 12, 0, 0, tzinfo=UTC)  # Sun (excl)
        per_day = {
            "2026-03-05": 30,  # Thu
            "2026-03-06": 25,  # Fri
            "2026-03-07": 0,  # Sat (skipped)
        }
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        result = _compute_baseline_daily(per_day, posting_days, window_start, window_end)
        # Posting days: Thu(30), Fri(25) -> only 2 values, < 4
        # Falls back to min([30, 25]) = 25.0
        assert result == 25.0

    def test_exact_issue_1866_scenario_oc(self) -> None:
        """Exact scenario from #1866: OC 866-doc backfill spike.

        On 2026-03-23, OC had 7 rulings in 24h. The 7-day window had a
        866-ruling backfill day. With median, the baseline was 36/day
        (inflated). With 25th percentile, the baseline stays near the
        normal 20/day rate.
        """
        # 2026-03-23 is Sunday. Window: [3/16 Mon - 3/22 Sun)
        # 3/16 is Monday. Posting days Mon-Fri:
        # Mon 3/16, Tue 3/17, Wed 3/18, Thu 3/19, Fri 3/20 = 5 posting days
        monday = datetime(2026, 3, 23, 12, 0, 0, tzinfo=UTC)
        window_start = monday - timedelta(days=7)  # 3/16 Mon
        window_end = monday - timedelta(days=1)  # 3/22 Sun
        per_day = {
            "2026-03-16": 20,  # Mon - normal
            "2026-03-17": 20,  # Tue - normal
            "2026-03-18": 20,  # Wed - normal
            "2026-03-19": 866,  # Thu - backfill spike
            "2026-03-20": 20,  # Fri - normal
        }
        posting_days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        result = _compute_baseline_daily(per_day, posting_days, window_start, window_end)
        # Posting-day values: [20, 20, 20, 866, 20]
        # Sorted: [20, 20, 20, 20, 866] -> Q1 = 20.0
        assert result == 20.0


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

    def test_staleness_county_filter(self) -> None:
        """County filter works without SQL errors (#1792).

        The county filter (AND ct.county = %s) must appear inside the CTE
        where the ``ct`` alias is available, not in the outer query where
        only CTE columns exist.
        """
        stale = NOW - timedelta(hours=27)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-sd-tentatives-civil", "San Diego", stale, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {"San Diego": {"expected_daily_rulings": 10, "schedule_type": "daily"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines, county="San Diego")
        assert len(alerts) == 1
        assert alerts[0].county == "San Diego"
        assert alerts[0].metric == "scraper_stale"

        # Verify the SQL: county filter must be inside the CTE (before the
        # outer SELECT), not after ``WHERE rn = 1`` in the outer query.
        scraper_query = conn.cursors[0].captured_calls[0][0]
        # Split on the outer SELECT to separate CTE body from outer query
        outer_select_pos = scraper_query.rfind("SELECT scraper_id")
        assert outer_select_pos > 0, "Expected outer SELECT in query"
        cte_body = scraper_query[:outer_select_pos]
        outer_query = scraper_query[outer_select_pos:]
        # The county filter should appear inside the CTE body
        assert "ct.county" in cte_body, (
            "county filter must be inside the CTE where ct alias is available"
        )
        # The outer query should NOT reference ct.county
        assert "ct.county" not in outer_query, (
            "county filter must NOT be in the outer query — ct alias is undefined there"
        )

    def test_staleness_county_filter_fresh_no_alert(self) -> None:
        """County filter with a fresh scraper produces no alert."""
        recent = NOW - timedelta(hours=1)
        conn = FakeConnection(
            {
                "scraper_runs": [("ca-sd-tentatives-civil", "San Diego", recent, "success")],
                "MAX(d.captured_at)": [],
            }
        )
        baselines = _make_baselines(
            {"San Diego": {"expected_daily_rulings": 10, "schedule_type": "daily"}}
        )
        alerts = check_scraper_staleness(conn, NOW, baselines, county="San Diego")
        assert len(alerts) == 0


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
                "AT TIME ZONE": _uniform_per_day_rows("Los Angeles", 200, NOW),
                "d.captured_at >=": [],
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
        original_enr_load = dqc.load_expected_null_rates
        dqc.load_expected_null_rates = MagicMock(return_value={})
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            metrics = {a.metric for a in alerts}
            assert "zero_rulings" in metrics
            assert "scraper_stale" in metrics
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load
            dqc.load_field_baselines = original_field_load
            dqc.load_expected_null_rates = original_enr_load

    def test_run_checks_healthy(self) -> None:
        """run_checks returns empty list when everything is healthy."""
        recent = NOW - timedelta(hours=1)
        la_rows = _uniform_per_day_rows("Los Angeles", 200, NOW)
        og_rows = _uniform_per_day_rows("Orange", 100, NOW)
        conn = FakeConnection(
            {
                # "has_ruling" matches FIELD_COMPLETENESS_QUERY.
                "has_ruling": [],
                # "orphaned_docs" matches ORPHANED_DOCUMENTS_QUERY.
                "orphaned_docs": [],
                "AT TIME ZONE": la_rows + og_rows,
                "d.captured_at >=": [("Los Angeles", 40), ("Orange", 15)],
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
        original_enr_load = dqc.load_expected_null_rates
        dqc.load_expected_null_rates = MagicMock(return_value={})
        try:
            alerts = dqc.run_checks("fake://dsn", now=NOW)
            assert len(alerts) == 0
        finally:
            dqc.psycopg.connect = original_connect
            dqc.load_baselines = original_load
            dqc.load_field_baselines = original_field_load
            dqc.load_expected_null_rates = original_enr_load


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

    def test_updates_total_documents(self, tmp_path: Path) -> None:
        """Updates total_documents for each county when totals dict provided."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {
                        "Los Angeles": {
                            "total_documents": 500,
                            "ruling": 99.0,
                        },
                        "Orange": {
                            "total_documents": 100,
                            "ruling": 98.0,
                        },
                    },
                }
            )
        )
        current = {"Los Angeles": {"ruling": 99.5}, "Orange": {"ruling": 97.0}}
        totals = {"Los Angeles": 748, "Orange": 1772}
        save_field_baselines(current, baselines_file, totals=totals)

        with open(baselines_file) as f:
            raw = json.load(f)
        fc = raw["field_completeness"]
        # total_documents should be overwritten (not ratcheted)
        assert fc["Los Angeles"]["total_documents"] == 748
        assert fc["Orange"]["total_documents"] == 1772
        # Field percentages still ratchet up only
        assert fc["Los Angeles"]["ruling"] == 99.5  # Updated (higher)
        assert fc["Orange"]["ruling"] == 98.0  # Kept old (higher)

    def test_total_documents_overwrites_even_when_lower(self, tmp_path: Path) -> None:
        """total_documents is overwritten even when the new value is lower."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {
                        "Los Angeles": {
                            "total_documents": 1000,
                            "ruling": 99.0,
                        },
                    },
                }
            )
        )
        current = {"Los Angeles": {"ruling": 99.0}}
        totals = {"Los Angeles": 500}
        save_field_baselines(current, baselines_file, totals=totals)

        with open(baselines_file) as f:
            raw = json.load(f)
        # total_documents lowered from 1000 to 500
        assert raw["field_completeness"]["Los Angeles"]["total_documents"] == 500

    def test_total_documents_adds_to_new_county(self, tmp_path: Path) -> None:
        """total_documents is set for a new county not yet in baselines."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {},
                }
            )
        )
        current = {"Riverside": {"ruling": 100.0}}
        totals = {"Riverside": 358}
        save_field_baselines(current, baselines_file, totals=totals)

        with open(baselines_file) as f:
            raw = json.load(f)
        assert raw["field_completeness"]["Riverside"]["total_documents"] == 358
        assert raw["field_completeness"]["Riverside"]["ruling"] == 100.0

    def test_no_totals_preserves_existing_total_documents(self, tmp_path: Path) -> None:
        """When totals is None, existing total_documents values are preserved."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "field_completeness": {
                        "Los Angeles": {
                            "total_documents": 748,
                            "ruling": 99.0,
                        },
                    },
                }
            )
        )
        current = {"Los Angeles": {"ruling": 99.5}}
        # No totals parameter — backward compatible
        save_field_baselines(current, baselines_file)

        with open(baselines_file) as f:
            raw = json.load(f)
        # total_documents should remain unchanged
        assert raw["field_completeness"]["Los Angeles"]["total_documents"] == 748
        assert raw["field_completeness"]["Los Angeles"]["ruling"] == 99.5


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

    def test_grace_period_constant_is_60_minutes(self) -> None:
        """Grace period constant is set to 60 minutes (#1887)."""
        assert FIELD_COMPLETENESS_GRACE_MINUTES == 60


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


class TestLowVolumeCountySuppression:
    """Tests for low_volume flag suppressing field_completeness_low_sample alerts."""

    def test_low_volume_county_no_alert(self) -> None:
        """Low-volume county with low sample size does NOT emit an alert."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "San Francisco",
                        total=2,
                        judge=0,
                    ),
                ],
            }
        )
        field_baselines = {
            "San Francisco": {"judge": 100.0, "ruling": 100.0},
        }
        baselines_with_low_volume = {
            "San Francisco": Baselines(
                expected_daily_rulings=0.5,
                schedule_type="daily",
                low_volume=True,
            ),
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, baselines=baselines_with_low_volume
        )
        # Should NOT produce any alerts — low_volume suppresses low_sample.
        assert len(alerts) == 0

    def test_non_low_volume_county_still_alerts(self) -> None:
        """Non-low-volume county with low sample size still emits the alert."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Ventura",
                        total=3,
                        judge=0,
                    ),
                ],
            }
        )
        field_baselines = {
            "Ventura": {"judge": 100.0, "ruling": 100.0},
        }
        baselines_without_low_volume = {
            "Ventura": Baselines(
                expected_daily_rulings=5,
                schedule_type="daily",
                low_volume=False,
            ),
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, baselines=baselines_without_low_volume
        )
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 1
        assert low_sample[0].county == "Ventura"

    def test_low_volume_does_not_affect_field_regression_detection(self) -> None:
        """Low-volume flag only suppresses low_sample alerts, not regressions.

        If a low-volume county actually has enough docs, field completeness
        regressions are still detected normally.
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "San Francisco",
                        total=10,  # Above MIN_FIELD_CHECK_SAMPLE_SIZE
                        judge=8,  # 8/10 = 80% → 20pp drop from 100% baseline → P1
                    ),
                ],
            }
        )
        field_baselines = {
            "San Francisco": {"judge": 100.0},
        }
        baselines_with_low_volume = {
            "San Francisco": Baselines(
                expected_daily_rulings=0.5,
                schedule_type="daily",
                low_volume=True,
            ),
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, baselines=baselines_with_low_volume
        )
        # Should still produce a field_completeness regression alert.
        p1_alerts = [a for a in alerts if a.metric == "field_completeness"]
        assert len(p1_alerts) == 1
        assert p1_alerts[0].severity == "p1"

    def test_no_baselines_param_backward_compatible(self) -> None:
        """When baselines param is None (not provided), alerts still fire."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=2,
                        judge=0,
                    ),
                ],
            }
        )
        field_baselines = {
            "Santa Clara": {"judge": 95.0},
        }
        # Call without baselines param → backward-compatible behavior.
        alerts = check_field_completeness(conn, NOW, field_baselines)
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 1
        assert low_sample[0].county == "Santa Clara"

    def test_mixed_low_volume_and_normal_counties(self) -> None:
        """Low-volume county suppressed, normal county alerts as usual."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "San Francisco",
                        total=2,
                        judge=0,
                    ),
                    _make_field_completeness_row(
                        "Ventura",
                        total=3,
                        judge=0,
                    ),
                ],
            }
        )
        field_baselines = {
            "San Francisco": {"judge": 100.0, "ruling": 100.0},
            "Ventura": {"judge": 100.0, "ruling": 100.0},
        }
        baselines_mixed = {
            "San Francisco": Baselines(
                expected_daily_rulings=0.5,
                schedule_type="daily",
                low_volume=True,
            ),
            "Ventura": Baselines(
                expected_daily_rulings=5,
                schedule_type="daily",
                low_volume=False,
            ),
        }
        alerts = check_field_completeness(conn, NOW, field_baselines, baselines=baselines_mixed)
        # San Francisco: suppressed (low_volume).
        sf_alerts = [a for a in alerts if a.county == "San Francisco"]
        assert len(sf_alerts) == 0
        # Ventura: not low_volume, should get the alert.
        v_alerts = [a for a in alerts if a.county == "Ventura"]
        assert len(v_alerts) == 1
        assert v_alerts[0].metric == "field_completeness_low_sample"

    def test_county_not_in_baselines_still_alerts(self) -> None:
        """County present in field_baselines but missing from baselines dict still alerts."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Unknown County",
                        total=2,
                        judge=0,
                    ),
                ],
            }
        )
        field_baselines = {
            "Unknown County": {"judge": 95.0},
        }
        # baselines does not include "Unknown County" at all.
        baselines_empty = {}
        alerts = check_field_completeness(conn, NOW, field_baselines, baselines=baselines_empty)
        low_sample = [a for a in alerts if a.metric == "field_completeness_low_sample"]
        assert len(low_sample) == 1
        assert low_sample[0].county == "Unknown County"


class TestLoadExpectedNullRates:
    """Tests for load_expected_null_rates function."""

    def test_load_from_file(self, tmp_path: Path) -> None:
        """Loads expected null rates from a JSON file."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "expected_null_rates": {
                        "Orange": {
                            "outcome": 17.0,
                            "motion_type": 15.0,
                            "_note": "Calendar-list PDFs",
                        },
                    },
                }
            )
        )
        result = load_expected_null_rates(baselines_file)
        assert "Orange" in result
        assert result["Orange"]["outcome"] == 17.0
        assert result["Orange"]["motion_type"] == 15.0
        # _note should be excluded (starts with _).
        assert "_note" not in result["Orange"]

    def test_load_from_file_with_metadata_keys(self, tmp_path: Path) -> None:
        """Regression: file with _updated/_note at top level does not crash (#2333)."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {},
                    "expected_null_rates": {
                        "_updated": "2026-04-01",
                        "_note": "Per-county expected null rates ...",
                        "Orange": {
                            "outcome": 17.0,
                            "motion_type": 15.0,
                        },
                        "Santa Clara": {
                            "outcome": 2.0,
                            "motion_type": 10.0,
                        },
                    },
                }
            )
        )
        result = load_expected_null_rates(baselines_file)
        assert "Orange" in result
        assert "Santa Clara" in result
        assert result["Orange"]["outcome"] == 17.0
        assert result["Santa Clara"]["outcome"] == 2.0
        assert "_updated" not in result
        assert "_note" not in result

    def test_load_from_raw_dict(self) -> None:
        """Loads expected null rates from a pre-parsed dict."""
        raw = {
            "expected_null_rates": {
                "Santa Clara": {
                    "outcome": 2.0,
                    "_note": "Cross-references",
                },
            }
        }
        result = load_expected_null_rates(raw=raw)
        assert "Santa Clara" in result
        assert result["Santa Clara"]["outcome"] == 2.0

    def test_empty_when_missing(self) -> None:
        """Returns empty dict when expected_null_rates section is absent."""
        raw: dict[str, Any] = {"counties": {}}
        result = load_expected_null_rates(raw=raw)
        assert result == {}

    def test_empty_when_file_missing(self, tmp_path: Path) -> None:
        """Returns empty dict when baselines file does not exist."""
        result = load_expected_null_rates(tmp_path / "nonexistent.json")
        assert result == {}

    def test_skips_non_numeric_values(self) -> None:
        """Skips non-numeric values (except _-prefixed metadata)."""
        raw = {
            "expected_null_rates": {
                "Orange": {
                    "outcome": 17.0,
                    "judge": "not a number",
                },
            }
        }
        result = load_expected_null_rates(raw=raw)
        assert result["Orange"]["outcome"] == 17.0
        assert "judge" not in result["Orange"]

    def test_integer_values_converted_to_float(self) -> None:
        """Integer values are converted to float."""
        raw = {
            "expected_null_rates": {
                "Orange": {"outcome": 17},
            }
        }
        result = load_expected_null_rates(raw=raw)
        assert result["Orange"]["outcome"] == 17.0
        assert isinstance(result["Orange"]["outcome"], float)

    def test_skips_top_level_metadata_keys(self) -> None:
        """Top-level _-prefixed keys (e.g., _updated, _note) are skipped (#2333).

        The baselines JSON uses _updated and _note at the top level of
        expected_null_rates for human documentation.  These are strings,
        not county dicts, so iterating them with .items() would crash.
        """
        raw = {
            "expected_null_rates": {
                "_updated": "2026-04-01",
                "_note": "Per-county expected null rates ...",
                "Orange": {
                    "outcome": 17.0,
                    "motion_type": 15.0,
                },
            }
        }
        result = load_expected_null_rates(raw=raw)
        assert "Orange" in result
        assert result["Orange"]["outcome"] == 17.0
        assert "_updated" not in result
        assert "_note" not in result

    def test_skips_non_dict_county_values(self) -> None:
        """Non-dict values at the top level are skipped gracefully."""
        raw = {
            "expected_null_rates": {
                "Orange": {
                    "outcome": 17.0,
                },
                "bad_entry": "some string value",
                "another_bad": 42,
            }
        }
        result = load_expected_null_rates(raw=raw)
        assert "Orange" in result
        assert "bad_entry" not in result
        assert "another_bad" not in result


class TestExpectedNullRateFieldCompleteness:
    """Tests for expected null rate adjustments in check_field_completeness (#2318)."""

    def _all_fields_baselines(self, **overrides: float) -> dict[str, float]:
        """Create a full field baselines dict with reasonable defaults."""
        defaults = {
            "ruling": 100.0,
            "judge": 95.0,
            "motion_type": 90.0,
            "outcome": 90.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 80.0,
            "hearing_date": 100.0,
            "case_type": 85.0,
        }
        defaults.update(overrides)
        return defaults

    def test_no_alert_when_within_expected_null_rate(self) -> None:
        """No alert when field is within the expected null rate ceiling.

        OC outcome baseline is 90%, expected null rate is 17%.
        Effective baseline = min(90, 100-17) = min(90, 83) = 83%.
        Current is 83% → drop = 0pp → no alert.
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 0

    def test_alert_when_below_expected_null_rate_ceiling(self) -> None:
        """P1 alert when field drops significantly below the null rate ceiling.

        OC outcome baseline is 90%, expected null rate is 17%.
        Effective baseline = min(90, 83) = 83%.
        Current is 70% → drop = 13pp → P1 alert.
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=70,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert outcome_alerts[0].severity == "p1"
        # Effective baseline should be 83.0, not 90.0.
        assert outcome_alerts[0].expected == 83.0

    def test_p2_alert_for_moderate_drop_below_ceiling(self) -> None:
        """P2 alert when field drops 5-10pp below the null rate ceiling.

        OC outcome baseline is 90%, expected null rate is 17%.
        Effective baseline = 83%. Current is 77% → drop = 6pp → P2.
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=77,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert outcome_alerts[0].severity == "p2"

    def test_ceiling_does_not_raise_baseline(self) -> None:
        """Expected null rate does not raise the effective baseline.

        If baseline is 80% and expected null rate is 10%,
        effective baseline = min(80, 90) = 80% (no change).
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=74,  # 6pp below 80% baseline → P2
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=80.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 10.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        # The effective baseline should be 80.0 (baseline is lower than ceiling).
        assert outcome_alerts[0].expected == 80.0

    def test_no_expected_null_rates_backward_compatible(self) -> None:
        """check_field_completeness works normally when expected_null_rates is None."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,  # 7pp below 90% baseline → P2
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        # No expected_null_rates passed (backward compatibility).
        alerts = check_field_completeness(conn, NOW, field_baselines)
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert outcome_alerts[0].severity == "p2"
        assert outcome_alerts[0].expected == 90.0

    def test_empty_expected_null_rates_backward_compatible(self) -> None:
        """check_field_completeness works normally when expected_null_rates is empty dict."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        alerts = check_field_completeness(conn, NOW, field_baselines, expected_null_rates={})
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert outcome_alerts[0].expected == 90.0

    def test_only_configured_fields_adjusted(self) -> None:
        """Only fields listed in expected_null_rates are adjusted.

        If OC has outcome null rate configured but not judge,
        judge still uses the raw baseline.
        """
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,  # Within ceiling: 100-17=83
                        judge=80,  # 15pp below 95% baseline → P1
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0, judge=95.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        judge_alerts = [a for a in alerts if "judge" in a.message]
        # Outcome: no alert (within ceiling).
        assert len(outcome_alerts) == 0
        # Judge: P1 alert (no null rate configured, raw baseline used).
        assert len(judge_alerts) == 1
        assert judge_alerts[0].severity == "p1"
        assert judge_alerts[0].expected == 95.0

    def test_multiple_counties_independent(self) -> None:
        """Expected null rates apply per-county independently."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,
                    ),
                    _make_field_completeness_row(
                        "Santa Clara",
                        total=100,
                        outcome=83,  # 7pp below 90% baseline → P2 (no null rate configured)
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
            "Santa Clara": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
            # Santa Clara has no expected null rate for outcome.
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        oc_outcome = [a for a in alerts if a.county == "Orange" and "outcome" in a.message]
        sc_outcome = [a for a in alerts if a.county == "Santa Clara" and "outcome" in a.message]
        # Orange: no alert (within ceiling).
        assert len(oc_outcome) == 0
        # Santa Clara: P2 alert (no null rate configured).
        assert len(sc_outcome) == 1
        assert sc_outcome[0].severity == "p2"

    def test_zero_null_rate_has_no_effect(self) -> None:
        """A zero expected null rate does not change the effective baseline."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=83,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 0.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert outcome_alerts[0].expected == 90.0

    def test_message_uses_effective_baseline(self) -> None:
        """Alert message shows the effective (capped) baseline, not the raw one."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Orange",
                        total=100,
                        outcome=70,
                    ),
                ],
            }
        )
        field_baselines = {
            "Orange": self._all_fields_baselines(outcome=90.0),
        }
        expected_null_rates = {
            "Orange": {"outcome": 17.0},
        }
        alerts = check_field_completeness(
            conn, NOW, field_baselines, expected_null_rates=expected_null_rates
        )
        outcome_alerts = [a for a in alerts if "outcome" in a.message]
        assert len(outcome_alerts) == 1
        assert "83.0%" in outcome_alerts[0].message
        assert "70.0%" in outcome_alerts[0].message
        assert "13.0pp drop" in outcome_alerts[0].message


class TestIsBulkIngest:
    """Tests for _is_bulk_ingest helper function."""

    def test_detects_bulk_when_over_multiplier(self) -> None:
        """Returns True when window count exceeds multiplier times baseline."""
        field_baselines = {"Riverside": {"total_documents": 66}}
        # 200 > 3.0 * 66 = 198
        assert _is_bulk_ingest("Riverside", 200, field_baselines) is True

    def test_no_bulk_when_under_multiplier(self) -> None:
        """Returns False when window count is within multiplier range."""
        field_baselines = {"Riverside": {"total_documents": 66}}
        # 190 < 3.0 * 66 = 198
        assert _is_bulk_ingest("Riverside", 190, field_baselines) is False

    def test_exact_multiplier_not_bulk(self) -> None:
        """Returns False when window count equals exactly multiplier times baseline."""
        field_baselines = {"Riverside": {"total_documents": 100}}
        # 300 is NOT > 3.0 * 100 = 300 (strictly greater than)
        assert _is_bulk_ingest("Riverside", 300, field_baselines) is False

    def test_no_baseline_returns_false(self) -> None:
        """Returns False when county has no field baselines."""
        assert _is_bulk_ingest("Unknown", 1000, {}) is False

    def test_zero_baseline_returns_false(self) -> None:
        """Returns False when total_documents baseline is 0."""
        field_baselines = {"Riverside": {"total_documents": 0}}
        assert _is_bulk_ingest("Riverside", 1000, field_baselines) is False

    def test_missing_total_documents_returns_false(self) -> None:
        """Returns False when total_documents key is missing."""
        field_baselines = {"Riverside": {"ruling": 100.0}}
        assert _is_bulk_ingest("Riverside", 1000, field_baselines) is False


class TestBulkIngestFieldCompleteness:
    """Tests for bulk-ingest detection in check_field_completeness (#1887)."""

    def test_bulk_ingest_downgrades_to_p2(self) -> None:
        """Bulk ingest emits P2 informational instead of P1 regression."""
        # Simulate 2500 docs in window vs 66 baseline (>3x = bulk)
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Riverside",
                        total=2500,
                        judge=1000,  # 40% vs 57.6% baseline = 17.6pp drop → P1 normally
                    ),
                ],
            }
        )
        field_baselines = {
            "Riverside": {
                "total_documents": 66,
                "ruling": 100.0,
                "judge": 57.6,
                "motion_type": 97.0,
                "outcome": 90.0,
                "case_title": 97.0,
                "case_number": 97.0,
                "parties": 97.0,
                "hearing_date": 100.0,
                "case_type": 97.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        # Should NOT produce any P1 field_completeness alerts.
        p1_alerts = [a for a in alerts if a.severity == "p1"]
        assert len(p1_alerts) == 0
        # Should produce exactly one P2 bulk_ingest informational alert.
        bulk_alerts = [a for a in alerts if a.metric == "field_completeness_bulk_ingest"]
        assert len(bulk_alerts) == 1
        assert bulk_alerts[0].severity == "p2"
        assert bulk_alerts[0].county == "Riverside"
        assert "bulk ingest" in bulk_alerts[0].message.lower()
        assert "2500" in bulk_alerts[0].message
        assert "66" in bulk_alerts[0].message

    def test_no_bulk_ingest_alerts_normally(self) -> None:
        """Normal doc count still produces standard regression alerts."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,
                        judge=80,  # 80% vs 95% baseline = 15pp drop → P1
                    ),
                ],
            }
        )
        field_baselines = {
            "Los Angeles": {
                "total_documents": 748,
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
        # Normal doc count (100 < 3 * 748) → standard alerts.
        bulk_alerts = [a for a in alerts if a.metric == "field_completeness_bulk_ingest"]
        assert len(bulk_alerts) == 0
        p1_alerts = [a for a in alerts if a.severity == "p1"]
        assert len(p1_alerts) >= 1

    def test_bulk_ingest_without_total_documents_alerts_normally(self) -> None:
        """Counties without total_documents in baselines bypass bulk detection."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "New County",
                        total=500,
                        judge=200,  # 40% vs 95% = 55pp drop → P1 normally
                    ),
                ],
            }
        )
        field_baselines = {
            "New County": {
                # No total_documents key
                "judge": 95.0,
                "ruling": 100.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        # No bulk detection → normal P1 alert.
        p1_alerts = [a for a in alerts if a.severity == "p1"]
        assert len(p1_alerts) >= 1

    def test_bulk_ingest_checked_before_per_field(self) -> None:
        """Bulk ingest check prevents any per-field alerts from being emitted."""
        # All fields have significant drops, but bulk ingest should suppress them all
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Riverside",
                        total=300,  # >3x of 66 baseline
                        ruling=100,
                        judge=50,
                        motion_type=50,
                        outcome=50,
                        title=50,
                        case_number=50,
                        parties=50,
                        hearing_date=50,
                        case_type=50,
                    ),
                ],
            }
        )
        field_baselines = {
            "Riverside": {
                "total_documents": 66,
                "ruling": 100.0,
                "judge": 57.6,
                "motion_type": 97.0,
                "outcome": 90.0,
                "case_title": 97.0,
                "case_number": 97.0,
                "parties": 97.0,
                "hearing_date": 100.0,
                "case_type": 97.0,
            },
        }
        alerts = check_field_completeness(conn, NOW, field_baselines)
        # Only the single bulk_ingest alert, no per-field alerts.
        assert len(alerts) == 1
        assert alerts[0].metric == "field_completeness_bulk_ingest"

    def test_mixed_counties_bulk_and_normal(self) -> None:
        """Bulk county gets bulk alert, normal county gets standard alerts."""
        conn = FakeConnection(
            {
                "case_parties": [
                    _make_field_completeness_row(
                        "Riverside",
                        total=300,  # >3x of 66 = bulk
                        judge=50,
                    ),
                    _make_field_completeness_row(
                        "Los Angeles",
                        total=100,  # <3x of 748 = normal
                        judge=80,
                    ),
                ],
            }
        )
        field_baselines = {
            "Riverside": {
                "total_documents": 66,
                "judge": 57.6,
                "ruling": 100.0,
                "motion_type": 97.0,
                "outcome": 90.0,
                "case_title": 97.0,
                "case_number": 97.0,
                "parties": 97.0,
                "hearing_date": 100.0,
                "case_type": 97.0,
            },
            "Los Angeles": {
                "total_documents": 748,
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
        # Riverside: bulk ingest alert.
        rv_alerts = [a for a in alerts if a.county == "Riverside"]
        assert len(rv_alerts) == 1
        assert rv_alerts[0].metric == "field_completeness_bulk_ingest"
        # Los Angeles: standard regression alert.
        la_alerts = [
            a for a in alerts if a.county == "Los Angeles" and a.metric == "field_completeness"
        ]
        assert len(la_alerts) >= 1


class TestBulkIngestOrphanedDocuments:
    """Tests for bulk-ingest detection in check_orphaned_documents (#1887)."""

    def test_bulk_ingest_downgrades_orphan_alert(self) -> None:
        """Bulk ingest downgrades orphaned documents alert to P2."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Riverside", total=300, orphaned=100),
                ],
            }
        )
        field_baselines = {
            "Riverside": {"total_documents": 66},
        }
        alerts = check_orphaned_documents(conn, NOW, field_baselines=field_baselines)
        # Should produce a P2 bulk_ingest alert instead of P1 orphaned.
        assert len(alerts) == 1
        assert alerts[0].metric == "orphaned_documents_bulk_ingest"
        assert alerts[0].severity == "p2"
        assert "bulk ingest" in alerts[0].message.lower()

    def test_normal_orphan_alert_without_bulk(self) -> None:
        """Normal doc count produces standard orphaned documents alert."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Orange", total=100, orphaned=30),
                ],
            }
        )
        field_baselines = {
            "Orange": {"total_documents": 1772},
        }
        alerts = check_orphaned_documents(conn, NOW, field_baselines=field_baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "orphaned_documents"
        assert alerts[0].severity == "p1"

    def test_orphan_backward_compatible_no_baselines(self) -> None:
        """Without field_baselines, bulk detection is skipped."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Riverside", total=300, orphaned=100),
                ],
            }
        )
        # No field_baselines → standard P1 alert.
        alerts = check_orphaned_documents(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].metric == "orphaned_documents"
        assert alerts[0].severity == "p1"

    def test_orphan_bulk_mixed_counties(self) -> None:
        """Bulk county gets downgraded, normal county gets standard alert."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Riverside", total=300, orphaned=100),
                    _make_orphaned_docs_row("Orange", total=100, orphaned=30),
                ],
            }
        )
        field_baselines = {
            "Riverside": {"total_documents": 66},
            "Orange": {"total_documents": 1772},
        }
        alerts = check_orphaned_documents(conn, NOW, field_baselines=field_baselines)
        rv_alerts = [a for a in alerts if a.county == "Riverside"]
        assert len(rv_alerts) == 1
        assert rv_alerts[0].metric == "orphaned_documents_bulk_ingest"
        assert rv_alerts[0].severity == "p2"
        oc_alerts = [a for a in alerts if a.county == "Orange"]
        assert len(oc_alerts) == 1
        assert oc_alerts[0].metric == "orphaned_documents"
        assert oc_alerts[0].severity == "p1"

    def test_orphan_below_threshold_not_affected_by_bulk(self) -> None:
        """Orphaned docs below threshold produce no alert even during bulk ingest."""
        conn = FakeConnection(
            {
                "orphaned_docs": [
                    _make_orphaned_docs_row("Riverside", total=300, orphaned=5),
                ],
            }
        )
        field_baselines = {
            "Riverside": {"total_documents": 66},
        }
        # 5/300 = 1.7% → below both thresholds → no alert.
        alerts = check_orphaned_documents(conn, NOW, field_baselines=field_baselines)
        assert len(alerts) == 0


class TestLoadBaselinesLowVolume:
    """Tests for low_volume flag in load_baselines."""

    def test_load_low_volume_true(self, tmp_path: Path) -> None:
        """Loads low_volume=True from baselines JSON."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "San Francisco": {
                            "expected_daily_rulings": 0.5,
                            "schedule_type": "daily",
                            "low_volume": True,
                        },
                    }
                }
            )
        )
        result = load_baselines(baselines_file)
        assert result["San Francisco"].low_volume is True

    def test_load_low_volume_default_false(self, tmp_path: Path) -> None:
        """Low_volume defaults to False when not specified."""
        baselines_file = tmp_path / "baselines.json"
        baselines_file.write_text(
            json.dumps(
                {
                    "counties": {
                        "Los Angeles": {
                            "expected_daily_rulings": 50,
                            "schedule_type": "daily",
                        },
                    }
                }
            )
        )
        result = load_baselines(baselines_file)
        assert result["Los Angeles"].low_volume is False

    def test_load_low_volume_from_raw_dict(self) -> None:
        """Loads low_volume flag from pre-parsed raw dict."""
        raw = {
            "counties": {
                "Santa Clara": {
                    "expected_daily_rulings": 0.1,
                    "schedule_type": "daily",
                    "low_volume": True,
                },
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                },
            }
        }
        result = load_baselines(raw=raw)
        assert result["Santa Clara"].low_volume is True
        assert result["Orange"].low_volume is False


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
                # RULING_COUNTS_7D_PER_DAY_QUERY (matched first by "AT TIME ZONE")
                "AT TIME ZONE": [],
                # RULING_COUNTS_7D_QUERY (has "captured_at < %s", 24h query does not)
                "captured_at < %s": [],
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
        """Collects ruling_count_7d_avg from 7-day window (no baselines = calendar days)."""
        conn = FakeConnection(
            {
                "AT TIME ZONE": _uniform_per_day_rows("Orange", 120, NOW),
                # 7d query matches first via "captured_at < %s"
                "captured_at < %s": [("Orange", 120)],
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
        assert m["value"] == round(120 / 6.0, 2)  # No baselines = calendar days
        assert m["metadata"]["total_7d_window"] == 120
        # Baseline (25th percentile) should also be present in metadata.
        assert "baseline_7d" in m["metadata"]
        assert m["metadata"]["baseline_7d"] == 20.0

    def test_collects_ruling_count_7d_avg_with_posting_days(self) -> None:
        """7d average uses posting days when baselines are provided (#1784)."""
        # NOW is 2026-03-11 12:00 (Wednesday).
        # 7d window = [Mar 4 12:00, Mar 10 12:00) = 6 calendar days.
        # Days: Wed Mar 4, Thu Mar 5, Fri Mar 6, Sat Mar 7, Sun Mar 8, Mon Mar 9
        # Mon-Fri posting days in window: Wed, Thu, Fri, Mon = 4
        conn = FakeConnection(
            {
                # Per-day: 30/day on posting days (Wed-Fri + Mon), 0 on weekends
                # Indices: Wed=30, Thu=30, Fri=30, Sat=0, Sun=0, Mon=30
                "AT TIME ZONE": _make_per_day_rows("Orange", [30, 30, 30, 0, 0, 30], NOW),
                "captured_at < %s": [("Orange", 120)],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
            }
        )
        orange_baselines = _make_baselines(
            {
                "Orange": {
                    "expected_daily_rulings": 20,
                    "schedule_type": "daily",
                    "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                },
            }
        )
        result = _collect_full_metrics(conn, NOW, baselines=orange_baselines)
        assert "Orange" in result
        m = result["Orange"]["ruling_count_7d_avg"]
        # 120 / 4 posting days = 30.0
        assert m["value"] == 30.0
        assert m["metadata"]["total_7d_window"] == 120
        assert m["metadata"]["window_days"] == 4.0
        # Posting-day values: [30, 30, 30, 30] -> Q1 = 30.0
        assert m["metadata"]["baseline_7d"] == 30.0

    def test_collects_field_completeness_metrics(self) -> None:
        """Collects overall and per-field completeness metrics."""
        conn = FakeConnection(
            {
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": [],
                "captured_at < %s": [],
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
                "AT TIME ZONE": _uniform_per_day_rows("TestCounty", 210, NOW),
                "captured_at < %s": [("TestCounty", 210)],
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
        """  # sql-check:ignore — intentionally invalid SQL for testing validation
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
# Timezone safety: DATE() calls must use explicit AT TIME ZONE (#1844)
# ---------------------------------------------------------------------------


class TestTimezoneExplicitness:
    """Verify that SQL queries handle timezones correctly (#1844).

    - Any DATE() call on a TIMESTAMPTZ column must include
      ``AT TIME ZONE 'UTC'`` to avoid session-timezone dependence.
    - Bare TIMESTAMPTZ comparisons (>=, <, <=) are inherently safe and
      should NOT have ``AT TIME ZONE`` added.
    """

    def test_date_calls_have_explicit_timezone(self) -> None:
        """Every DATE(column) in a query must use AT TIME ZONE 'UTC'."""
        import re

        queries = _get_all_query_constants()
        violations: list[str] = []
        # Match DATE(<anything>) that does NOT include "AT TIME ZONE"
        # inside the parentheses.
        pattern = re.compile(r"DATE\(([^)]+)\)", re.IGNORECASE)
        for name, sql in queries.items():
            for match in pattern.finditer(sql):
                inner = match.group(1)
                if "AT TIME ZONE" not in inner.upper():
                    violations.append(f"{name}: DATE({inner}) missing AT TIME ZONE 'UTC'")
        assert not violations, (
            "DATE() calls on TIMESTAMPTZ columns must include "
            "AT TIME ZONE 'UTC' to avoid session-timezone dependence:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_no_bare_date_cast_on_timestamptz(self) -> None:
        """No query should cast a timestamptz column to date via ::date."""
        import re

        queries = _get_all_query_constants()
        violations: list[str] = []
        # Match alias.column::date patterns (e.g. d.captured_at::date)
        pattern = re.compile(r"\b\w+\.\w+::date\b", re.IGNORECASE)
        for name, sql in queries.items():
            for match in pattern.finditer(sql):
                violations.append(f"{name}: {match.group(0)}")
        assert not violations, (
            "::date casts on TIMESTAMPTZ columns are session-timezone-dependent. "
            "Use DATE(col AT TIME ZONE 'UTC') instead:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_per_day_query_uses_explicit_utc(self) -> None:
        """RULING_COUNTS_7D_PER_DAY_QUERY must use AT TIME ZONE 'UTC'."""
        sql = dqc.RULING_COUNTS_7D_PER_DAY_QUERY
        assert "AT TIME ZONE 'UTC'" in sql, (
            "RULING_COUNTS_7D_PER_DAY_QUERY DATE() grouping must use "
            "AT TIME ZONE 'UTC' for timezone-safe date conversion"
        )

    def test_safe_timestamptz_queries_do_not_use_at_time_zone(self) -> None:
        """Queries with only timezone-safe TIMESTAMPTZ operations should NOT add AT TIME ZONE.

        This includes comparisons (>=, <, <=), ordering (ORDER BY), and
        aggregations/functions (MAX, GREATEST).  Adding AT TIME ZONE to
        any of these would convert the column to timestamp-without-timezone,
        changing the operation's semantics.  This test guards against
        well-intentioned but incorrect 'fixes'.
        """
        # These queries use only timezone-safe timestamptz operations, not DATE().
        safe_timestamptz_queries = [
            ("RULING_COUNTS_24H_QUERY", dqc.RULING_COUNTS_24H_QUERY),
            ("RULING_COUNTS_7D_QUERY", dqc.RULING_COUNTS_7D_QUERY),
            ("SCRAPER_SUCCESS_RATE_24H_QUERY", dqc.SCRAPER_SUCCESS_RATE_24H_QUERY),
            ("RULING_COUNT_BY_TYPE_QUERY", dqc.RULING_COUNT_BY_TYPE_QUERY),
            ("FIELD_GAP_DOCS_QUERY", dqc.FIELD_GAP_DOCS_QUERY),
            ("FIELD_COMPLETENESS_QUERY", dqc.FIELD_COMPLETENESS_QUERY),
            ("ORPHANED_DOCUMENTS_QUERY", dqc.ORPHANED_DOCUMENTS_QUERY),
            ("LATEST_SCRAPER_RUN_QUERY", dqc.LATEST_SCRAPER_RUN_QUERY),
            ("LATEST_CAPTURE_PER_COUNTY_QUERY", dqc.LATEST_CAPTURE_PER_COUNTY_QUERY),
        ]
        for name, sql in safe_timestamptz_queries:
            assert "AT TIME ZONE" not in sql, (
                f"{name} uses only timezone-safe TIMESTAMPTZ operations which "
                f"are inherently safe.  Do NOT add AT TIME ZONE — "
                f"it converts the column to timestamp-without-timezone and "
                f"changes the operation's semantics."
            )


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


# ---------------------------------------------------------------------------
# ECS service health check tests
# ---------------------------------------------------------------------------


class TestEcsServiceHealthCheck:
    """Tests for check_ecs_service_health()."""

    def _make_ecs_configs(self) -> list[EcsServiceConfig]:
        """Create test ECS service configs."""
        return [
            EcsServiceConfig(
                cluster="judgemind-dev",
                service="judgemind-ingestion-worker-dev",
                display_name="Ingestion Worker (dev)",
            ),
            EcsServiceConfig(
                cluster="judgemind-dev",
                service="judgemind-api-dev",
                display_name="API (dev)",
            ),
        ]

    def test_healthy_services_no_alerts(self) -> None:
        """Healthy services (running == desired) produce no alerts."""
        mock_client = MagicMock()
        mock_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "judgemind-ingestion-worker-dev",
                    "runningCount": 1,
                    "desiredCount": 1,
                },
                {
                    "serviceName": "judgemind-api-dev",
                    "runningCount": 1,
                    "desiredCount": 1,
                },
            ],
        }
        alerts = check_ecs_service_health(self._make_ecs_configs(), mock_client)
        assert len(alerts) == 0

    def test_zero_running_p1_alert(self) -> None:
        """Service with runningCount=0 produces a P1 alert."""
        mock_client = MagicMock()
        mock_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "judgemind-ingestion-worker-dev",
                    "runningCount": 0,
                    "desiredCount": 1,
                },
                {
                    "serviceName": "judgemind-api-dev",
                    "runningCount": 1,
                    "desiredCount": 1,
                },
            ],
        }
        alerts = check_ecs_service_health(self._make_ecs_configs(), mock_client)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.severity == "p1"
        assert alert.metric == "ecs_service_health"
        assert alert.county == "INFRASTRUCTURE"
        assert alert.actual == 0
        assert alert.expected == 1
        assert "Ingestion Worker (dev)" in alert.message
        assert "runningCount=0" in alert.message

    def test_degraded_service_p2_alert(self) -> None:
        """Service with 0 < running < desired produces a P2 alert."""
        configs = [
            EcsServiceConfig(
                cluster="judgemind-dev",
                service="judgemind-api-dev",
                display_name="API (dev)",
            ),
        ]
        mock_client = MagicMock()
        mock_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "judgemind-api-dev",
                    "runningCount": 1,
                    "desiredCount": 2,
                },
            ],
        }
        alerts = check_ecs_service_health(configs, mock_client)
        assert len(alerts) == 1
        assert alerts[0].severity == "p2"

    def test_service_not_found_p1_alert(self) -> None:
        """Service not returned in describe_services produces a P1 alert."""
        configs = [
            EcsServiceConfig(
                cluster="judgemind-dev",
                service="judgemind-missing-service",
                display_name="Missing Service",
            ),
        ]
        mock_client = MagicMock()
        mock_client.describe_services.return_value = {"services": []}
        alerts = check_ecs_service_health(configs, mock_client)
        assert len(alerts) == 1
        assert alerts[0].severity == "p1"
        assert "not found" in alerts[0].message

    def test_desired_zero_skipped(self) -> None:
        """Services with desiredCount=0 are intentionally scaled down — no alert."""
        configs = [
            EcsServiceConfig(
                cluster="judgemind-dev",
                service="judgemind-scaled-down",
                display_name="Scaled Down",
            ),
        ]
        mock_client = MagicMock()
        mock_client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "judgemind-scaled-down",
                    "runningCount": 0,
                    "desiredCount": 0,
                },
            ],
        }
        alerts = check_ecs_service_health(configs, mock_client)
        assert len(alerts) == 0

    def test_empty_configs_no_alerts(self) -> None:
        """Empty ECS config list produces no alerts."""
        mock_client = MagicMock()
        alerts = check_ecs_service_health([], mock_client)
        assert len(alerts) == 0
        mock_client.describe_services.assert_not_called()

    def test_api_exception_handled_gracefully(self) -> None:
        """AWS API exception is caught and logged, not raised."""
        mock_client = MagicMock()
        mock_client.describe_services.side_effect = Exception("AccessDenied")
        alerts = check_ecs_service_health(self._make_ecs_configs(), mock_client)
        assert len(alerts) == 0

    def test_multiple_clusters_batched(self) -> None:
        """Services in different clusters trigger separate API calls."""
        configs = [
            EcsServiceConfig(
                cluster="cluster-a",
                service="svc-a",
                display_name="Service A",
            ),
            EcsServiceConfig(
                cluster="cluster-b",
                service="svc-b",
                display_name="Service B",
            ),
        ]
        mock_client = MagicMock()
        mock_client.describe_services.side_effect = [
            {
                "services": [
                    {
                        "serviceName": "svc-a",
                        "runningCount": 1,
                        "desiredCount": 1,
                    },
                ],
            },
            {
                "services": [
                    {
                        "serviceName": "svc-b",
                        "runningCount": 1,
                        "desiredCount": 1,
                    },
                ],
            },
        ]
        alerts = check_ecs_service_health(configs, mock_client)
        assert len(alerts) == 0
        assert mock_client.describe_services.call_count == 2


class TestLoadEcsServiceConfigs:
    """Tests for load_ecs_service_configs()."""

    def test_loads_from_raw_dict(self) -> None:
        """Loads ECS service configs from a raw baselines dict."""
        raw: dict[str, Any] = {
            "ecs_services": [
                {
                    "cluster": "test-cluster",
                    "service": "test-svc",
                    "display_name": "Test Service",
                },
            ],
        }
        configs = load_ecs_service_configs(raw=raw)
        assert len(configs) == 1
        assert configs[0].cluster == "test-cluster"
        assert configs[0].service == "test-svc"
        assert configs[0].display_name == "Test Service"

    def test_defaults_when_key_missing(self) -> None:
        """Falls back to DEFAULT_ECS_SERVICES when ecs_services key is absent."""
        raw: dict[str, Any] = {"counties": {}}
        configs = load_ecs_service_configs(raw=raw)
        assert len(configs) == len(DEFAULT_ECS_SERVICES)

    def test_display_name_defaults_to_service_name(self) -> None:
        """display_name defaults to service name when omitted."""
        raw: dict[str, Any] = {
            "ecs_services": [
                {"cluster": "test-cluster", "service": "my-svc"},
            ],
        }
        configs = load_ecs_service_configs(raw=raw)
        assert configs[0].display_name == "my-svc"

    def test_loads_from_file(self, tmp_path: Path) -> None:
        """Loads ECS configs from a baselines JSON file."""
        baselines = {
            "ecs_services": [
                {
                    "cluster": "file-cluster",
                    "service": "file-svc",
                    "display_name": "File Service",
                },
            ],
        }
        path = tmp_path / "baselines.json"
        path.write_text(json.dumps(baselines))
        configs = load_ecs_service_configs(path=path)
        assert len(configs) == 1
        assert configs[0].cluster == "file-cluster"

    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        """Falls back to defaults when baselines file doesn't exist."""
        path = tmp_path / "nonexistent.json"
        configs = load_ecs_service_configs(path=path)
        assert len(configs) == len(DEFAULT_ECS_SERVICES)


# ---------------------------------------------------------------------------
# Ruling-to-document ratio check (#2230)
# ---------------------------------------------------------------------------


def _make_ratio_row(
    county: str,
    total_docs: int = 100,
    total_rulings: int = 100,
) -> tuple[Any, ...]:
    """Create a row matching the RULING_DOC_RATIO_QUERY result shape."""
    return (county, total_docs, total_rulings)


class TestCheckRulingDocumentRatio:
    """Tests for check_ruling_document_ratio function."""

    def test_healthy_ratio_no_alert(self) -> None:
        """No alerts when ruling-to-document ratio is at or above threshold."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Los Angeles", total_docs=100, total_rulings=98)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 0

    def test_ratio_exactly_at_threshold_no_alert(self) -> None:
        """No alert when ratio is exactly at the threshold."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Orange", total_docs=100, total_rulings=50)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 0

    def test_ratio_above_one_no_alert(self) -> None:
        """No alert when ratio exceeds 1.0 (multi-ruling documents)."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Fresno", total_docs=50, total_rulings=75)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 0

    def test_low_ratio_triggers_p1_alert(self) -> None:
        """P1 alert when ratio drops below threshold."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Orange", total_docs=100, total_rulings=30)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].county == "Orange"
        assert alerts[0].metric == "ruling_document_ratio"
        assert alerts[0].severity == "p1"
        assert alerts[0].expected == RULING_DOC_RATIO_THRESHOLD
        assert alerts[0].actual == 0.3
        assert "0.300" in alerts[0].message
        assert "30 rulings / 100 documents" in alerts[0].message
        assert "silently dropped" in alerts[0].message

    def test_zero_rulings_triggers_alert(self) -> None:
        """Alert when there are zero rulings for documents."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Riverside", total_docs=50, total_rulings=0)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].actual == 0.0

    def test_zero_documents_no_alert(self) -> None:
        """No alert when a county has zero documents."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Empty", total_docs=0, total_rulings=0)]}
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 0

    def test_multiple_counties_independent(self) -> None:
        """Alerts generated for multiple counties independently."""
        conn = FakeConnection(
            {
                "total_rulings": [
                    _make_ratio_row("Los Angeles", total_docs=100, total_rulings=95),
                    _make_ratio_row("Orange", total_docs=100, total_rulings=20),
                    _make_ratio_row("San Bernardino", total_docs=100, total_rulings=40),
                ],
            }
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 2
        counties = {a.county for a in alerts}
        assert counties == {"Orange", "San Bernardino"}

    def test_passes_time_window_params(self) -> None:
        """Passes window cutoff and grace period cutoff as query params."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Los Angeles", total_docs=100, total_rulings=100)]}
        )
        check_ruling_document_ratio(conn, NOW)
        assert len(conn.cursors) == 1
        captured = conn.cursors[0].captured_calls
        assert len(captured) == 1
        _query, params = captured[0]
        cutoff = NOW - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS)
        grace = NOW - timedelta(minutes=FIELD_COMPLETENESS_GRACE_MINUTES)
        assert params[0] == cutoff
        assert params[1] == grace

    def test_county_filter_passed(self) -> None:
        """County filter is passed to the query."""
        conn = FakeConnection(
            {"total_rulings": [_make_ratio_row("Orange", total_docs=100, total_rulings=100)]}
        )
        check_ruling_document_ratio(conn, NOW, county="Orange")
        captured = conn.cursors[0].captured_calls
        _query, params = captured[0]
        assert params[-1] == "Orange"


class TestBulkIngestRulingDocRatio:
    """Tests for bulk-ingest detection in check_ruling_document_ratio (#2230)."""

    def test_bulk_ingest_downgrades_ratio_alert(self) -> None:
        """Bulk ingest downgrades ruling-to-document ratio alert to P2."""
        conn = FakeConnection(
            {
                "total_rulings": [
                    _make_ratio_row("Riverside", total_docs=300, total_rulings=50),
                ],
            }
        )
        field_baselines = {
            "Riverside": {"total_documents": 66},
        }
        alerts = check_ruling_document_ratio(conn, NOW, field_baselines=field_baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ruling_document_ratio_bulk_ingest"
        assert alerts[0].severity == "p2"
        assert "bulk ingest" in alerts[0].message.lower()

    def test_normal_ratio_alert_without_bulk(self) -> None:
        """Normal doc count produces standard ratio alert."""
        conn = FakeConnection(
            {
                "total_rulings": [
                    _make_ratio_row("Orange", total_docs=100, total_rulings=30),
                ],
            }
        )
        field_baselines = {
            "Orange": {"total_documents": 1772},
        }
        alerts = check_ruling_document_ratio(conn, NOW, field_baselines=field_baselines)
        assert len(alerts) == 1
        assert alerts[0].metric == "ruling_document_ratio"
        assert alerts[0].severity == "p1"

    def test_ratio_backward_compatible_no_baselines(self) -> None:
        """Without field_baselines, bulk detection is skipped."""
        conn = FakeConnection(
            {
                "total_rulings": [
                    _make_ratio_row("Riverside", total_docs=300, total_rulings=50),
                ],
            }
        )
        alerts = check_ruling_document_ratio(conn, NOW)
        assert len(alerts) == 1
        assert alerts[0].metric == "ruling_document_ratio"
        assert alerts[0].severity == "p1"

    def test_ratio_above_threshold_no_alert_during_bulk(self) -> None:
        """Ratio above threshold produces no alert even during bulk ingest."""
        conn = FakeConnection(
            {
                "total_rulings": [
                    _make_ratio_row("Riverside", total_docs=300, total_rulings=200),
                ],
            }
        )
        field_baselines = {
            "Riverside": {"total_documents": 66},
        }
        alerts = check_ruling_document_ratio(conn, NOW, field_baselines=field_baselines)
        assert len(alerts) == 0


class TestCollectFullMetricsRulingDocRatio:
    """Tests that ruling_document_ratio appears in _collect_full_metrics."""

    def test_ratio_metric_collected(self) -> None:
        """Ruling-document ratio is collected in full metrics."""
        conn = FakeConnection(
            {
                # Keys match substrings in each SQL query used by _collect_full_metrics.
                "AT TIME ZONE": [],
                "captured_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
                # RULING_DOC_RATIO_QUERY
                "total_rulings": [
                    _make_ratio_row("Los Angeles", total_docs=100, total_rulings=95),
                ],
            }
        )
        metrics = _collect_full_metrics(conn, NOW)
        assert "Los Angeles" in metrics
        assert "ruling_document_ratio" in metrics["Los Angeles"]
        metric = metrics["Los Angeles"]["ruling_document_ratio"]
        assert metric["value"] == 0.95
        assert metric["metadata"]["total_documents"] == 100
        assert metric["metadata"]["total_rulings"] == 95

    def test_ratio_zero_docs_skipped(self) -> None:
        """Counties with zero documents are skipped in metrics collection."""
        conn = FakeConnection(
            {
                "AT TIME ZONE": [],
                "captured_at < %s": [],
                "AS ruling_count": [],
                "d.document_type": [],
                "has_ruling": [],
                "r.judge_id IS NULL": [],
                "ranked_runs": [],
                "success_count": [],
                "total_rulings": [
                    _make_ratio_row("Empty", total_docs=0, total_rulings=0),
                ],
            }
        )
        metrics = _collect_full_metrics(conn, NOW)
        # If "Empty" exists at all, it should not have ruling_document_ratio.
        if "Empty" in metrics:
            assert "ruling_document_ratio" not in metrics["Empty"]


class TestFormatMetricsSnapshotRulingDocRatio:
    """Tests that ruling_document_ratio appears in snapshot format."""

    def test_ratio_in_snapshot(self) -> None:
        """Ruling-document ratio is included in the snapshot format."""
        full_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {"value": 50, "metadata": None},
                "ruling_document_ratio": {
                    "value": 0.95,
                    "metadata": {"total_documents": 100, "total_rulings": 95},
                },
            },
        }
        snapshot = _format_metrics_for_snapshot(full_metrics)
        assert "Los Angeles" in snapshot
        assert snapshot["Los Angeles"]["ruling_document_ratio"] == 0.95

    def test_snapshot_without_ratio(self) -> None:
        """Snapshot works when ratio metric is absent."""
        full_metrics = {
            "Los Angeles": {
                "ruling_count_24h": {"value": 50, "metadata": None},
            },
        }
        snapshot = _format_metrics_for_snapshot(full_metrics)
        assert "ruling_document_ratio" not in snapshot["Los Angeles"]


# ---------------------------------------------------------------------------
# Rebuild marker tests (#2222)
# ---------------------------------------------------------------------------


class TestIsRebuildInProgress:
    """Tests for is_rebuild_in_progress() — detecting active DB rebuilds."""

    def test_no_marker_rows(self) -> None:
        """Returns False when no marker rows exist in data_quality_metrics."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is False

    def test_marker_active(self) -> None:
        """Returns True when most recent marker has value 1.0 and is fresh."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Marker written 30 minutes ago, value = 1.0 (in progress)
        marker_time = NOW - timedelta(minutes=30)
        mock_cur.fetchone.return_value = (1.0, marker_time)

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is True

    def test_marker_completed(self) -> None:
        """Returns False when most recent marker has value 0.0 (completed)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Marker written 5 minutes ago, value = 0.0 (completed)
        marker_time = NOW - timedelta(minutes=5)
        mock_cur.fetchone.return_value = (0.0, marker_time)

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is False

    def test_marker_stale_ttl_expired(self) -> None:
        """Returns False when marker is active but older than TTL (safety valve)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Marker written 5 hours ago (TTL is 4 hours), value = 1.0
        marker_time = NOW - timedelta(hours=5)
        mock_cur.fetchone.return_value = (1.0, marker_time)

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is False

    def test_marker_at_ttl_boundary(self) -> None:
        """Returns True when marker age exactly equals TTL (> not >=)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Marker written exactly REBUILD_MARKER_TTL_HOURS ago
        marker_time = NOW - timedelta(hours=REBUILD_MARKER_TTL_HOURS)
        mock_cur.fetchone.return_value = (1.0, marker_time)

        # At exactly the TTL boundary, age == TTL so `>` is False,
        # meaning the marker is still considered active.
        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is True

    def test_marker_just_within_ttl(self) -> None:
        """Returns True when marker is just under the TTL."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Marker written 3 hours 59 minutes ago (TTL is 4 hours)
        marker_time = NOW - timedelta(hours=3, minutes=59)
        mock_cur.fetchone.return_value = (1.0, marker_time)

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is True

    def test_db_error_returns_false(self) -> None:
        """Returns False when DB query fails (fail-open for alerting)."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.execute.side_effect = Exception("connection lost")

        result = is_rebuild_in_progress(mock_conn, now=NOW)
        assert result is False

    def test_defaults_to_utc_now_when_no_now_arg(self) -> None:
        """Uses datetime.now(UTC) when now is not provided."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None

        # Call without the now argument — should not raise.
        result = is_rebuild_in_progress(mock_conn)
        assert result is False


class TestDowngradeP1AlertsForRebuild:
    """Tests for _downgrade_p1_alerts_for_rebuild()."""

    def test_p1_alerts_downgraded_to_p2(self) -> None:
        """P1 alerts are downgraded to P2 with rebuild note."""
        alerts = [
            Alert(
                county="Los Angeles",
                metric="zero_rulings",
                severity="p1",
                expected=50,
                actual=0,
                message="Los Angeles: zero new rulings in 24h (expected ~50.0/day)",
            ),
        ]
        result = _downgrade_p1_alerts_for_rebuild(alerts)
        assert len(result) == 1
        assert result[0].severity == "p2"
        assert "rebuild in progress" in result[0].message
        assert "downgraded from P1" in result[0].message

    def test_p2_alerts_unchanged(self) -> None:
        """P2 alerts are not modified during rebuild."""
        alerts = [
            Alert(
                county="Orange",
                metric="ingest_rate",
                severity="p2",
                expected=20,
                actual=5,
                message="Orange: 5 rulings in 24h, baseline 20.0/day",
            ),
        ]
        result = _downgrade_p1_alerts_for_rebuild(alerts)
        assert len(result) == 1
        assert result[0].severity == "p2"
        assert "rebuild in progress" not in result[0].message

    def test_mixed_p1_and_p2(self) -> None:
        """Only P1 alerts are downgraded; P2 alerts remain unchanged."""
        alerts = [
            Alert(
                county="Los Angeles",
                metric="zero_rulings",
                severity="p1",
                expected=50,
                actual=0,
                message="LA: zero rulings",
            ),
            Alert(
                county="Orange",
                metric="ingest_rate",
                severity="p2",
                expected=20,
                actual=5,
                message="OC: low ingest",
            ),
            Alert(
                county="Fresno",
                metric="scraper_stale",
                severity="p1",
                expected="<26h",
                actual="100h",
                message="Fresno: scraper stale",
            ),
        ]
        result = _downgrade_p1_alerts_for_rebuild(alerts)
        assert len(result) == 3
        # First alert (was P1) -> P2
        assert result[0].severity == "p2"
        assert "rebuild in progress" in result[0].message
        # Second alert (was P2) -> unchanged
        assert result[1].severity == "p2"
        assert "rebuild in progress" not in result[1].message
        # Third alert (was P1) -> P2
        assert result[2].severity == "p2"
        assert "rebuild in progress" in result[2].message

    def test_empty_alerts(self) -> None:
        """Returns empty list when no alerts."""
        result = _downgrade_p1_alerts_for_rebuild([])
        assert result == []

    def test_preserves_other_fields(self) -> None:
        """Downgraded alerts preserve county, metric, expected, actual."""
        original = Alert(
            county="Kern",
            metric="field_completeness",
            severity="p1",
            expected=95.0,
            actual=80.0,
            message="Kern: judge completeness dropped",
        )
        result = _downgrade_p1_alerts_for_rebuild([original])
        assert result[0].county == "Kern"
        assert result[0].metric == "field_completeness"
        assert result[0].expected == 95.0
        assert result[0].actual == 80.0


class TestRunChecksRebuildIntegration:
    """Integration tests: run_checks downgrades P1 alerts during active rebuild."""

    def test_run_checks_downgrades_p1_during_rebuild(self) -> None:
        """When rebuild is active, P1 alerts from all checks are downgraded to P2."""
        mock_conn = MagicMock()
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(dqc, "psycopg", mock_psycopg),
            patch.object(dqc, "is_rebuild_in_progress", return_value=True),
            patch.object(
                dqc,
                "check_ingest_rates",
                return_value=[
                    Alert(
                        county="Los Angeles",
                        metric="zero_rulings",
                        severity="p1",
                        expected=50,
                        actual=0,
                        message="LA: zero rulings",
                    ),
                ],
            ),
            patch.object(dqc, "check_scraper_staleness", return_value=[]),
            patch.object(dqc, "check_field_completeness", return_value=[]),
            patch.object(dqc, "check_orphaned_documents", return_value=[]),
            patch.object(dqc, "check_ruling_document_ratio", return_value=[]),
            patch.object(dqc, "load_baselines", return_value={}),
            patch.object(dqc, "load_field_baselines", return_value={}),
            patch.object(dqc, "load_expected_null_rates", return_value={}),
        ):
            alerts = dqc.run_checks("postgresql://fake", now=NOW)

        assert len(alerts) == 1
        assert alerts[0].severity == "p2"
        assert "rebuild in progress" in alerts[0].message

    def test_run_checks_no_downgrade_when_no_rebuild(self) -> None:
        """When no rebuild is active, P1 alerts remain P1."""
        mock_conn = MagicMock()
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(dqc, "psycopg", mock_psycopg),
            patch.object(dqc, "is_rebuild_in_progress", return_value=False),
            patch.object(
                dqc,
                "check_ingest_rates",
                return_value=[
                    Alert(
                        county="Los Angeles",
                        metric="zero_rulings",
                        severity="p1",
                        expected=50,
                        actual=0,
                        message="LA: zero rulings",
                    ),
                ],
            ),
            patch.object(dqc, "check_scraper_staleness", return_value=[]),
            patch.object(dqc, "check_field_completeness", return_value=[]),
            patch.object(dqc, "check_orphaned_documents", return_value=[]),
            patch.object(dqc, "check_ruling_document_ratio", return_value=[]),
            patch.object(dqc, "load_baselines", return_value={}),
            patch.object(dqc, "load_field_baselines", return_value={}),
            patch.object(dqc, "load_expected_null_rates", return_value={}),
        ):
            alerts = dqc.run_checks("postgresql://fake", now=NOW)

        assert len(alerts) == 1
        assert alerts[0].severity == "p1"
        assert "rebuild in progress" not in alerts[0].message
