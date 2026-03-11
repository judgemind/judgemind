#!/usr/bin/env python3
"""Data quality monitoring — collection health and field completeness checks.

Queries the database and flags counties with unhealthy ruling ingest
rates, stale scrapers, zero new rulings, or field completeness regressions.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/data-quality-check.py

Options:
    --json              Machine-readable JSON output (default).
    --text              Human-readable text output.
    --county NAME       Check only the specified county.
    --update-baselines  Snapshot current field completeness as baselines (ratchet up only).

Exit code: 0 if all healthy, 1 if alerts found.
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

# ruff: noqa: E402
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# Resolve repo root from scripts/ directory.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent

DEFAULT_BASELINES_PATH = _REPO_ROOT / "data-quality-baselines.json"

# Thresholds
INGEST_DROP_THRESHOLD = 0.5  # Flag if below 50% of 7-day average
DAILY_SCRAPER_STALE_HOURS = 6
FREQUENT_SCRAPER_STALE_HOURS = 2

# Field completeness regression thresholds (percentage points below baseline).
FIELD_DROP_P1_THRESHOLD = 10.0  # >10pp drop = p1
FIELD_DROP_P2_THRESHOLD = 5.0   # 5-10pp drop = p2

# Window for recent-only field completeness checks (days).
FIELD_COMPLETENESS_WINDOW_DAYS = 7


@dataclass
class Alert:
    """A single data quality alert."""

    county: str
    metric: str  # ingest_rate, scraper_stale, zero_rulings, field_completeness
    severity: str  # p1, p2
    expected: float | int | str
    actual: float | int | str
    message: str


@dataclass
class Baselines:
    """Per-county baseline configuration."""

    expected_daily_rulings: float
    schedule_type: str  # daily, frequent


def load_baselines(path: Path | None = None) -> dict[str, Baselines]:
    """Load per-county baselines from JSON config file.

    Args:
        path: Path to baselines JSON file. Defaults to repo root.

    Returns:
        Dict mapping county name to Baselines.
    """
    baselines_path = path or DEFAULT_BASELINES_PATH
    if not baselines_path.exists():
        logger.warning(
            "Baselines file not found at %s, using empty baselines", baselines_path
        )
        return {}
    with open(baselines_path) as f:
        raw = json.load(f)
    result: dict[str, Baselines] = {}
    for county, config in raw.get("counties", {}).items():
        result[county] = Baselines(
            expected_daily_rulings=config.get("expected_daily_rulings", 0),
            schedule_type=config.get("schedule_type", "daily"),
        )
    return result


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

RULING_COUNTS_24H_QUERY = """
    SELECT ct.county, COUNT(d.id) AS ruling_count
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
      AND d.created_at >= %s
      {county_filter}
    GROUP BY ct.county
"""

RULING_COUNTS_7D_QUERY = """
    SELECT ct.county,
           COUNT(d.id) AS ruling_count
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
      AND d.created_at >= %s
      AND d.created_at < %s
      {county_filter}
    GROUP BY ct.county
"""

# The 7-day window spans [now-7d, now-24h) = exactly 6 days.
ROLLING_WINDOW_DAYS = 6.0

ALL_ACTIVE_COUNTIES_QUERY = """
    SELECT DISTINCT ct.county
    FROM courts ct
    WHERE ct.is_active = TRUE
    {county_filter}
    ORDER BY ct.county
"""

LATEST_SCRAPER_RUN_QUERY = """
    WITH ranked_runs AS (
        SELECT sr.scraper_id, ct.county, sr.started_at, sr.status,
               ROW_NUMBER() OVER(PARTITION BY sr.scraper_id ORDER BY sr.started_at DESC) AS rn
        FROM scraper_runs sr
        JOIN courts ct ON ct.id = sr.court_id
    )
    SELECT scraper_id, county, started_at, status
    FROM ranked_runs
    WHERE rn = 1
    {county_filter}
"""

LATEST_CAPTURE_PER_COUNTY_QUERY = """
    SELECT ct.county, MAX(d.captured_at) AS last_capture
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
    {county_filter}
    GROUP BY ct.county
