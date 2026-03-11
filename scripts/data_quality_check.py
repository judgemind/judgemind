#!/usr/bin/env python3
"""Data quality check — collection health monitoring for Judgemind scrapers.

Queries the database and reports on:
  - Ruling ingest rate per county (last 24h vs 7-day average)
  - Scraper staleness (time since last successful run)
  - Zero-ruling alerts (counties with no new data)

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 scripts/data_quality_check.py

Options:
    --json          Machine-readable JSON output (default).
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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

_BASELINES_PATH = Path(__file__).resolve().parent.parent / "data-quality-baselines.json"


def load_baselines(path: Path | None = None) -> dict[str, Any]:
    """Load the data-quality-baselines.json file."""
    p = path or _BASELINES_PATH
    if not p.exists():
        logger.warning("Baselines file not found at %s — using empty baselines", p)
        return {"scrapers": {}}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    """A single data quality alert."""

    county: str
    scraper_id: str
    metric: str  # "zero_rulings", "ingest_rate_drop", "scraper_stale"
    severity: str  # "p1", "p2"
    expected: float
    actual: float
    message: str


@dataclass
class CountyStats:
    """Per-county collection statistics."""

    last_24h: int
    seven_day_avg: float


@dataclass
class CheckResult:
    """Full result of a data quality check run."""

    timestamp: str
    alerts: list[Alert] = field(default_factory=list)
    county_stats: dict[str, CountyStats] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

# Count documents per county in the last N days, grouped by day.
# Uses captured_at (indexed) and filters to active documents.
DAILY_COUNTS_QUERY = """
    SELECT
        ct.county,
        d.captured_at::date AS capture_date,
        COUNT(d.id) AS doc_count
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
      AND d.captured_at >= %s
      {county_filter}
    GROUP BY ct.county, d.captured_at::date
    ORDER BY ct.county, capture_date
"""

# Get the latest successful scraper run per scraper_id.
LATEST_SCRAPER_RUNS_QUERY = """
    SELECT
        sr.scraper_id,
        ct.county,
        MAX(sr.completed_at) AS last_success
    FROM scraper_runs sr
    LEFT JOIN courts ct ON ct.id = sr.court_id
    WHERE sr.status = 'success'
    GROUP BY sr.scraper_id, ct.county
"""

# Fallback: get the latest document capture per scraper_id.
LATEST_CAPTURE_QUERY = """
    SELECT
        d.scraper_id,
        ct.county,
        MAX(d.captured_at) AS last_capture
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
      AND d.scraper_id IS NOT NULL
    GROUP BY d.scraper_id, ct.county
