"""Cross-module integration test for the data quality output pipeline.

Validates the round-trip from data-quality-check.py's format_json() output
through format-dq-summary.py's extract_json_from_output() parser. This
catches format mismatches between the producer and consumer that unit tests
on each side might miss (e.g. indent=2 JSON with ECS log noise).

See: https://github.com/judgemind/judgemind/issues/893
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

# Add scripts/ to sys.path so we can import the modules.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# ruff: noqa: E402
# Import both modules (they have hyphens in their names).
dqc = import_module("data-quality-check")
fds = import_module("format-dq-summary")

Alert = dqc.Alert
format_json = dqc.format_json
extract_json_from_output = fds.extract_json_from_output
format_markdown_summary = fds.format_markdown_summary
format_telegram_summary = fds.format_telegram_summary

# Realistic ECS noise: status transitions and logging lines that appear
# around the actual script output in CloudWatch / ecs-run-task.sh captures.
_ECS_PREFIX = (
    "Status: PROVISIONING\n"
    "Status: PENDING\n"
    "Status: RUNNING\n"
    "2026-03-12 19:55:05,516 INFO     Connecting to database\n"
    "2026-03-12 19:55:06,102 INFO     Checking county: Los Angeles\n"
    "2026-03-12 19:55:06,400 INFO     Checking county: Orange\n"
    "2026-03-12 19:55:06,702 INFO     Checking county: Santa Clara\n"
    "2026-03-12 19:56:05,516 INFO     data_quality_check_complete alert_count={alert_count}\n"
)

_ECS_SUFFIX = "Status: DEPROVISIONING\nStatus: STOPPED\nExit code: {exit_code}\n"


def _wrap_in_ecs_output(json_output: str, alert_count: int) -> str:
    """Wrap JSON output in realistic ECS task noise.

    Args:
        json_output: The raw JSON string from format_json().
        alert_count: Number of alerts (used for log line and exit code).

    Returns:
        Simulated ECS task output with the JSON embedded.
    """
    exit_code = 1 if alert_count > 0 else 0
    return (
        _ECS_PREFIX.format(alert_count=alert_count)
        + json_output
        + "\n"
        + _ECS_SUFFIX.format(exit_code=exit_code)
    )


class TestDqPipelineRoundTrip:
    """End-to-end integration: format_json -> ECS noise -> extract_json_from_output."""

    def test_single_alert_round_trip(self) -> None:
        """Single P1 alert survives the producer -> ECS -> consumer pipeline."""
        alerts = [
            Alert(
                county="Los Angeles",
                metric="zero_rulings",
                severity="p1",
                expected="~10.0/day",
                actual=0,
                message="Los Angeles: zero new rulings in 24h (expected ~10.0/day)",
            ),
        ]

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=len(alerts))
        result = extract_json_from_output(ecs_output)

        assert result is not None
        assert result["healthy"] is False
        assert result["alert_count"] == 1
        assert len(result["alerts"]) == 1

        alert = result["alerts"][0]
        assert alert["county"] == "Los Angeles"
        assert alert["severity"] == "p1"
        assert alert["metric"] == "zero_rulings"
        assert alert["message"] == "Los Angeles: zero new rulings in 24h (expected ~10.0/day)"

    def test_multiple_alerts_mixed_severity_round_trip(self) -> None:
        """Multiple alerts with mixed P1/P2 severity survive the pipeline."""
        alerts = [
            Alert(
                county="Santa Clara",
                metric="scraper_stale",
                severity="p1",
                expected="<6h",
                actual="78.5h",
                message="Santa Clara: scraper stale for 78.5h (threshold: 6h)",
            ),
            Alert(
                county="Los Angeles",
                metric="scraper_stale",
                severity="p2",
                expected="<6h",
                actual="6.6h",
                message="Los Angeles: scraper stale for 6.6h (threshold: 6h)",
            ),
            Alert(
                county="Orange",
                metric="ingest_rate",
                severity="p2",
                expected=22.5,
                actual=4,
                message="Orange: 4 rulings in 24h, 7-day avg is 22.5/day (>50% drop)",
            ),
        ]

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=len(alerts))
        result = extract_json_from_output(ecs_output)

        assert result is not None
        assert result["healthy"] is False
        assert result["alert_count"] == 3
        assert len(result["alerts"]) == 3

        counties = [a["county"] for a in result["alerts"]]
        assert counties == ["Santa Clara", "Los Angeles", "Orange"]

        severities = [a["severity"] for a in result["alerts"]]
        assert severities == ["p1", "p2", "p2"]

        metrics = [a["metric"] for a in result["alerts"]]
        assert metrics == ["scraper_stale", "scraper_stale", "ingest_rate"]

    def test_healthy_no_alerts_round_trip(self) -> None:
        """Healthy output (no alerts) survives the pipeline."""
        alerts: list[Alert] = []

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=0)
        result = extract_json_from_output(ecs_output)

        assert result is not None
        assert result["healthy"] is True
        assert result["alert_count"] == 0
        assert result["alerts"] == []

    def test_field_completeness_alert_round_trip(self) -> None:
        """Field completeness regression alert survives the pipeline."""
        alerts = [
            Alert(
                county="San Diego",
                metric="field_completeness",
                severity="p2",
                expected="95%",
                actual="88%",
                message="San Diego: judge_name completeness dropped from 95% to 88%",
            ),
        ]

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=len(alerts))
        result = extract_json_from_output(ecs_output)

        assert result is not None
        assert result["alerts"][0]["county"] == "San Diego"
        assert result["alerts"][0]["metric"] == "field_completeness"
        assert "judge_name" in result["alerts"][0]["message"]


class TestDqPipelineToFormatters:
    """Integration: format_json -> ECS noise -> extract -> format_markdown/telegram."""

    def test_round_trip_to_markdown(self) -> None:
        """Alerts survive the full pipeline to markdown formatting."""
        alerts = [
            Alert(
                county="Los Angeles",
                metric="zero_rulings",
                severity="p1",
                expected="~10.0/day",
                actual=0,
                message="Los Angeles: zero new rulings in 24h",
            ),
            Alert(
                county="Orange",
                metric="ingest_rate",
                severity="p2",
                expected=20.0,
                actual=5,
                message="Orange: 5 rulings in 24h, 7-day avg is 20.0/day",
            ),
        ]

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=len(alerts))
        parsed = extract_json_from_output(ecs_output)
        assert parsed is not None

        markdown = format_markdown_summary(parsed)
        assert "2 alert(s)" in markdown
        assert "1 P1" in markdown
        assert "1 P2" in markdown
        assert "Los Angeles" in markdown
        assert "Orange" in markdown
        assert "Zero rulings (24h)" in markdown
        assert "Ingest rate drop" in markdown

    def test_round_trip_to_telegram(self) -> None:
        """Alerts survive the full pipeline to Telegram formatting."""
        alerts = [
            Alert(
                county="Santa Clara",
                metric="scraper_stale",
                severity="p1",
                expected="<6h",
                actual="78.5h",
                message="Santa Clara: scraper stale for 78.5h (threshold: 6h)",
            ),
        ]

        json_output = format_json(alerts)
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=len(alerts))
        parsed = extract_json_from_output(ecs_output)
        assert parsed is not None

        telegram = format_telegram_summary(parsed)
        assert "1 alert(s)" in telegram
        assert "1 P1" in telegram
        assert "Santa Clara" in telegram

    def test_healthy_round_trip_to_markdown(self) -> None:
        """Healthy output produces correct markdown."""
        json_output = format_json([])
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=0)
        parsed = extract_json_from_output(ecs_output)
        assert parsed is not None

        markdown = format_markdown_summary(parsed)
        assert markdown == "No alerts detected."

    def test_healthy_round_trip_to_telegram(self) -> None:
        """Healthy output produces correct Telegram summary."""
        json_output = format_json([])
        ecs_output = _wrap_in_ecs_output(json_output, alert_count=0)
        parsed = extract_json_from_output(ecs_output)
        assert parsed is not None

        telegram = format_telegram_summary(parsed)
        assert telegram == "All counties healthy."
