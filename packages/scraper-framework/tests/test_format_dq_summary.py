"""Tests for scripts/format-dq-summary.py — alert summary formatting."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

# Add scripts/ to sys.path so we can import the module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# Import the script as a module (hyphenated name).
fds = import_module("format-dq-summary")

extract_json_from_output = fds.extract_json_from_output
format_markdown_summary = fds.format_markdown_summary
format_telegram_summary = fds.format_telegram_summary


class TestExtractJsonFromOutput:
    """Tests for JSON extraction from noisy ECS output."""

    def test_clean_json_line(self) -> None:
        """Extract JSON from a single clean line."""
        data = json.dumps(
            {
                "healthy": False,
                "alert_count": 2,
                "alerts": [
                    {
                        "county": "Los Angeles",
                        "severity": "p1",
                        "metric": "zero_rulings",
                        "message": "Los Angeles: zero new rulings in 24h",
                    },
                    {
                        "county": "Orange",
                        "severity": "p2",
                        "metric": "ingest_rate",
                        "message": "Orange: 5 rulings in 24h, 7-day avg is 20.0/day",
                    },
                ],
            }
        )
        result = extract_json_from_output(data)
        assert result is not None
        assert result["alert_count"] == 2
        assert len(result["alerts"]) == 2

    def test_json_with_ecs_noise(self) -> None:
        """Extract JSON from output with ECS task management noise."""
        text = (
            "2026-03-11 15:00:00 INFO Starting ECS task...\n"
            "Task ARN: arn:aws:ecs:us-west-2:155326049300:task/abc123\n"
            "Waiting for task to complete...\n"
            "Task completed with exit code 1\n"
            "--- Task Output ---\n"
            "2026-03-11 15:01:00 INFO  dq_check alert_count=1\n"
            '{"healthy": false, "alert_count": 1, "alerts": '
            '[{"county": "LA", "severity": "p1", '
            '"metric": "zero_rulings", "message": "LA: zero"}]}\n'
            "--- End Output ---\n"
        )
        result = extract_json_from_output(text)
        assert result is not None
        assert result["healthy"] is False
        assert result["alert_count"] == 1

    def test_no_json_returns_none(self) -> None:
        """Return None when no JSON alert data is found."""
        text = """Some random log output
No JSON here
Task failed"""
        result = extract_json_from_output(text)
        assert result is None

    def test_text_output_parsing(self) -> None:
        """Parse human-readable text output format."""
        text = """Found 2 alert(s):

  [P1] Los Angeles: zero new rulings in 24h (expected ~10.0/day)
  [P2] Orange: 5 rulings in 24h, 7-day avg is 20.0/day (>50% drop)
