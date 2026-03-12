#!/usr/bin/env python3
"""Format a data quality check summary from captured ECS task output.

Reads the output of data-quality-check.py (captured from ECS via CloudWatch
logs) and produces a human-readable markdown summary suitable for embedding
in GitHub issues and Telegram notifications.

The script searches the input for a JSON object containing alert data and
formats it into a structured summary.

Usage:
    python3 scripts/format-dq-summary.py /tmp/check_output.txt
    python3 scripts/format-dq-summary.py --telegram /tmp/check_output.txt
    cat output.txt | python3 scripts/format-dq-summary.py -

Arguments:
    FILE           Path to the captured output file, or '-' for stdin.

Options:
    --telegram     Output a one-line Telegram-friendly summary instead
                   of the full markdown summary.

Exit code: 0 on success, 1 if no alert data could be extracted.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any


def extract_json_from_output(text: str) -> dict[str, Any] | None:
    """Extract the JSON alert payload from noisy ECS task output.

    The data-quality-check.py script outputs a JSON object like:
        {"healthy": false, "alert_count": N, "alerts": [...]}

    This function searches the text for that JSON block, handling the
    case where CloudWatch log lines may have timestamps or other prefixes.

    Args:
        text: Raw captured output from ecs-run-task.sh.

    Returns:
        Parsed JSON dict, or None if no valid alert JSON was found.
    """
    # Strategy 1: Try to find a single line containing the full JSON.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and '"healthy"' in stripped:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                continue

    # Strategy 2: Reconstruct multi-line JSON block.
    # When data-quality-check.py uses indent=2, the opening '{' is on its
    # own line and '"healthy"' is on the next.  Collect lines from the first
    # standalone '{' through the matching top-level '}' and parse.
    lines = text.splitlines()
    json_start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "{" and json_start is None:
            # Peek ahead to see if the next non-empty line looks like JSON
            for j in range(i + 1, min(i + 5, len(lines))):
                if '"healthy"' in lines[j]:
                    json_start = i
                    break
            if json_start is not None:
                break

    if json_start is not None:
        # Walk forward tracking brace depth to find the closing '}'
        depth = 0
        json_lines: list[str] = []
        for i in range(json_start, len(lines)):
            stripped = lines[i].strip()
            json_lines.append(stripped)
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                break
        try:
            return json.loads("\n".join(json_lines))
        except json.JSONDecodeError:
            pass

    # Strategy 3: Regex fallback for compact or slightly noisy JSON.
    match = re.search(
        r'\{\s*"healthy"\s*:.*?"alerts"\s*:\s*\[.*?\]\s*\}',
        text,
        re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3: Try the text output format (--text flag).
    # "Found N alert(s):" followed by "[P1] message" lines.
    alerts = _parse_text_output(text)
    if alerts:
        return {
            "healthy": False,
            "alert_count": len(alerts),
            "alerts": alerts,
        }

    return None


def _parse_text_output(text: str) -> list[dict[str, Any]]:
    """Parse human-readable text output into alert dicts.

    Args:
        text: Raw output that may contain text-format alerts.

    Returns:
        List of alert dicts, or empty list if no text alerts found.
    """
    alerts: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"\[(P[12])\]\s+(.+)", stripped)
        if match:
            severity = match.group(1).lower()
            message = match.group(2)
            # Try to extract county from the message (typically "County: ...")
            county_match = re.match(r"^([^:]+):", message)
            county = county_match.group(1).strip() if county_match else "Unknown"
            alerts.append(
                {
                    "county": county,
                    "severity": severity,
                    "message": message,
                    "metric": _infer_metric(message),
                }
            )
    return alerts


def _infer_metric(message: str) -> str:
    """Infer the alert metric type from the message text.

    Args:
        message: Alert message string.

    Returns:
        Metric type string.
    """
    lower = message.lower()
    if "zero" in lower and "ruling" in lower:
        return "zero_rulings"
    if "stale" in lower:
        return "scraper_stale"
    if "completeness" in lower or "dropped" in lower:
        return "field_completeness"
    if "ingest" in lower or "drop" in lower:
        return "ingest_rate"
    return "unknown"


def format_markdown_summary(data: dict[str, Any]) -> str:
    """Format alert data as a markdown summary for GitHub issues.

    Args:
        data: Parsed JSON alert data with 'alerts' list.

    Returns:
        Markdown-formatted summary string.
    """
    alerts = data.get("alerts", [])
    if not alerts:
        return "No alerts detected."

    alert_count = len(alerts)

    # Group alerts by severity
    p1_alerts = [a for a in alerts if a.get("severity") == "p1"]
    p2_alerts = [a for a in alerts if a.get("severity") == "p2"]

    # Group by metric type
    metric_counts: Counter[str] = Counter()
    for a in alerts:
        metric_counts[a.get("metric", "unknown")] += 1

    # Collect affected counties
    counties = sorted({a.get("county", "Unknown") for a in alerts})

    lines: list[str] = []

    # Summary line
    severity_parts: list[str] = []
    if p1_alerts:
        severity_parts.append(f"{len(p1_alerts)} P1")
    if p2_alerts:
        severity_parts.append(f"{len(p2_alerts)} P2")
    severity_str = ", ".join(severity_parts)
    lines.append(
        f"**{alert_count} alert(s)** ({severity_str}) across "
        f"**{len(counties)} county/counties**: {', '.join(counties)}"
    )
    lines.append("")

    # Alert type breakdown
    metric_display = {
        "zero_rulings": "Zero rulings (24h)",
        "ingest_rate": "Ingest rate drop",
        "scraper_stale": "Scraper stale",
        "field_completeness": "Field completeness regression",
    }

    lines.append("**Alert types:**")
    for metric, count in metric_counts.most_common():
        display = metric_display.get(metric, metric)
        lines.append(f"- {display}: {count}")
    lines.append("")

    # Detailed alert list
    lines.append("**Details:**")
    lines.append("")
    for a in alerts:
        severity_tag = "P1" if a.get("severity") == "p1" else "P2"
        message = a.get("message", "No details available")
        lines.append(f"- **[{severity_tag}]** {message}")

    return "\n".join(lines)


def format_telegram_summary(data: dict[str, Any]) -> str:
    """Format alert data as a concise one-line Telegram summary.

    Args:
        data: Parsed JSON alert data with 'alerts' list.

    Returns:
        HTML-formatted one-line summary suitable for Telegram.
    """
    alerts = data.get("alerts", [])
    if not alerts:
        return "All counties healthy."

    p1_count = sum(1 for a in alerts if a.get("severity") == "p1")
    p2_count = sum(1 for a in alerts if a.get("severity") == "p2")
    counties = sorted({a.get("county", "Unknown") for a in alerts})

    parts: list[str] = []
    if p1_count:
        parts.append(f"{p1_count} P1")
    if p2_count:
        parts.append(f"{p2_count} P2")

    return f"{len(alerts)} alert(s) ({', '.join(parts)}): {', '.join(counties)}"


def main() -> None:
    """CLI entry point."""
    telegram_mode = "--telegram" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--telegram"]

    if not args:
        print("Usage: format-dq-summary.py [--telegram] <file|->", file=sys.stderr)
        sys.exit(1)

    file_path = args[0]
    if file_path == "-":
        text = sys.stdin.read()
    else:
        with open(file_path) as f:
            text = f.read()

    data = extract_json_from_output(text)
    if data is None:
        print("Could not extract alert data from output.", file=sys.stderr)
        sys.exit(1)

    if telegram_mode:
        print(format_telegram_summary(data))
    else:
        print(format_markdown_summary(data))


if __name__ == "__main__":
    main()