"""


# ---------------------------------------------------------------------------
# Check logic
# ---------------------------------------------------------------------------


def _compute_county_stats(
    daily_counts: list[tuple[str, Any, int]],
    now: datetime,
) -> dict[str, CountyStats]:
    """Build per-county stats from daily count rows.

    ``last_24h`` is today's document count (partial day).
    ``seven_day_avg`` is the average daily count over the previous 7 full days
    (excluding today), so it represents a stable baseline for comparison.

    Args:
        daily_counts: List of (county, capture_date, doc_count) tuples.
        now: Current timestamp for determining "today".

    Returns:
        Dict mapping county name to CountyStats.
    """
    # Aggregate by county and date
    county_days: dict[str, dict[str, int]] = {}
    for county, capture_date, doc_count in daily_counts:
        date_str = str(capture_date)
        county_days.setdefault(county, {})[date_str] = doc_count

    today_str = str(now.date())

    # The 7-day window covers the 7 days before today
    prev_7_days = {str((now - timedelta(days=i)).date()) for i in range(1, 8)}

    stats: dict[str, CountyStats] = {}
    for county, days in county_days.items():
        # last_24h: today's count only
        last_24h = days.get(today_str, 0)

        # 7-day average: sum of previous 7 days / 7
        total_prev_7 = sum(days.get(d, 0) for d in prev_7_days)
        seven_day_avg = round(total_prev_7 / 7, 1)

        stats[county] = CountyStats(last_24h=last_24h, seven_day_avg=seven_day_avg)

    return stats


def check_ingest_rates(
    county_stats: dict[str, CountyStats],
    baselines: dict[str, Any],
) -> list[Alert]:
    """Check for ingest rate drops and zero-ruling alerts.

    Args:
        county_stats: Per-county statistics from _compute_county_stats.
        baselines: Loaded baselines config.

    Returns:
        List of alerts for rate drops and zero-ruling conditions.
    """
    alerts: list[Alert] = []
    scrapers = baselines.get("scrapers", {})

    # Build county -> list of scraper_ids mapping from baselines
    county_to_scrapers: dict[str, list[str]] = {}
    for scraper_id, config in scrapers.items():
        county = config.get("county", "")
        if county:
            county_to_scrapers.setdefault(county, []).append(scraper_id)

    for county, stats in county_stats.items():
        scraper_ids = county_to_scrapers.get(county, ["unknown"])
        scraper_id = ",".join(scraper_ids)

        # Zero-ruling alert (p1): county historically has data but got 0 in 24h
        if stats.last_24h == 0 and stats.seven_day_avg > 0:
            alerts.append(
                Alert(
                    county=county,
                    scraper_id=scraper_id,
                    metric="zero_rulings",
                    severity="p1",
                    expected=stats.seven_day_avg,
                    actual=0,
                    message=(
                        f"Zero rulings in last 24h for {county} "
                        f"(7-day avg: {stats.seven_day_avg})"
                    ),
                )
            )
        # Ingest rate drop (p2): >50% drop from 7-day average
        elif stats.seven_day_avg > 0 and stats.last_24h < stats.seven_day_avg * 0.5:
            alerts.append(
                Alert(
                    county=county,
                    scraper_id=scraper_id,
                    metric="ingest_rate_drop",
                    severity="p2",
                    expected=stats.seven_day_avg,
                    actual=float(stats.last_24h),
                    message=(
                        f"Ruling count dropped >50% for {county}: "
                        f"{stats.last_24h} in last 24h vs {stats.seven_day_avg} avg"
                    ),
                )
            )

    return alerts


def check_scraper_staleness(
    scraper_last_seen: dict[str, datetime],
    baselines: dict[str, Any],
    now: datetime,
) -> list[Alert]:
    """Check for stale scrapers that haven't produced data recently.

    Args:
        scraper_last_seen: Dict mapping scraper_id to its last success/capture time.
        baselines: Loaded baselines config.
        now: Current timestamp.

    Returns:
        List of alerts for stale scrapers.
    """
    alerts: list[Alert] = []
    scrapers = baselines.get("scrapers", {})

    for scraper_id, config in scrapers.items():
        threshold_hours = config.get("stale_threshold_hours", 26)
        county = config.get("county", scraper_id)

        last_seen = scraper_last_seen.get(scraper_id)
        if last_seen is None:
            # Scraper has never run — p1 alert
            alerts.append(
                Alert(
                    county=county,
                    scraper_id=scraper_id,
                    metric="scraper_stale",
                    severity="p1",
                    expected=threshold_hours,
                    actual=-1,
                    message=f"Scraper {scraper_id} has never run successfully",
                )
            )
            continue

        hours_since = (now - last_seen).total_seconds() / 3600
        if hours_since > threshold_hours:
            severity = "p1" if hours_since > threshold_hours * 2 else "p2"
            alerts.append(
                Alert(
                    county=county,
                    scraper_id=scraper_id,
                    metric="scraper_stale",
                    severity=severity,
                    expected=threshold_hours,
                    actual=round(hours_since, 1),
                    message=(
                        f"Scraper {scraper_id} last succeeded "
                        f"{round(hours_since, 1)}h ago (threshold: {threshold_hours}h)"
                    ),
                )
            )

    return alerts


# ---------------------------------------------------------------------------
# Main check runner
# ---------------------------------------------------------------------------


def run_check(
    dsn: str,
    *,
    county: str | None = None,
    baselines_path: Path | None = None,
    now: datetime | None = None,
) -> CheckResult:
    """Run all data quality checks against the database.

    Args:
        dsn: PostgreSQL connection string.
        county: Optional county filter.
        baselines_path: Path to baselines JSON file.
        now: Override for current time (for testing).

    Returns:
        CheckResult with all alerts and stats.
    """
    if now is None:
        now = datetime.now(UTC)

    baselines = load_baselines(baselines_path)

    # Query window: 8 days back to compute 7-day average + today
    lookback = now - timedelta(days=8)

    county_filter = ""
    params: list[Any] = [lookback]
    if county:
        county_filter = "AND ct.county = %s"
        params.append(county)

    with psycopg.connect(dsn) as conn:
        # 1. Get daily document counts
        with conn.cursor() as cur:
            cur.execute(
                DAILY_COUNTS_QUERY.format(county_filter=county_filter),
                params,
            )
            daily_rows = cur.fetchall()

        # 2. Get latest scraper runs
        with conn.cursor() as cur:
            cur.execute(LATEST_SCRAPER_RUNS_QUERY)
            run_rows = cur.fetchall()

        # 3. Get latest captures (fallback)
        with conn.cursor() as cur:
            cur.execute(LATEST_CAPTURE_QUERY)
            capture_rows = cur.fetchall()

    # Build scraper last-seen map (prefer scraper_runs, fall back to captures)
    scraper_last_seen: dict[str, datetime] = {}
    for scraper_id, _county, last_capture in capture_rows:
        if scraper_id and last_capture:
            scraper_last_seen[scraper_id] = last_capture
    for scraper_id, _county, last_success in run_rows:
        if scraper_id and last_success:
            # Scraper runs take precedence over capture timestamps
            scraper_last_seen[scraper_id] = last_success

    # Compute stats and run checks
    county_stats = _compute_county_stats(daily_rows, now)
    alerts: list[Alert] = []
    alerts.extend(check_ingest_rates(county_stats, baselines))
    alerts.extend(check_scraper_staleness(scraper_last_seen, baselines, now))

    return CheckResult(
        timestamp=now.isoformat(),
        alerts=alerts,
        county_stats=county_stats,
    )


def result_to_dict(result: CheckResult) -> dict[str, Any]:
    """Convert a CheckResult to a JSON-serializable dict."""
    return {
        "timestamp": result.timestamp,
        "alerts": [asdict(a) for a in result.alerts],
        "county_stats": {k: asdict(v) for k, v in result.county_stats.items()},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint for data quality checks."""
    parser = argparse.ArgumentParser(
        description="Data quality check — scraper health monitoring.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Machine-readable JSON output (default).",
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
    result = run_check(dsn, county=args.county, baselines_path=baselines_path)
    output = result_to_dict(result)
    print(json.dumps(output, indent=2))

    sys.exit(1 if result.alerts else 0)


if __name__ == "__main__":
    main()
