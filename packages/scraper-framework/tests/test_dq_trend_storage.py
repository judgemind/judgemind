"""Tests for scripts/dq_trend_storage.py — trend storage, detection, and weekly summary."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

# Add scripts/ to sys.path so we can import the module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Skip venv re-exec during tests.
os.environ["_VENV_HELPER_SKIP"] = "1"

# ruff: noqa: E402
from dq_trend_storage import (
    TREND_WINDOW_DAYS,
    Snapshot,
    detect_trends,
    generate_weekly_summary,
    load_snapshots,
    store_snapshot,
)

NOW = datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)
BUCKET = "test-bucket"


def _make_snapshot(
    timestamp: datetime,
    counties: dict[str, dict[str, Any]] | None = None,
    alerts: list[dict[str, Any]] | None = None,
) -> Snapshot:
    """Create a test snapshot."""
    if counties is None:
        counties = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {
                    "judge": 95.0,
                    "motion_type": 88.0,
                    "outcome": 90.0,
                },
            },
        }
    return Snapshot(
        timestamp=timestamp.isoformat(),
        county_metrics=counties,
        alerts=alerts or [],
    )


@pytest.fixture()
def s3_client() -> Any:
    """Create a mocked S3 client with a test bucket."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        yield client


class TestStoreSnapshot:
    """Tests for store_snapshot function."""

    def test_stores_to_correct_key(self, s3_client: Any) -> None:
        """Snapshot is stored at data-quality/<date>/<hour>.json."""
        snap = _make_snapshot(datetime(2026, 3, 11, 14, 0, 0, tzinfo=UTC))
        key = store_snapshot(snap, bucket=BUCKET, s3_client=s3_client)
        assert key == "data-quality/2026-03-11/14.json"

        # Verify the object exists and contains correct data.
        obj = s3_client.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        assert data["timestamp"] == snap.timestamp
        assert "Los Angeles" in data["county_metrics"]

    def test_stores_alerts(self, s3_client: Any) -> None:
        """Snapshot includes alerts in the stored JSON."""
        alerts = [{"county": "LA", "metric": "zero_rulings", "severity": "p1"}]
        snap = _make_snapshot(NOW, alerts=alerts)
        key = store_snapshot(snap, bucket=BUCKET, s3_client=s3_client)

        obj = s3_client.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        assert len(data["alerts"]) == 1
        assert data["alerts"][0]["severity"] == "p1"

    def test_stores_empty_metrics(self, s3_client: Any) -> None:
        """Handles snapshot with no county metrics."""
        snap = _make_snapshot(NOW, counties={})
        key = store_snapshot(snap, bucket=BUCKET, s3_client=s3_client)

        obj = s3_client.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        assert data["county_metrics"] == {}


class TestLoadSnapshots:
    """Tests for load_snapshots function."""

    def test_loads_stored_snapshots(self, s3_client: Any) -> None:
        """Loads snapshots that were previously stored."""
        snap1 = _make_snapshot(NOW - timedelta(hours=2))
        snap2 = _make_snapshot(NOW)
        store_snapshot(snap1, bucket=BUCKET, s3_client=s3_client)
        store_snapshot(snap2, bucket=BUCKET, s3_client=s3_client)

        loaded = load_snapshots(days=1, now=NOW, bucket=BUCKET, s3_client=s3_client)
        assert len(loaded) == 2
        # Should be sorted by timestamp ascending.
        assert loaded[0].timestamp <= loaded[1].timestamp

    def test_loads_across_multiple_days(self, s3_client: Any) -> None:
        """Loads snapshots spanning multiple days."""
        for day_offset in range(3):
            ts = NOW - timedelta(days=day_offset)
            snap = _make_snapshot(ts)
            store_snapshot(snap, bucket=BUCKET, s3_client=s3_client)

        loaded = load_snapshots(days=3, now=NOW, bucket=BUCKET, s3_client=s3_client)
        assert len(loaded) == 3

    def test_empty_bucket_returns_empty(self, s3_client: Any) -> None:
        """Returns empty list when no snapshots exist."""
        loaded = load_snapshots(days=7, now=NOW, bucket=BUCKET, s3_client=s3_client)
        assert loaded == []

    def test_respects_day_limit(self, s3_client: Any) -> None:
        """Only loads snapshots within the requested day range."""
        # Store a snapshot 10 days ago.
        old_snap = _make_snapshot(NOW - timedelta(days=10))
        store_snapshot(old_snap, bucket=BUCKET, s3_client=s3_client)

        # Store a snapshot today.
        new_snap = _make_snapshot(NOW)
        store_snapshot(new_snap, bucket=BUCKET, s3_client=s3_client)

        loaded = load_snapshots(days=3, now=NOW, bucket=BUCKET, s3_client=s3_client)
        # Only today's snapshot should be loaded (old one is outside window).
        assert len(loaded) == 1

    def test_handles_corrupt_json(self, s3_client: Any) -> None:
        """Skips snapshots with invalid JSON without crashing."""
        # Store a valid snapshot.
        snap = _make_snapshot(NOW)
        store_snapshot(snap, bucket=BUCKET, s3_client=s3_client)

        # Put corrupt data at another key.
        s3_client.put_object(
            Bucket=BUCKET,
            Key="data-quality/2026-03-11/99.json",
            Body=b"not valid json",
        )

        loaded = load_snapshots(days=1, now=NOW, bucket=BUCKET, s3_client=s3_client)
        # Should still load the valid snapshot.
        assert len(loaded) == 1


