#!/usr/bin/env python3
# venv: scraper-framework
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
    --store-results     Store check results to S3 for trend analysis.
    --weekly-summary    Generate a markdown weekly summary from stored snapshots.

Exit code: 0 if all healthy, 1 if alerts found.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

# dq_trend_storage is lazily imported only when --store-results or
# --weekly-summary is used.  This avoids a hard ModuleNotFoundError when
# the script runs as an ECS oneshot (only the main script is uploaded).
_dq_trend_storage = None


def _import_trend_storage():  # noqa: ANN202
    """Lazy-import dq_trend_storage on first use."""
    global _dq_trend_storage  # noqa: PLW0603
    if _dq_trend_storage is None:
        import dq_trend_storage as _mod

        _dq_trend_storage = _mod
    return _dq_trend_storage


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
DAILY_SCRAPER_STALE_HOURS = (
    26  # Must exceed the 24h daily schedule interval (+ 2h buffer for runtime/skew)
)
FREQUENT_SCRAPER_STALE_HOURS = 2

# Field completeness regression thresholds (percentage points below baseline).
FIELD_DROP_P1_THRESHOLD = 10.0  # >10pp drop = p1
FIELD_DROP_P2_THRESHOLD = 5.0  # 5-10pp drop = p2

# Window for recent-only field completeness checks (days).
FIELD_COMPLETENESS_WINDOW_DAYS = 7

# Grace period (minutes) — exclude documents created this recently so the
# ingestion pipeline has time to process them before they are evaluated.
FIELD_COMPLETENESS_GRACE_MINUTES = 30

# Minimum number of documents in the window for field completeness checks.
# Counties with fewer than this many documents are skipped to avoid noisy
# alerts from tiny sample sizes (e.g. 1 bad doc out of 3 total = 33% drop).
MIN_FIELD_CHECK_SAMPLE_SIZE = 5


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
    posting_days: list[str] | None = None  # e.g. ["Mon", "Tue", "Wed", "Thu"]
    max_expected_gap_hours: float | None = None  # explicit override


