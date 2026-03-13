"""Tests for api-error-check.py.

Tests cover CloudWatch metric querying, threshold evaluation, and output
formatting. All tests are offline — boto3 calls are mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The script uses a hyphenated filename so we import via importlib
import importlib

api_error_check = importlib.import_module("api-error-check")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_datapoint(sum_val: float = 0, p99_val: float | None = None) -> dict:
    """Build a mock CloudWatch datapoint."""
    dp: dict = {"Timestamp": "2026-03-12T12:00:00Z"}
    if sum_val is not None:
        dp["Sum"] = sum_val
    if p99_val is not None:
        dp["ExtendedStatistics"] = {"p99": p99_val}
    return dp


def _mock_cloudwatch(
    count_5xx: float = 0,
    count_4xx: float = 0,
    count_2xx: float = 100,
    p99_latency: float | None = 0.5,
) -> MagicMock:
    """Create a mock CloudWatch client with preconfigured metric responses."""
    client = MagicMock()

    def get_metric_statistics(**kwargs: object) -> dict:
        metric = kwargs.get("MetricName", "")
        if "Statistics" in kwargs:
            if metric == "HTTPCode_Target_5XX_Count":
                return {"Datapoints": [_make_datapoint(sum_val=count_5xx)]}
            if metric == "HTTPCode_Target_4XX_Count":
                return {"Datapoints": [_make_datapoint(sum_val=count_4xx)]}
            if metric == "HTTPCode_Target_2XX_Count":
                return {"Datapoints": [_make_datapoint(sum_val=count_2xx)]}
        if "ExtendedStatistics" in kwargs:
            if p99_latency is not None:
                return {"Datapoints": [_make_datapoint(p99_val=p99_latency)]}
            return {"Datapoints": []}
        return {"Datapoints": []}

    client.get_metric_statistics = MagicMock(side_effect=get_metric_statistics)
    return client


# ---------------------------------------------------------------------------
# check_api_errors tests
# ---------------------------------------------------------------------------


class TestCheckApiErrors:
    """Tests for the check_api_errors function."""

    @patch("boto3.client")
    def test_healthy_metrics(self, mock_boto: MagicMock) -> None:
        """All metrics within thresholds returns healthy=True."""
        mock_boto.return_value = _mock_cloudwatch(
            count_5xx=0, count_4xx=10, count_2xx=500, p99_latency=0.2
        )

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is True
        assert len(result["alerts"]) == 0
        assert result["metrics"]["5xx_count"] == 0
        assert result["metrics"]["4xx_count"] == 10
        assert result["metrics"]["2xx_count"] == 500

    @patch("boto3.client")
    def test_5xx_alert(self, mock_boto: MagicMock) -> None:
        """5xx count exceeding threshold triggers alert."""
        mock_boto.return_value = _mock_cloudwatch(count_5xx=15)

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is False
        assert len(result["alerts"]) == 1
        assert result["alerts"][0]["metric"] == "5xx_errors"
        assert result["alerts"][0]["value"] == 15

    @patch("boto3.client")
    def test_4xx_alert(self, mock_boto: MagicMock) -> None:
        """4xx count exceeding threshold triggers alert."""
        mock_boto.return_value = _mock_cloudwatch(count_4xx=150)

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is False
        assert any(a["metric"] == "4xx_errors" for a in result["alerts"])

    @patch("boto3.client")
    def test_latency_alert(self, mock_boto: MagicMock) -> None:
        """P99 latency exceeding threshold triggers alert."""
        mock_boto.return_value = _mock_cloudwatch(p99_latency=7.5)

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is False
        assert any(a["metric"] == "p99_latency" for a in result["alerts"])
        assert result["metrics"]["p99_latency_seconds"] == 7.5

    @patch("boto3.client")
    def test_no_traffic_is_healthy(self, mock_boto: MagicMock) -> None:
        """No traffic (no datapoints) should be treated as healthy."""
        mock_boto.return_value = _mock_cloudwatch(
            count_5xx=0, count_4xx=0, count_2xx=0, p99_latency=None
        )

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is True
        assert result["metrics"]["p99_latency_seconds"] is None

    @patch("boto3.client")
    def test_multiple_alerts(self, mock_boto: MagicMock) -> None:
        """Multiple thresholds breached produce multiple alerts."""
        mock_boto.return_value = _mock_cloudwatch(
            count_5xx=20, count_4xx=200, p99_latency=10.0
        )

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is False
        assert len(result["alerts"]) == 3
        alert_metrics = {a["metric"] for a in result["alerts"]}
        assert alert_metrics == {"5xx_errors", "4xx_errors", "p99_latency"}

    @patch("boto3.client")
    def test_exact_threshold_triggers(self, mock_boto: MagicMock) -> None:
        """Exact threshold value should trigger (>=)."""
        mock_boto.return_value = _mock_cloudwatch(count_5xx=10)

        result = api_error_check.check_api_errors(
            alb_arn_suffix="app/test/123",
            tg_arn_suffix="targetgroup/test/456",
            period_minutes=60,
            threshold_5xx=10,
            threshold_4xx=100,
            threshold_latency=5.0,
        )

        assert result["healthy"] is False
        assert result["alerts"][0]["metric"] == "5xx_errors"


# ---------------------------------------------------------------------------
# Output formatting tests
# ---------------------------------------------------------------------------


class TestFormatText:
    """Tests for text output formatting."""

    def test_healthy_output(self) -> None:
        """Healthy result includes HEALTHY status."""
        result = {
            "timestamp": "2026-03-12T12:00:00Z",
            "period_minutes": 60,
            "metrics": {
                "5xx_count": 0,
                "4xx_count": 5,
                "2xx_count": 100,
                "p99_latency_seconds": 0.25,
            },
            "alerts": [],
            "healthy": True,
        }
        text = api_error_check.format_text(result)
        assert "HEALTHY" in text
        assert "ALERT" not in text

    def test_alert_output(self) -> None:
        """Alert result includes ALERT status and details."""
        result = {
            "timestamp": "2026-03-12T12:00:00Z",
            "period_minutes": 60,
            "metrics": {
                "5xx_count": 15,
                "4xx_count": 5,
                "2xx_count": 100,
                "p99_latency_seconds": 0.25,
            },
            "alerts": [
                {
                    "metric": "5xx_errors",
                    "value": 15,
                    "threshold": 10,
                    "message": "5xx error count (15) exceeded threshold (10)",
                }
            ],
            "healthy": False,
        }
        text = api_error_check.format_text(result)
        assert "ALERT" in text
        assert "5xx error count" in text

    def test_no_traffic_output(self) -> None:
        """No traffic formats latency as N/A."""
        result = {
            "timestamp": "2026-03-12T12:00:00Z",
            "period_minutes": 60,
            "metrics": {
                "5xx_count": 0,
                "4xx_count": 0,
                "2xx_count": 0,
                "p99_latency_seconds": None,
            },
            "alerts": [],
            "healthy": True,
        }
        text = api_error_check.format_text(result)
        assert "N/A" in text


# ---------------------------------------------------------------------------
# get_metric_sum / get_metric_p99 unit tests
# ---------------------------------------------------------------------------


class TestGetMetricSum:
    """Tests for the get_metric_sum helper."""

    def test_sums_datapoints(self) -> None:
        """Multiple datapoints are summed."""
        from datetime import UTC, datetime

        client = MagicMock()
        client.get_metric_statistics.return_value = {
            "Datapoints": [
                {"Sum": 5.0},
                {"Sum": 3.0},
            ]
        }

        result = api_error_check.get_metric_sum(
            client, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count",
            [], datetime.now(UTC), datetime.now(UTC), 300,
        )
        assert result == 8.0

    def test_empty_datapoints(self) -> None:
        """No datapoints returns 0."""
        from datetime import UTC, datetime

        client = MagicMock()
        client.get_metric_statistics.return_value = {"Datapoints": []}

        result = api_error_check.get_metric_sum(
            client, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count",
            [], datetime.now(UTC), datetime.now(UTC), 300,
        )
        assert result == 0.0


class TestGetMetricP99:
    """Tests for the get_metric_p99 helper."""

    def test_returns_max_p99(self) -> None:
        """Multiple datapoints returns the max p99."""
        from datetime import UTC, datetime

        client = MagicMock()
        client.get_metric_statistics.return_value = {
            "Datapoints": [
                {"ExtendedStatistics": {"p99": 1.5}},
                {"ExtendedStatistics": {"p99": 2.3}},
            ]
        }

        result = api_error_check.get_metric_p99(
            client, "AWS/ApplicationELB", "TargetResponseTime",
            [], datetime.now(UTC), datetime.now(UTC), 300,
        )
        assert result == 2.3

    def test_no_datapoints_returns_none(self) -> None:
        """No datapoints returns None."""
        from datetime import UTC, datetime

        client = MagicMock()
        client.get_metric_statistics.return_value = {"Datapoints": []}

        result = api_error_check.get_metric_p99(
            client, "AWS/ApplicationELB", "TargetResponseTime",
            [], datetime.now(UTC), datetime.now(UTC), 300,
        )
        assert result is None