class TestDetectTrends:
    """Tests for detect_trends function."""

    def _make_historical_snapshots(
        self,
        prior_values: dict[str, dict[str, Any]],
        recent_values: dict[str, dict[str, Any]],
        num_each: int = 3,
    ) -> list[Snapshot]:
        """Create snapshots split across the trend window.

        Args:
            prior_values: County metrics for the older half.
            recent_values: County metrics for the recent half.
            num_each: Number of snapshots per half.

        Returns:
            List of snapshots covering the full trend window.
        """
        snapshots: list[Snapshot] = []
        midpoint = NOW - timedelta(days=TREND_WINDOW_DAYS / 2)

        for i in range(num_each):
            # Prior snapshots (older half).
            ts = midpoint - timedelta(hours=(num_each - i) * 6)
            snapshots.append(_make_snapshot(ts, counties=prior_values))

        for i in range(num_each):
            # Recent snapshots (newer half).
            ts = midpoint + timedelta(hours=i * 6)
            snapshots.append(_make_snapshot(ts, counties=recent_values))

        snapshots.sort(key=lambda s: s.timestamp)
        return snapshots

    def test_no_trends_with_stable_data(self) -> None:
        """No alerts when metrics are stable."""
        metrics = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 95.0},
            },
        }
        snapshots = self._make_historical_snapshots(metrics, metrics)
        alerts = detect_trends(snapshots, now=NOW)
        assert len(alerts) == 0

    def test_detects_field_completeness_decline(self) -> None:
        """Flags a county with declining field completeness."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 95.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 85.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        field_alerts = [a for a in alerts if "field_trend" in a.metric]
        assert len(field_alerts) == 1
        assert field_alerts[0].county == "Los Angeles"
        assert field_alerts[0].severity == "p2"
        assert field_alerts[0].prior_avg == 95.0
        assert field_alerts[0].current_avg == 85.0

    def test_detects_ruling_count_decline(self) -> None:
        """Flags a county with declining ruling counts."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 40,
                "field_completeness": {"judge": 95.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        count_alerts = [a for a in alerts if a.metric == "ruling_count_trend"]
        assert len(count_alerts) == 1
        assert count_alerts[0].county == "Los Angeles"
        assert count_alerts[0].severity == "p2"

    def test_ignores_small_decline(self) -> None:
        """Does not flag declines below the threshold."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 49,
                "field_completeness": {"judge": 93.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        assert len(alerts) == 0

    def test_ignores_improving_trends(self) -> None:
        """Does not flag counties with improving metrics."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 40,
                "field_completeness": {"judge": 85.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        assert len(alerts) == 0

    def test_too_few_snapshots_returns_empty(self) -> None:
        """Returns empty when fewer than 2 snapshots exist."""
        snap = _make_snapshot(NOW)
        alerts = detect_trends([snap], now=NOW)
        assert len(alerts) == 0

    def test_empty_snapshots_returns_empty(self) -> None:
        """Returns empty for an empty list."""
        alerts = detect_trends([], now=NOW)
        assert len(alerts) == 0

    def test_multiple_counties(self) -> None:
        """Detects trends for multiple counties independently."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
            "Orange": {
                "ruling_count_24h": 20,
                "field_completeness": {"judge": 90.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 40,
                "field_completeness": {"judge": 80.0},
            },
            "Orange": {
                "ruling_count_24h": 20,
                "field_completeness": {"judge": 90.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        # LA should have field and count trend alerts; OC should be clean.
        la_alerts = [a for a in alerts if a.county == "Los Angeles"]
        oc_alerts = [a for a in alerts if a.county == "Orange"]
        assert len(la_alerts) >= 2
        assert len(oc_alerts) == 0

    def test_ignores_zero_prior_avg(self) -> None:
        """Does not alert when prior average is zero."""
        prior = {
            "New County": {
                "ruling_count_24h": 0,
                "field_completeness": {"judge": 0.0},
            },
        }
        recent = {
            "New County": {
                "ruling_count_24h": 0,
                "field_completeness": {"judge": 50.0},
            },
        }
        snapshots = self._make_historical_snapshots(prior, recent)
        alerts = detect_trends(snapshots, now=NOW)
        assert len(alerts) == 0


class TestGenerateWeeklySummary:
    """Tests for generate_weekly_summary function."""

    def test_empty_snapshots(self) -> None:
        """Produces a report even with no data."""
        report = generate_weekly_summary([], now=NOW)
        assert "Weekly Summary" in report
        assert "No historical data" in report

    def test_contains_overall_rate(self) -> None:
        """Report includes overall collection rate section."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 95.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
        }
        midpoint = NOW - timedelta(days=TREND_WINDOW_DAYS / 2)
        snapshots = [
            _make_snapshot(midpoint - timedelta(hours=6), counties=prior),
            _make_snapshot(midpoint + timedelta(hours=6), counties=recent),
        ]
        report = generate_weekly_summary(snapshots, now=NOW)
        assert "Overall Collection Rate" in report
        assert "Active counties" in report

    def test_contains_field_trends_table(self) -> None:
        """Report includes field completeness trends table."""
        metrics = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 95.0, "motion_type": 88.0},
            },
        }
        midpoint = NOW - timedelta(days=TREND_WINDOW_DAYS / 2)
        snapshots = [
            _make_snapshot(midpoint - timedelta(hours=6), counties=metrics),
            _make_snapshot(midpoint + timedelta(hours=6), counties=metrics),
        ]
        report = generate_weekly_summary(snapshots, now=NOW)
        assert "Field Completeness Trends" in report
        assert "Los Angeles" in report
        assert "judge" in report
        assert "stable" in report

    def test_contains_downward_trends_section(self) -> None:
        """Report includes sustained downward trends section."""
        prior = {
            "Los Angeles": {
                "ruling_count_24h": 50,
                "field_completeness": {"judge": 95.0},
            },
        }
        recent = {
            "Los Angeles": {
                "ruling_count_24h": 30,
                "field_completeness": {"judge": 80.0},
            },
        }
        midpoint = NOW - timedelta(days=TREND_WINDOW_DAYS / 2)
        snapshots = [
            _make_snapshot(midpoint - timedelta(hours=6), counties=prior),
            _make_snapshot(midpoint + timedelta(hours=6), counties=recent),
        ]
        report = generate_weekly_summary(snapshots, now=NOW)
        assert "Sustained Downward Trends" in report
        assert "Los Angeles" in report

    def test_contains_recent_alerts(self) -> None:
        """Report includes alerts from the most recent snapshot."""
        alerts = [{"county": "LA", "severity": "p1", "message": "LA zero rulings"}]
        snap = _make_snapshot(NOW, alerts=alerts)
        report = generate_weekly_summary([snap], now=NOW)
        assert "Recent Alerts" in report
        assert "[P1]" in report

    def test_report_footer(self) -> None:
        """Report includes generation timestamp."""
        snap = _make_snapshot(NOW)
        report = generate_weekly_summary([snap], now=NOW)
        assert "2026-03-11" in report
        assert "snapshots" in report

    def test_no_county_metrics(self) -> None:
        """Handles snapshots with empty county metrics."""
        snap = _make_snapshot(NOW, counties={})
        report = generate_weekly_summary([snap], now=NOW)
        assert "No county metrics" in report

    def test_multiple_counties_in_report(self) -> None:
        """Report covers multiple counties."""
        metrics = {
            "Los Angeles": {
                "ruling_count_24h": 45,
                "field_completeness": {"judge": 95.0},
            },
            "Orange": {
                "ruling_count_24h": 20,
                "field_completeness": {"judge": 90.0},
            },
        }
        midpoint = NOW - timedelta(days=TREND_WINDOW_DAYS / 2)
        snapshots = [
            _make_snapshot(midpoint - timedelta(hours=6), counties=metrics),
            _make_snapshot(midpoint + timedelta(hours=6), counties=metrics),
        ]
        report = generate_weekly_summary(snapshots, now=NOW)
        assert "Los Angeles" in report
        assert "Orange" in report
