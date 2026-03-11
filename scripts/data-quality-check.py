#!/usr/bin/env python3
"""Data quality monitoring — collection health checks.

Queries the database and flags counties with unhealthy ruling ingest
rates, stale scrapers, or zero new rulings.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/data-quality-check.py

Options:
    --json          Machine-readable JSON output (default).
    --text          Human-readable text output.
    --county NAME   Check only the specified county.

Exit code: 0 if all healthy, 1 if alerts found.
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

import argparse
import json
import logging
import os
import sys
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


@dataclass
class Alert:
    """A single data quality alert."""

    county: str
    metric: str  # ingest_rate, scraper_stale, zero_rulings
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
) -> list[Alert]:
    """Run all data quality checks.

    Args:
        dsn: Database connection string.
        county: Optional county name filter.
        baselines_path: Path to baselines JSON file.
        now: Override current time (for testing).

    Returns:
        List of all alerts found.
    """
    if now is None:
        now = datetime.now(UTC)

    baselines = load_baselines(baselines_path)
    alerts: list[Alert] = []

    with psycopg.connect(dsn) as conn:
        alerts.extend(check_ingest_rates(conn, now, baselines, county))
        alerts.extend(check_scraper_staleness(conn, now, baselines, county))

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


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Data quality monitoring — collection health checks.",
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
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    baselines_path = Path(args.baselines) if args.baselines else None
    alerts = run_checks(dsn, county=args.county, baselines_path=baselines_path)

    if args.text:
        print(format_text(alerts))
    else:
        print(format_json(alerts))

    sys.exit(0 if len(alerts) == 0 else 1)


if __name__ == "__main__":
    main()