"""
        result = extract_json_from_output(text)
        assert result is not None
        assert result["alert_count"] == 2
        assert result["alerts"][0]["severity"] == "p1"
        assert result["alerts"][0]["county"] == "Los Angeles"
        assert result["alerts"][1]["severity"] == "p2"
        assert result["alerts"][1]["county"] == "Orange"

    def test_multiline_indented_json(self) -> None:
        """Extract multi-line JSON with indent=2, as produced by data-quality-check.py."""
        payload = {
            "healthy": False,
            "alert_count": 2,
            "alerts": [
                {
                    "county": "Santa Clara",
                    "metric": "scraper_stale",
                    "severity": "p1",
                    "expected": "<6h",
                    "actual": "78.5h",
                    "message": "Santa Clara: scraper stale for 78.5h (threshold: 6h)",
                },
                {
                    "county": "Los Angeles",
                    "metric": "scraper_stale",
                    "severity": "p2",
                    "expected": "<6h",
                    "actual": "6.6h",
                    "message": "Los Angeles: scraper stale for 6.6h (threshold: 6h)",
                },
            ],
        }
        # Simulate ECS output: logging lines, then indented JSON, then status
        text = (
            "2026-03-12 19:55:05,516 INFO     Checking county: Los Angeles\n"
            "2026-03-12 19:56:05,516 INFO     data_quality_check_complete alert_count=2\n"
            + json.dumps(payload, indent=2)
            + "\n"
            "Status: DEPROVISIONING\n"
            "Status: STOPPED\n"
        )
        result = extract_json_from_output(text)
        assert result is not None
        assert result["healthy"] is False
        assert result["alert_count"] == 2
        assert len(result["alerts"]) == 2
        assert result["alerts"][0]["county"] == "Santa Clara"

    def test_multiline_json_with_log_prefixes(self) -> None:
        """Extract JSON even when lines have CloudWatch-style prefixes stripped."""
        # In practice, ecs-run-task.sh strips CloudWatch prefixes, but the
        # JSON is still multi-line with indent=2.
        text = (
            "Status: RUNNING\n"
            "2026-03-12 19:56:05,516 INFO     data_quality_check_complete\n"
            "{\n"
            '  "healthy": false,\n'
            '  "alert_count": 1,\n'
            '  "alerts": [\n'
            "    {\n"
            '      "county": "Orange",\n'
            '      "metric": "ingest_rate",\n'
            '      "severity": "p2",\n'
            '      "expected": 22.5,\n'
            '      "actual": 4,\n'
            '      "message": "Orange: 4 rulings in 24h"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "Status: STOPPED\n"
        )
        result = extract_json_from_output(text)
        assert result is not None
        assert result["healthy"] is False
        assert result["alert_count"] == 1
        assert result["alerts"][0]["county"] == "Orange"

    def test_healthy_json(self) -> None:
        """Extract healthy JSON output."""
        data = json.dumps({"healthy": True, "alert_count": 0, "alerts": []})
        result = extract_json_from_output(data)
        assert result is not None
        assert result["healthy"] is True
        assert result["alert_count"] == 0


class TestFormatMarkdownSummary:
    """Tests for markdown summary formatting."""

    def test_no_alerts(self) -> None:
        """Format when no alerts."""
        result = format_markdown_summary({"alerts": []})
        assert result == "No alerts detected."

    def test_single_p1_alert(self) -> None:
        """Format a single P1 alert."""
        data = {
            "alerts": [
                {
                    "county": "Los Angeles",
                    "severity": "p1",
                    "metric": "zero_rulings",
                    "message": "Los Angeles: zero new rulings in 24h (expected ~10.0/day)",
                }
            ]
        }
        result = format_markdown_summary(data)
        assert "1 alert(s)" in result
        assert "1 P1" in result
        assert "Los Angeles" in result
        assert "Zero rulings (24h)" in result
        assert "[P1]" in result

    def test_mixed_severity_alerts(self) -> None:
        """Format alerts with mixed P1 and P2 severity."""
        data = {
            "alerts": [
                {
                    "county": "Los Angeles",
                    "severity": "p1",
                    "metric": "zero_rulings",
                    "message": "LA: zero rulings",
                },
                {
                    "county": "Orange",
                    "severity": "p2",
                    "metric": "ingest_rate",
                    "message": "Orange: ingest rate drop",
                },
                {
                    "county": "San Diego",
                    "severity": "p2",
                    "metric": "scraper_stale",
                    "message": "San Diego: scraper stale",
                },
            ]
        }
        result = format_markdown_summary(data)
        assert "3 alert(s)" in result
        assert "1 P1" in result
        assert "2 P2" in result
        assert "3 county/counties" in result
        assert "Los Angeles" in result
        assert "Orange" in result
        assert "San Diego" in result

    def test_multiple_alert_types(self) -> None:
        """Alert type breakdown is included."""
        data = {
            "alerts": [
                {
                    "county": "LA",
                    "severity": "p1",
                    "metric": "zero_rulings",
                    "message": "LA: zero rulings",
                },
                {
                    "county": "LA",
                    "severity": "p2",
                    "metric": "field_completeness",
                    "message": "LA: judge completeness dropped",
                },
            ]
        }
        result = format_markdown_summary(data)
        assert "Zero rulings (24h): 1" in result
        assert "Field completeness regression: 1" in result


class TestFormatTelegramSummary:
    """Tests for Telegram one-liner formatting."""

    def test_no_alerts(self) -> None:
        """Telegram summary for healthy state."""
        result = format_telegram_summary({"alerts": []})
        assert result == "All counties healthy."

    def test_single_alert(self) -> None:
        """Telegram summary with a single alert."""
        data = {
            "alerts": [
                {
                    "county": "Los Angeles",
                    "severity": "p1",
                    "metric": "zero_rulings",
                    "message": "LA: zero rulings",
                },
            ]
        }
        result = format_telegram_summary(data)
        assert "1 alert(s)" in result
        assert "1 P1" in result
        assert "Los Angeles" in result

    def test_multiple_alerts(self) -> None:
        """Telegram summary with multiple alerts across counties."""
        data = {
            "alerts": [
                {
                    "county": "Los Angeles",
                    "severity": "p1",
                    "metric": "zero_rulings",
                    "message": "LA: zero",
                },
                {
                    "county": "Orange",
                    "severity": "p2",
                    "metric": "ingest_rate",
                    "message": "OC: drop",
                },
            ]
        }
        result = format_telegram_summary(data)
        assert "2 alert(s)" in result
        assert "1 P1" in result
        assert "1 P2" in result
        assert "Los Angeles" in result
        assert "Orange" in result