def load_baselines(
    path: Path | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Baselines]:
    """Load per-county baselines from JSON config file or pre-parsed dict.

    Args:
        path: Path to baselines JSON file. Defaults to repo root.
        raw: Pre-parsed baselines dict (takes priority over file path).
            Useful when running as an ECS oneshot where the file is unavailable.

    Returns:
        Dict mapping county name to Baselines.
    """
    if raw is None:
        baselines_path = path or DEFAULT_BASELINES_PATH
        if not baselines_path.exists():
            logger.warning(
                "Baselines file not found at %s, using empty baselines",
                baselines_path,
            )
            return {}
        with open(baselines_path) as f:
            raw = json.load(f)
    result: dict[str, Baselines] = {}
    for county, config in raw.get("counties", {}).items():
        result[county] = Baselines(
            expected_daily_rulings=config.get("expected_daily_rulings", 0),
            schedule_type=config.get("schedule_type", "daily"),
            posting_days=config.get("posting_days"),
            max_expected_gap_hours=config.get("max_expected_gap_hours"),
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
    SELECT ct.county,
           GREATEST(MAX(d.last_seen_at), MAX(d.captured_at)) AS last_capture
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
    {county_filter}
    GROUP BY ct.county
"""

SCRAPER_SUCCESS_RATE_24H_QUERY = """
    SELECT ct.county,
           COUNT(*) AS total_runs,
           COUNT(CASE WHEN sr.status = 'success' THEN 1 END) AS success_count,
           json_agg(
               json_build_object(
                   'status', sr.status,
                   'error_message', sr.error_message
               ) ORDER BY sr.started_at DESC
           ) FILTER (WHERE sr.status != 'success') AS error_details
    FROM scraper_runs sr
    JOIN courts ct ON ct.id = sr.court_id
    WHERE sr.started_at >= %s
    {county_filter}
    GROUP BY ct.county
"""

RULING_COUNT_BY_TYPE_QUERY = """
    SELECT ct.county, d.document_type, COUNT(d.id) AS count
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    WHERE d.status = 'active'
      AND d.created_at >= %s
      {county_filter}
    GROUP BY ct.county, d.document_type
"""

FIELD_GAP_DOCS_QUERY = """
    SELECT ct.county, d.id AS doc_id
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    LEFT JOIN rulings r ON r.document_id = d.id
    LEFT JOIN cases c ON c.id = d.case_id
    WHERE d.status = 'active'
      AND d.created_at >= %s
      AND d.created_at <= %s
      AND (
          r.judge_id IS NULL
          OR r.motion_type IS NULL
          OR r.outcome IS NULL
          OR d.hearing_date IS NULL
          OR NOT EXISTS (SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id)
      )
      {county_filter}
    GROUP BY ct.county, d.id
    ORDER BY ct.county
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
      AND d.created_at <= %s
    {county_filter}
    GROUP BY ct.county ORDER BY ct.county
"""


def load_field_baselines(
    path: Path | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    """Load per-county field completeness baselines from JSON config file or dict.

    Args:
        path: Path to baselines JSON file. Defaults to repo root.
        raw: Pre-parsed baselines dict (takes priority over file path).
            Useful when running as an ECS oneshot where the file is unavailable.

    Returns:
        Dict mapping county name to dict of field name -> baseline percentage.
    """
    if raw is None:
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
) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
    """Query the database for current field completeness percentages.

    Only considers documents created within the last FIELD_COMPLETENESS_WINDOW_DAYS
    and at least FIELD_COMPLETENESS_GRACE_MINUTES ago, to focus on recent
    regressions while excluding documents still being processed by the
    ingestion pipeline.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        county: Optional county filter.

    Returns:
        Tuple of:
        - Dict mapping county name to dict of field name -> percentage (0-100).
        - Dict mapping county name to total document count in the window.
    """
    county_filter, county_params = _build_county_filter(county)
    cutoff = now - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS)
    grace_cutoff = now - timedelta(minutes=FIELD_COMPLETENESS_GRACE_MINUTES)
    result: dict[str, dict[str, float]] = {}
    totals: dict[str, int] = {}

    with conn.cursor() as cur:
        cur.execute(
            FIELD_COMPLETENESS_QUERY.format(county_filter=county_filter),
            (cutoff, grace_cutoff, *county_params),
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

            totals[county_name] = total

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
                field: round(count / total * 100, 1) for field, count in counts.items()
            }

    return result, totals


def check_field_completeness(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    field_baselines: dict[str, dict[str, float]],
    county: str | None = None,
) -> list[Alert]:
    """Check field completeness against baselines and flag regressions.

    Counties with fewer than ``MIN_FIELD_CHECK_SAMPLE_SIZE`` documents in the
    window are skipped to avoid noisy alerts from tiny sample sizes. A P2
    informational alert is emitted instead so the county is not silently ignored.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        field_baselines: Per-county, per-field baseline percentages.
        county: Optional county filter.

    Returns:
        List of alerts for field completeness regressions.
    """
    alerts: list[Alert] = []
    current, totals = _query_field_completeness(conn, now, county)

    for county_name, fields in current.items():
        county_baselines = field_baselines.get(county_name, {})
        if not county_baselines:
            continue

        total_docs = totals.get(county_name, 0)
        if total_docs < MIN_FIELD_CHECK_SAMPLE_SIZE:
            alerts.append(
                Alert(
                    county=county_name,
                    metric="field_completeness_low_sample",
                    severity="p2",
                    expected=MIN_FIELD_CHECK_SAMPLE_SIZE,
                    actual=total_docs,
                    message=(
                        f"{county_name}: only {total_docs} document(s) in "
                        f"{FIELD_COMPLETENESS_WINDOW_DAYS}-day window, skipping "
                        f"field completeness check "
                        f"(minimum sample size: {MIN_FIELD_CHECK_SAMPLE_SIZE})"
                    ),
                )
            )
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


def _24h_overlaps_posting_day(
    now: datetime,
    posting_days: list[str] | None,
) -> bool:
    """Return True if the 24h window ending at *now* overlaps a posting day.

    When *posting_days* is ``None`` or empty, every day is considered a
    posting day and the function returns ``True``.

    The 24h window spans two calendar days: *today* and *yesterday*.  If
    either is in the posting schedule, the court was expected to post
    content that would appear in the window.
    """
    if not posting_days:
        return True

    # _DAY_ABBREVS is ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    posting_weekdays: set[int] = set()
    for day in posting_days:
        try:
            posting_weekdays.add(_DAY_ABBREVS.index(day))
        except ValueError:
            continue

    if not posting_weekdays:
        return True

    today_wd = now.weekday()  # Mon=0 .. Sun=6
    yesterday_wd = (today_wd - 1) % 7
    return today_wd in posting_weekdays or yesterday_wd in posting_weekdays


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

        # Zero-ruling alert (critical).
        # Suppress on non-posting days: if the county has a posting_days
        # schedule and the 24h window doesn't overlap any posting day,
        # zero rulings is expected — not an alert.
        posting_days = baseline.posting_days if baseline else None
        if count_24h == 0 and expected_daily > 0:
            if _24h_overlaps_posting_day(now, posting_days):
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


# Day-of-week abbreviations used in posting_days config.
_DAY_ABBREVS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _calculate_stale_threshold(
    now: datetime,
    schedule_type: str,
    posting_days: list[str] | None,
    max_expected_gap_hours: float | None,
) -> float:
    """Calculate the staleness threshold in hours for a county.

    For counties with ``posting_days`` set, the threshold accounts for
    expected gaps when no new content is posted (e.g. weekends).  The
    threshold is the number of hours from the *end* of the most recent
    posting day to ``now``, plus a buffer equal to the base threshold
    for that schedule type.

    Args:
        now: Current timestamp (must be timezone-aware).
        schedule_type: "daily" or "frequent".
        posting_days: Optional list of day abbreviations (Mon-Sun) when
            the court posts content.
        max_expected_gap_hours: Explicit override.  If set, returned
            directly without further calculation.

    Returns:
        Staleness threshold in hours.
    """
    if max_expected_gap_hours is not None:
        return max_expected_gap_hours

    base_threshold = (
        FREQUENT_SCRAPER_STALE_HOURS
        if schedule_type == "frequent"
        else DAILY_SCRAPER_STALE_HOURS
    )

    if not posting_days:
        return base_threshold

    # Map day abbreviations to weekday integers (Mon=0 .. Sun=6).
    posting_weekdays: set[int] = set()
    for day in posting_days:
        if day in _DAY_ABBREVS:
            posting_weekdays.add(_DAY_ABBREVS.index(day))

    if not posting_weekdays:
        return base_threshold

    # Walk backwards from today to find the most recent posting day.
    # Start from today (if today is a posting day, the scraper should
    # have run today so the gap is just the base threshold).
    current_weekday = now.weekday()  # Mon=0 .. Sun=6
    for days_back in range(8):  # at most 7 days back + today
        check_day = (current_weekday - days_back) % 7
        if check_day in posting_weekdays:
            if days_back == 0:
                # Today is a posting day — use the normal base threshold.
                return base_threshold
            # The last posting day was ``days_back`` days ago.  The
            # expected gap is approximately ``days_back * 24`` hours,
            # plus the base buffer to allow the scraper time to run.
            return days_back * 24.0 + base_threshold

    # Should never reach here (we check 8 days covering a full week),
    # but fall back to a safe large value.
    return 7 * 24.0 + base_threshold  # pragma: no cover


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

    # Fall back to MAX(last_seen_at, captured_at) for counties without scraper_runs.
    # last_seen_at is updated on every document upsert (even when content is
    # unchanged), so it accurately reflects the last time the scraper ran —
    # unlike captured_at which only reflects the first insert.
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
        posting_days = baseline.posting_days if baseline else None
        max_gap_override = baseline.max_expected_gap_hours if baseline else None
        stale_threshold_hours = _calculate_stale_threshold(
            now, schedule_type, posting_days, max_gap_override
        )

        last_activity: datetime | None = None
        source = "unknown"

        if county_name in scraper_last_run:
            _, started_at, _ = scraper_last_run[county_name]
            last_activity = started_at
            source = "scraper_runs"
        elif county_name in capture_fallback:
            last_activity = capture_fallback[county_name]
            source = "documents.last_seen_at"

        if last_activity is None:
            continue

        hours_since = (now - last_activity).total_seconds() / 3600

        # last_seen_at is updated on every document upsert (including dedup'd
        # re-scrapes), so it accurately reflects scraper activity.  No
        # multiplier needed — the normal threshold applies regardless of
        # data source.  See #986.
        effective_threshold = stale_threshold_hours

        if hours_since > effective_threshold:
            alerts.append(
                Alert(
                    county=county_name,
                    metric="scraper_stale",
                    severity="p1" if hours_since > effective_threshold * 4 else "p2",
                    expected=f"<{effective_threshold}h",
                    actual=f"{hours_since:.1f}h",
                    message=(
                        f"{county_name}: scraper stale for {hours_since:.1f}h "
                        f"(threshold: {effective_threshold}h, source: {source})"
                    ),
                )
            )

    return alerts


@dataclass
class CheckResult:
    """Full result from a data quality check run."""

    alerts: list[Alert]
    county_metrics: dict[str, dict[str, Any]]


def _collect_county_metrics(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    county: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect per-county metrics for snapshot storage.

    Gathers ruling counts and field completeness into a dict suitable
    for storing as a snapshot.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        county: Optional county filter.

    Returns:
        Dict mapping county name to metrics dict.
    """
    county_filter, county_params = _build_county_filter(county)
    result: dict[str, dict[str, Any]] = {}

    # Get 24h ruling counts
    cutoff_24h = now - timedelta(hours=24)
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNTS_24H_QUERY.format(county_filter=county_filter),
            (cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            county_name, count = row[0], row[1]
            if county_name not in result:
                result[county_name] = {}
            result[county_name]["ruling_count_24h"] = count

    # Get field completeness
    field_completeness, _totals = _query_field_completeness(conn, now, county)
    for county_name, fields in field_completeness.items():
        if county_name not in result:
            result[county_name] = {}
        result[county_name]["field_completeness"] = fields

    return result


def _collect_full_metrics(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    now: datetime,
    county: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect all per-county metrics for persistence to data_quality_metrics.

    Extends ``_collect_county_metrics`` with 7-day averages, scraper health,
    and metadata suitable for the data_quality_metrics table.

    Args:
        conn: Database connection.
        now: Current timestamp for time calculations.
        county: Optional county filter.

    Returns:
        Dict mapping county name to a flat metrics dict.  Each value dict
        contains metric_name -> {value, metadata} pairs.
    """
    county_filter, county_params = _build_county_filter(county)
    result: dict[str, dict[str, Any]] = {}

    def _ensure(county_name: str) -> dict[str, Any]:
        if county_name not in result:
            result[county_name] = {}
        return result[county_name]

    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    # --- Ruling count 24h ---
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNTS_24H_QUERY.format(county_filter=county_filter),
            (cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            county_name, count = row[0], row[1]
            _ensure(county_name)["ruling_count_24h"] = {
                "value": count,
                "metadata": None,  # type breakdown added below
            }

    # --- Ruling count by type (metadata for ruling_count_24h) ---
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNT_BY_TYPE_QUERY.format(county_filter=county_filter),
            (cutoff_24h, *county_params),
        )
        type_breakdown: dict[str, dict[str, int]] = {}
        for row in cur.fetchall():
            county_name, doc_type, count = row[0], row[1], row[2]
            if county_name not in type_breakdown:
                type_breakdown[county_name] = {}
            type_breakdown[county_name][doc_type or "unknown"] = count

    for county_name, breakdown in type_breakdown.items():
        metrics = _ensure(county_name)
        if "ruling_count_24h" in metrics:
            metrics["ruling_count_24h"]["metadata"] = {
                "by_doc_type": breakdown,
            }

    # --- Ruling count 7d average ---
    with conn.cursor() as cur:
        cur.execute(
            RULING_COUNTS_7D_QUERY.format(county_filter=county_filter),
            (cutoff_7d, cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            county_name = row[0]
            total_7d = row[1]
            avg = round(total_7d / ROLLING_WINDOW_DAYS, 2)
            _ensure(county_name)["ruling_count_7d_avg"] = {
                "value": avg,
                "metadata": {
                    "total_7d_window": total_7d,
                    "window_days": ROLLING_WINDOW_DAYS,
                },
            }

    # --- Field completeness ---
    field_completeness, _fc_totals = _query_field_completeness(conn, now, county)

    # Collect doc IDs with field gaps for metadata.
    gap_docs: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            FIELD_GAP_DOCS_QUERY.format(county_filter=county_filter),
            (
                now - timedelta(days=FIELD_COMPLETENESS_WINDOW_DAYS),
                now - timedelta(minutes=FIELD_COMPLETENESS_GRACE_MINUTES),
                *county_params,
            ),
        )
        for row in cur.fetchall():
            county_name, doc_id = row[0], str(row[1])
            if county_name not in gap_docs:
                gap_docs[county_name] = []
            # Cap at 20 doc IDs per county for metadata size.
            if len(gap_docs[county_name]) < 20:
                gap_docs[county_name].append(doc_id)

    for county_name, fields in field_completeness.items():
        metrics = _ensure(county_name)
        gap_doc_ids = gap_docs.get(county_name, [])
        gap_metadata = {"docs_with_gaps": gap_doc_ids} if gap_doc_ids else None

        # Overall field completeness (average of all individual fields).
        if fields:
            overall_pct = round(sum(fields.values()) / len(fields), 2)
            metrics["field_completeness_pct"] = {
                "value": overall_pct,
                "metadata": gap_metadata,
            }

        # Individual field completeness metrics.
        _field_metric_map = {
            "judge": "field_completeness_judge",
            "motion_type": "field_completeness_motion_type",
            "parties": "field_completeness_parties",
            "outcome": "field_completeness_outcome",
            "hearing_date": "field_completeness_hearing_date",
        }
        for field_key, metric_name in _field_metric_map.items():
            if field_key in fields:
                metrics[metric_name] = {
                    "value": fields[field_key],
                    "metadata": gap_metadata,
                }

    # --- Scraper last success age ---
    with conn.cursor() as cur:
        cur.execute(
            LATEST_SCRAPER_RUN_QUERY.format(county_filter=county_filter),
            county_params,
        )
        for row in cur.fetchall():
            _scraper_id, county_name, started_at, status = row
            if status == "success" and started_at is not None:
                hours_since = round((now - started_at).total_seconds() / 3600, 2)
                _ensure(county_name)["scraper_last_success_age_hours"] = {
                    "value": hours_since,
                    "metadata": None,
                }

    # --- Scraper success rate 24h ---
    with conn.cursor() as cur:
        cur.execute(
            SCRAPER_SUCCESS_RATE_24H_QUERY.format(county_filter=county_filter),
            (cutoff_24h, *county_params),
        )
        for row in cur.fetchall():
            county_name = row[0]
            total_runs = row[1]
            success_count = row[2]
            error_details_json = row[3]
            rate = round(success_count / total_runs * 100, 2) if total_runs > 0 else 0.0

            error_metadata: dict[str, Any] = {
                "total_runs": total_runs,
                "success_count": success_count,
            }
            # Summarize error types for metadata.
            if error_details_json:
                error_types: dict[str, int] = {}
                for err in error_details_json:
                    err_msg = err.get("error_message") or "unknown"
                    # Truncate long error messages for metadata.
                    err_key = err_msg[:100]
                    error_types[err_key] = error_types.get(err_key, 0) + 1
                error_metadata["error_types"] = error_types

            _ensure(county_name)["scraper_run_success_rate_24h"] = {
                "value": rate,
                "metadata": error_metadata,
            }

    return result


def _format_metrics_for_snapshot(
    full_metrics: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Convert ``_collect_full_metrics`` output to the legacy snapshot format.

    The S3 trend-storage layer expects the simpler structure produced by
    the old ``_collect_county_metrics``: ``{county: {ruling_count_24h: int,
    field_completeness: {field: pct, ...}}}``.  This helper adapts the
    richer ``_collect_full_metrics`` output to that format.

    Args:
        full_metrics: Output of ``_collect_full_metrics``.

    Returns:
        Dict in the legacy snapshot format.
    """
    snapshot_data: dict[str, dict[str, Any]] = {}
    for county_name, metrics in full_metrics.items():
        county_data: dict[str, Any] = {}
        if "ruling_count_24h" in metrics:
            county_data["ruling_count_24h"] = metrics["ruling_count_24h"]["value"]

        # Re-construct the field_completeness dict from the individual metrics.
        _field_prefix = "field_completeness_"
        fc_data = {
            k.replace(_field_prefix, ""): v["value"]
            for k, v in metrics.items()
            if k.startswith(_field_prefix) and k != "field_completeness_pct"
        }
        if fc_data:
            county_data["field_completeness"] = fc_data

        if county_data:
            snapshot_data[county_name] = county_data
    return snapshot_data


# ---------------------------------------------------------------------------
# Metrics persistence
# ---------------------------------------------------------------------------

INSERT_METRICS_QUERY = """
    INSERT INTO data_quality_metrics (recorded_at, county, metric_name, metric_value, metadata)
    VALUES (%s, %s, %s, %s, %s)
"""


def persist_metrics(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    county_metrics: dict[str, dict[str, Any]],
    now: datetime | None = None,
) -> int:
    """Write collected metrics to the data_quality_metrics table.

    Uses batched inserts via ``executemany`` to minimize round-trips.
    Failures are logged but do not raise — the check run must not be
    blocked by metrics storage.

    Args:
        conn: Database connection (reused from the check run).
        county_metrics: Dict from ``_collect_full_metrics``.  Each value
            is a dict of metric_name -> {value, metadata}.
        now: Timestamp for the recorded_at column.  Defaults to UTC now.

    Returns:
        Number of metric rows inserted, or 0 on failure.
    """
    if now is None:
        now = datetime.now(UTC)

    rows: list[tuple[datetime, str, str, float, str | None]] = []
    for county_name, metrics in county_metrics.items():
        for metric_name, metric_data in metrics.items():
            value = metric_data["value"]
            metadata = metric_data.get("metadata")
            metadata_json = json.dumps(metadata) if metadata is not None else None
            rows.append((now, county_name, metric_name, float(value), metadata_json))

    if not rows:
        logger.info("No metrics to persist.")
        return 0

    try:
        with conn.cursor() as cur:
            cur.executemany(INSERT_METRICS_QUERY, rows)
        conn.commit()
        logger.info("Persisted %d metric rows to data_quality_metrics.", len(rows))
        return len(rows)
    except Exception:
        logger.exception(
            "Failed to persist %d metrics to data_quality_metrics — "
            "continuing without metrics storage.",
            len(rows),
        )
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def run_checks(
    dsn: str,
    *,
    county: str | None = None,
    baselines_path: Path | None = None,
    baselines_raw: dict[str, Any] | None = None,
    now: datetime | None = None,
    update_baselines: bool = False,
) -> list[Alert]:
    """Run all data quality checks.

    Args:
        dsn: Database connection string.
        county: Optional county name filter.
        baselines_path: Path to baselines JSON file.
        baselines_raw: Pre-parsed baselines dict (takes priority over path).
        now: Override current time (for testing).
        update_baselines: If True, snapshot current field completeness
            as baselines (ratchet up only) and skip alerting.

    Returns:
        List of all alerts found.
    """
    if now is None:
        now = datetime.now(UTC)

    baselines = load_baselines(baselines_path, raw=baselines_raw)
    field_baselines = load_field_baselines(baselines_path, raw=baselines_raw)
    alerts: list[Alert] = []

    with psycopg.connect(dsn) as conn:
        alerts.extend(check_ingest_rates(conn, now, baselines, county))
        alerts.extend(check_scraper_staleness(conn, now, baselines, county))

        if update_baselines:
            current, _totals = _query_field_completeness(conn, now, county)
            save_field_baselines(current, baselines_path)
        else:
            alerts.extend(check_field_completeness(conn, now, field_baselines, county))

    return alerts


def run_checks_full(
    dsn: str,
    *,
    county: str | None = None,
    baselines_path: Path | None = None,
    baselines_raw: dict[str, Any] | None = None,
    now: datetime | None = None,
    update_baselines: bool = False,
    persist: bool = False,
) -> CheckResult:
    """Run all data quality checks and collect county metrics.

    Like ``run_checks`` but also returns the per-county metrics snapshot
    needed for trend storage.  When ``persist=True``, writes all collected
    metrics to the ``data_quality_metrics`` table.

    Args:
        dsn: Database connection string.
        county: Optional county name filter.
        baselines_path: Path to baselines JSON file.
        baselines_raw: Pre-parsed baselines dict (takes priority over path).
        now: Override current time (for testing).
        update_baselines: If True, snapshot current field completeness
            as baselines (ratchet up only) and skip alerting.
        persist: If True, write metrics to the data_quality_metrics table.

    Returns:
        CheckResult with alerts and county metrics.
    """
    if now is None:
        now = datetime.now(UTC)

    baselines = load_baselines(baselines_path, raw=baselines_raw)
    field_baselines = load_field_baselines(baselines_path, raw=baselines_raw)
    alerts: list[Alert] = []

    with psycopg.connect(dsn) as conn:
        alerts.extend(check_ingest_rates(conn, now, baselines, county))
        alerts.extend(check_scraper_staleness(conn, now, baselines, county))

        if update_baselines:
            current, _totals = _query_field_completeness(conn, now, county)
            save_field_baselines(current, baselines_path)
        else:
            alerts.extend(check_field_completeness(conn, now, field_baselines, county))

        # Single metric collection pass — _collect_full_metrics is a
        # superset of _collect_county_metrics.  We derive the legacy
        # snapshot format from the full metrics to avoid duplicate queries.
        full_metrics = _collect_full_metrics(conn, now, county)
        county_metrics = _format_metrics_for_snapshot(full_metrics)

        if persist:
            persist_metrics(conn, full_metrics, now)

    return CheckResult(alerts=alerts, county_metrics=county_metrics)


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
        "--baselines-json",
        type=str,
        default=None,
        help=(
            "Baselines as an inline JSON string. Takes priority over --baselines. "
            "Useful for ECS oneshot where the baselines file is unavailable."
        ),
    )
    parser.add_argument(
        "--baselines-base64",
        type=str,
        default=None,
        help=(
            "Baselines as a base64-encoded JSON string. Takes priority over "
            "--baselines and --baselines-json. Avoids shell quoting issues "
            "when passing JSON through multiple shell layers (e.g. GitHub "
            "Actions -> ecs-run-task.sh -> ECS container)."
        ),
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
    parser.add_argument(
        "--store-results",
        action="store_true",
        default=False,
        help="Store check results to S3 for trend analysis.",
    )
    parser.add_argument(
        "--s3-bucket",
        type=str,
        default="judgemind-assets-dev",
        help="S3 bucket for trend storage (default: judgemind-assets-dev).",
    )
    parser.add_argument(
        "--weekly-summary",
        action="store_true",
        default=False,
        help="Generate a markdown weekly summary from stored snapshots.",
    )
    parser.add_argument(
        "--persist-metrics",
        action="store_true",
        default=False,
        help="Write per-county metrics to the data_quality_metrics table.",
    )
    args = parser.parse_args()

    # Weekly summary mode: load snapshots from S3 and generate report.
    # Does not require a database connection.
    if args.weekly_summary:
        ts = _import_trend_storage()
        snapshots = ts.load_snapshots(bucket=args.s3_bucket)
        print(ts.generate_weekly_summary(snapshots))
        sys.exit(0)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    baselines_path = Path(args.baselines) if args.baselines else None

    # Parse inline baselines if provided (takes priority over file path).
    # This is used when running as an ECS oneshot where the baselines file
    # is not available alongside the script.
    # Priority: --baselines-base64 > --baselines-json > --baselines (file path).
    baselines_raw: dict[str, Any] | None = None
    if args.baselines_base64:
        try:
            decoded = base64.b64decode(args.baselines_base64).decode("utf-8")
            baselines_raw = json.loads(decoded)
        except (ValueError, json.JSONDecodeError) as exc:
            logger.error("Failed to decode --baselines-base64: %s", exc)
            sys.exit(1)
    elif args.baselines_json:
        try:
            baselines_raw = json.loads(args.baselines_json)
        except json.JSONDecodeError:
            logger.error("Failed to parse --baselines-json as JSON")
            sys.exit(1)

    if args.store_results or args.persist_metrics:
        # Use run_checks_full to also collect county metrics for storage.
        check_result = run_checks_full(
            dsn,
            county=args.county,
            baselines_path=baselines_path,
            baselines_raw=baselines_raw,
            update_baselines=args.update_baselines,
            persist=args.persist_metrics,
        )
        alerts = check_result.alerts

        if args.store_results:
            ts = _import_trend_storage()
            now = datetime.now(UTC)

            snapshot = ts.Snapshot(
                timestamp=now.isoformat(),
                county_metrics=check_result.county_metrics,
                alerts=[asdict(a) for a in alerts],
            )
            ts.store_snapshot(snapshot, bucket=args.s3_bucket)

            # Also run trend detection and include trend alerts in output.
            snapshots = ts.load_snapshots(bucket=args.s3_bucket, now=now)
            trend_alerts = ts.detect_trends(snapshots, now=now)
            if trend_alerts:
                for ta in trend_alerts:
                    alerts.append(
                        Alert(
                            county=ta.county,
                            metric=ta.metric,
                            severity=ta.severity,
                            expected=ta.prior_avg,
                            actual=ta.current_avg,
                            message=ta.message,
                        )
                    )
    else:
        alerts = run_checks(
            dsn,
            county=args.county,
            baselines_path=baselines_path,
            baselines_raw=baselines_raw,
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