"""

# Same field structure as AUDIT_QUERY in audit_field_completeness.py, but
# scoped to recent documents (last N days) for faster, regression-focused checks.
# audit_field_completeness.py scans all documents; this query only checks recent ones.
FIELD_COMPLETENESS_QUERY = """
    SELECT
        ct.county,
        COUNT(d.id) AS total_docs,
        COUNT(r.id) AS has_ruling,
        COUNT(r.judge_id) AS has_judge,
        COUNT(r.motion_type) AS has_motion_type,
        COUNT(r.outcome) AS has_outcome,
        COUNT(CASE WHEN c.case_title IS NOT NULL THEN 1 END) AS has_title,
        COUNT(CASE WHEN c.case_number NOT LIKE 'UNKNOWN-%%' THEN 1 END) AS has_case_number,
        COUNT(CASE WHEN EXISTS (
            SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id
        ) THEN 1 END) AS has_parties,
        COUNT(d.hearing_date) AS has_hearing_date,
        COUNT(c.case_type) AS has_case_type
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
      AND d.created_at >= %s
    {county_filter}
    GROUP BY ct.county ORDER BY ct.county
"""


def load_field_baselines(
    path: Path | None = None,
) -> dict[str, dict[str, float]]:
    """Load per-county field completeness baselines from JSON config file.

    Args:
        path: Path to baselines JSON file. Defaults to repo root.

    Returns:
        Dict mapping county name to dict of field name -> baseline percentage.
    """
    baselines_path = path or DEFAULT_BASELINES_PATH
    if not baselines_path.exists():
        return {}
    with open(baselines_path) as f:
        raw = json.load(f)
    return raw.get("field_completeness", {})


def save_field_baselines(
    current_completeness: dict[str, dict[str, float]],
    path: Path | None = None,
) -> None:
    """Save field completeness baselines, ratcheting up only.

    Updates baselines only if current values are higher than existing ones
    or if no baseline exists for a county/field. Never lowers baselines.

    Args:
        current_completeness: Dict of county -> field -> current percentage.
        path: Path to baselines JSON file. Defaults to repo root.
    """
    baselines_path = path or DEFAULT_BASELINES_PATH
    if baselines_path.exists():
        with open(baselines_path) as f:
            raw = json.load(f)
    else:
        raw = {}

    existing = raw.get("field_completeness", {})

    for county, fields in current_completeness.items():
        if county not in existing:
            existing[county] = {}
        for field, pct in fields.items():
            old_pct = existing[county].get(field, 0.0)
            # Ratchet: only update if current is higher or no baseline exists.
            if pct > old_pct:
                existing[county][field] = round(pct, 1)

    raw["field_completeness"] = existing
    with open(baselines_path, "w") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")

    logger.info("Field completeness baselines updated at %s", baselines_path)


def _query_field_completeness(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    county: str | None = None,
) -> dict[str, dict[str, float]]:
    """Query the database for current field completeness percentages.

    Only considers documents created within the last FIELD_COMPLETENESS_WINDOW_DAYS
    to focus on recent regressions and keep the query fast.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        county: Optional county filter.

    Returns:
        Dict mapping county name to dict of field name -> percentage (0-100).
    """
    county_filter, county_params = _build_county_filter(county)
    cutoff = now - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS)
    result: dict[str, dict[str, float]] = {}

    with conn.cursor() as cur:
        cur.execute(
            FIELD_COMPLETENESS_QUERY.format(county_filter=county_filter),
            (cutoff, *county_params),
        )
        for row in cur.fetchall():
            (
                county_name,
                total,
                has_ruling,
                has_judge,
                has_motion_type,
                has_outcome,
                has_title,
                has_case_number,
                has_parties,
                has_hearing_date,
                has_case_type,
            ) = row

            if total == 0:
                continue

            counts = {
                "ruling": has_ruling,
                "judge": has_judge,
                "motion_type": has_motion_type,
                "outcome": has_outcome,
                "case_title": has_title,
                "case_number": has_case_number,
                "parties": has_parties,
                "hearing_date": has_hearing_date,
                "case_type": has_case_type,
            }

            result[county_name] = {
                field: round(count / total * 100, 1)
                for field, count in counts.items()
            }

    return result


def check_field_completeness(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    field_baselines: dict[str, dict[str, float]],
    county: str | None = None,
) -> list[Alert]:
    """Check field completeness against baselines and flag regressions.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        field_baselines: Per-county, per-field baseline percentages.
        county: Optional county filter.

    Returns:
        List of alerts for field completeness regressions.
    """
    alerts: list[Alert] = []
    current = _query_field_completeness(conn, now, county)

    for county_name, fields in current.items():
        county_baselines = field_baselines.get(county_name, {})
        if not county_baselines:
            continue

        for field, current_pct in fields.items():
            baseline_pct = county_baselines.get(field, 0.0)

            # Ignore fields with 0% baseline (county genuinely lacks the field).
            if baseline_pct == 0.0:
                continue

            drop = baseline_pct - current_pct

            if drop > FIELD_DROP_P1_THRESHOLD:
                severity = "p1"
            elif drop > FIELD_DROP_P2_THRESHOLD:
                severity = "p2"
            else:
                continue

            alerts.append(
                Alert(
                    county=county_name,
                    metric="field_completeness",
                    severity=severity,
                    expected=baseline_pct,
                    actual=current_pct,
                    message=(
                        f"{county_name}: {field} completeness dropped from "
                        f"{baseline_pct:.1f}% to {current_pct:.1f}% "
                        f"({drop:.1f}pp drop)"
                    ),
                )
            )

    return alerts


def _build_county_filter(county: str | None) -> tuple[str, tuple[str, ...]]:
    """Build a SQL WHERE clause fragment for county filtering.

    Args:
        county: County name to filter on, or None for all counties.

    Returns:
        Tuple of (SQL fragment, params tuple).
    """
    if county:
        return "AND ct.county = %s", (county,)
    return "", ()


def check_ingest_rates(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    baselines: dict[str, Baselines],
    county: str | None = None,
) -> list[Alert]:
    """Check ruling ingest rates against 7-day averages.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        baselines: Per-county baseline config.
        county: Optional county filter.

    Returns:
        List of alerts for counties with low ingest rates.
    """
    alerts: list[Alert] = []
    county_filter, county_params = _build_county_filter(county)

    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    # Get 24h counts
    counts_24h: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNTS_24H_QUERY.format(county_filter=county_filter),
            (cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            counts_24h[row[0]] = row[1]

    # Get 7-day counts (excluding last 24h for the average baseline).
    # The window is [now-7d, now-24h) = exactly 6 days.
    avg_daily: dict[str, float] = {}
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNTS_7D_QUERY.format(county_filter=county_filter),
            (cutoff_7d, cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            county_name = row[0]
            total_7d = row[1]
            avg_daily[county_name] = total_7d / ROLLING_WINDOW_DAYS

    # Get all active counties to check for zeros
    all_counties: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            ALL_ACTIVE_COUNTIES_QUERY.format(county_filter=county_filter),
            county_params,
        )
        all_counties = [row[0] for row in cur.fetchall()]

    for county_name in all_counties:
        count_24h = counts_24h.get(county_name, 0)
        daily_avg = avg_daily.get(county_name, 0.0)
        baseline = baselines.get(county_name)
        expected_daily = baseline.expected_daily_rulings if baseline else daily_avg

        # Zero-ruling alert (critical)
        if count_24h == 0 and expected_daily > 0:
            alerts.append(
                Alert(
                    county=county_name,
                    metric="zero_rulings",
                    severity="p1",
                    expected=expected_daily,
                    actual=0,
                    message=(
                        f"{county_name}: zero new rulings in 24h "
                        f"(expected ~{expected_daily:.1f}/day)"
                    ),
                )
            )
        # Ingest rate drop alert
        elif daily_avg > 0 and count_24h < daily_avg * INGEST_DROP_THRESHOLD:
            alerts.append(
                Alert(
                    county=county_name,
                    metric="ingest_rate",
                    severity="p2",
                    expected=round(daily_avg, 1),
                    actual=count_24h,
                    message=(
                        f"{county_name}: {count_24h} rulings in 24h, "
                        f"7-day avg is {daily_avg:.1f}/day (>{INGEST_DROP_THRESHOLD * 100:.0f}% drop)"
                    ),
                )
            )

    return alerts


def check_scraper_staleness(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    baselines: dict[str, Baselines],
    county: str | None = None,
) -> list[Alert]:
    """Check for stale scrapers that haven't produced recent data.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        baselines: Per-county baseline config (for schedule_type).
        county: Optional county filter.

    Returns:
        List of alerts for stale scrapers.
    """
    alerts: list[Alert] = []
    county_filter, county_params = _build_county_filter(county)

    # Try scraper_runs first
    scraper_last_run: dict[str, tuple[str, datetime, str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            LATEST_SCRAPER_RUN_QUERY.format(county_filter=county_filter),
            county_params,
        )
        for row in cur.fetchall():
            scraper_id, county_name, started_at, status = row
            scraper_last_run[county_name] = (scraper_id, started_at, status)

    # Fall back to MAX(captured_at) for counties without scraper_runs
    capture_fallback: dict[str, datetime] = {}
    with conn.cursor() as cur:
        cur.execute(
            LATEST_CAPTURE_PER_COUNTY_QUERY.format(county_filter=county_filter),
            county_params,
        )
        for row in cur.fetchall():
            if row[1] is not None:
                capture_fallback[row[0]] = row[1]

    # Check all counties with baselines
    counties_to_check = set(scraper_last_run.keys()) | set(capture_fallback.keys())
    if county:
        counties_to_check = {c for c in counties_to_check if c == county}

    for county_name in sorted(counties_to_check):
        baseline = baselines.get(county_name)
        schedule_type = baseline.schedule_type if baseline else "daily"
        stale_threshold_hours = (
            FREQUENT_SCRAPER_STALE_HOURS
            if schedule_type == "frequent"
            else DAILY_SCRAPER_STALE_HOURS
        )

        last_activity: datetime | None = None
        source = "unknown"

        if county_name in scraper_last_run:
            _, started_at, _ = scraper_last_run[county_name]
            last_activity = started_at
            source = "scraper_runs"
        elif county_name in capture_fallback:
            last_activity = capture_fallback[county_name]
            source = "documents.captured_at"

        if last_activity is None:
            continue

        hours_since = (now - last_activity).total_seconds() / 3600
        if hours_since > stale_threshold_hours:
            alerts.append(
                Alert(
                    county=county_name,
                    metric="scraper_stale",
                    severity="p1" if hours_since > stale_threshold_hours * 4 else "p2",
                    expected=f"<{stale_threshold_hours}h",
                    actual=f"{hours_since:.1f}h",
                    message=(
                        f"{county_name}: scraper stale for {hours_since:.1f}h "
                        f"(threshold: {stale_threshold_hours}h, source: {source})"
                    ),
                )
            )

    return alerts


def run_checks(
    dsn: str,
    *,
    county: str | None = None,
    baselines_path: Path | None = None,
    now: datetime | None = None,
    update_baselines: bool = False,
) -> list[Alert]:
    """Run all data quality checks.

    Args:
        dsn: Database connection string.
        county: Optional county name filter.
        baselines_path: Path to baselines JSON file.
        now: Override current time (for testing).
        update_baselines: If True, snapshot current field completeness
            as baselines (ratchet up only) and skip alerting.

    Returns:
        List of all alerts found.
    """
    if now is None:
        now = datetime.now(UTC)

    baselines = load_baselines(baselines_path)
    field_baselines = load_field_baselines(baselines_path)
    alerts: list[Alert] = []

    with psycopg.connect(dsn) as conn:
        alerts.extend(check_ingest_rates(conn, now, baselines, county))
        alerts.extend(check_scraper_staleness(conn, now, baselines, county))

        if update_baselines:
            current = _query_field_completeness(conn, now, county)
            save_field_baselines(current, baselines_path)
        else:
            alerts.extend(
                check_field_completeness(conn, now, field_baselines, county)
            )

    return alerts


def format_json(alerts: list[Alert]) -> str:
    """Format alerts as JSON.

    Args:
        alerts: List of Alert objects.

    Returns:
        JSON string.
    """
    return json.dumps(
        {
            "healthy": len(alerts) == 0,
            "alert_count": len(alerts),
            "alerts": [asdict(a) for a in alerts],
        },
        indent=2,
        default=str,
    )


def format_text(alerts: list[Alert]) -> str:
    """Format alerts as human-readable text.

    Args:
        alerts: List of Alert objects.

    Returns:
        Formatted text string.
    """
    if not alerts:
        return "All counties healthy. No alerts."
    lines = [f"Found {len(alerts)} alert(s):", ""]
    for a in alerts:
        severity_marker = "[P1]" if a.severity == "p1" else "[P2]"
        lines.append(f"  {severity_marker} {a.message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub issue filing
# ---------------------------------------------------------------------------

# Map alert metrics to human-readable names for issue titles.
_METRIC_DISPLAY_NAMES: dict[str, str] = {
    "zero_rulings": "zero rulings",
    "ingest_rate": "ingest rate drop",
    "scraper_stale": "scraper stale",
}

# Default GitHub repo for issue filing.
DEFAULT_REPO = "judgemind/judgemind"


def _issue_title(alert: Alert) -> str:
    """Build a dedup-friendly issue title for an alert.

    Format: ``[DQ] <county> — <metric display name>``

    Args:
        alert: The alert to generate a title for.

    Returns:
        Issue title string.
    """
    metric_name = _METRIC_DISPLAY_NAMES.get(alert.metric, alert.metric)
    return f"[DQ] {alert.county} — {metric_name}"


def _issue_body(alert: Alert) -> str:
    """Build a GitHub issue body with diagnostic details.

    Args:
        alert: The alert to describe.

    Returns:
        Markdown-formatted issue body.
    """
    lines = [
        "## Data Quality Alert",
        "",
        f"**County:** {alert.county}",
        f"**Metric:** {alert.metric}",
        f"**Severity:** {alert.severity}",
        f"**Expected:** {alert.expected}",
        f"**Actual:** {alert.actual}",
        "",
        f"> {alert.message}",
        "",
        "## Diagnostic Guidance",
        "",
        f"- Check the scraper logs for {alert.county} county.",
        f"- Review recent scraper runs: "
        f'`scripts/dev-db-query.sh "SELECT * FROM scraper_runs '
        f"WHERE court_id IN (SELECT id FROM courts WHERE county = '{alert.county}') "
        f'ORDER BY started_at DESC LIMIT 5"`',
        f"- Check ruling counts: "
        f'`scripts/dev-db-query.sh "SELECT COUNT(*) FROM documents d '
        f"JOIN courts c ON c.id = d.court_id "
        f"WHERE c.county = '{alert.county}' "
        f"AND d.created_at >= NOW() - INTERVAL '24 hours'\"`",
        "",
        "## Context",
        "",
        "Parent monitoring issue: #739",
        "",
        "_Filed automatically by `scripts/data-quality-check.py --file-issues`._",
    ]
    return "\n".join(lines)


def _issue_labels(alert: Alert) -> list[str]:
    """Determine labels for a GitHub issue based on alert properties.

    Args:
        alert: The alert to label.

    Returns:
        List of label strings.
    """
    labels = ["type/bug", "agent/ready", "area/scraping"]
    if alert.severity == "p1":
        labels.append("priority/p1")
    else:
        labels.append("priority/p2")
    return labels


def _check_duplicate(
    alert: Alert,
    repo: str = DEFAULT_REPO,
) -> bool:
    """Check if an open issue already exists for this alert.

    Searches GitHub for open issues matching the ``[DQ] <county> — <metric>``
    title convention.

    Args:
        alert: The alert to check for duplicates.
        repo: GitHub repository in ``owner/repo`` format.

    Returns:
        True if a duplicate open issue exists, False otherwise.
    """
    title = _issue_title(alert)
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--search",
            title,
            "--state",
            "open",
            "--json",
            "number,title",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("Failed to search for duplicates: %s", result.stderr)
        return False

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("Failed to parse duplicate search results")
        return False

    # Check for exact title match among results.
    return any(issue.get("title") == title for issue in issues)


def _file_single_issue(
    alert: Alert,
    repo: str = DEFAULT_REPO,
) -> int | None:
    """File a single GitHub issue for an alert.

    Writes the issue body to a temp file and invokes ``gh issue create``.

    Args:
        alert: The alert to file an issue for.
        repo: GitHub repository in ``owner/repo`` format.

    Returns:
        The created issue number, or None if filing failed.
    """
    title = _issue_title(alert)
    body = _issue_body(alert)
    labels = _issue_labels(alert)

    # Write body to temp file (gh requires --body-file for multi-line bodies).
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        body_file = f.name

    try:
        cmd = [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body-file",
            body_file,
        ]
        for label in labels:
            cmd.extend(["--label", label])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            logger.error(
                "Failed to create issue for %s/%s: %s",
                alert.county,
                alert.metric,
                result.stderr,
            )
            return None

        # gh issue create prints the URL, e.g. https://github.com/.../issues/123
        url = result.stdout.strip()
        try:
            issue_number = int(url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            logger.warning("Could not parse issue number from: %s", url)
            return None

        logger.info(
            "Filed issue #%d for %s/%s", issue_number, alert.county, alert.metric
        )
        return issue_number

    finally:
        # Clean up temp file.
        try:
            os.unlink(body_file)
        except OSError:
            pass


@dataclass
class FileIssuesResult:
    """Result of the issue filing operation."""

    filed: list[int]
    skipped_duplicate: list[str]
    failed: list[str]


def file_issues_for_alerts(
    alerts: list[Alert],
    repo: str = DEFAULT_REPO,
) -> FileIssuesResult:
    """File GitHub issues for all alerts, with deduplication.

    For each alert, checks whether an open issue already exists with the same
    title convention. If not, creates a new issue with appropriate labels and
    diagnostic details.

    Args:
        alerts: List of alerts to potentially file issues for.
        repo: GitHub repository in ``owner/repo`` format.

    Returns:
        FileIssuesResult with lists of filed issue numbers, skipped duplicates,
        and failed filings.
    """
    result = FileIssuesResult(filed=[], skipped_duplicate=[], failed=[])

    for alert in alerts:
        title = _issue_title(alert)

        if _check_duplicate(alert, repo):
            logger.info("Skipping duplicate: %s", title)
            result.skipped_duplicate.append(title)
            continue

        issue_number = _file_single_issue(alert, repo)
        if issue_number is not None:
            result.filed.append(issue_number)
        else:
            result.failed.append(title)

    return result


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Data quality monitoring — collection health and field completeness checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Machine-readable JSON output (default).",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Human-readable text output.",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Check only this county.",
    )
    parser.add_argument(
        "--baselines",
        type=str,
        default=None,
        help="Path to baselines JSON file.",
    )
    parser.add_argument(
        "--file-issues",
        action="store_true",
        default=False,
        help="Automatically file GitHub issues for detected regressions.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=DEFAULT_REPO,
        help="GitHub repository for issue filing (default: judgemind/judgemind).",
    )
    parser.add_argument(
        "--update-baselines",
        action="store_true",
        help="Snapshot current field completeness as baselines (ratchet up only).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    baselines_path = Path(args.baselines) if args.baselines else None
    alerts = run_checks(
        dsn,
        county=args.county,
        baselines_path=baselines_path,
        update_baselines=args.update_baselines,
    )

    if args.text:
        print(format_text(alerts))
    else:
        print(format_json(alerts))

    if args.file_issues and alerts:
        logger.info("Filing GitHub issues for %d alert(s)...", len(alerts))
        filing_result = file_issues_for_alerts(alerts, repo=args.repo)
        logger.info(
            "Issue filing complete: %d filed, %d duplicates skipped, %d failed",
            len(filing_result.filed),
            len(filing_result.skipped_duplicate),
            len(filing_result.failed),
        )

    # Log a completion marker for CloudWatch metric filters.
    # This allows a CloudWatch alarm to detect when the check stops running.
    logger.info("data_quality_check_complete alert_count=%d", len(alerts))

    sys.exit(0 if len(alerts) == 0 else 1)


if __name__ == "__main__":
    main()
