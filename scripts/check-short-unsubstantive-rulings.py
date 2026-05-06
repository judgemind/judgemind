#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Short-unsubstantive ruling check — detects counties with many rulings
that have a very short ruling_text and no extracted motion_type or outcome.

A ruling with ``char_length(ruling_text) < 200`` AND ``motion_type IS NULL``
AND ``outcome IS NULL`` is a candidate for a "short unsubstantive" ruling
— content that slipped past the extraction pipeline without useful fields.
This check counts those rulings per county (joined via ``derived.courts``)
over the last 7 days (by ``hearing_date``) and fires an alert when any
county exceeds the configured threshold.

See https://github.com/judgemind/judgemind/issues/2671 for context.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/check-short-unsubstantive-rulings.py

Options:
    --json              Machine-readable JSON output (default).
    --threshold N       Count above which a county is in breach (default: 5).
    --persist-metrics   Write per-county counts to data_quality_metrics table.
    --county NAME       Limit the check to a specific county.

Exit code: 0 if no breaches, 1 if one or more counties are in breach.

Note:
    The dev database is in a private VPC. Run via
    ``scripts/ecs-run-task.sh`` or the GH Actions workflow
    ``short-unsubstantive-ruling-check.yml``, not locally.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLD = 5
METRIC_NAME = "short_unsubstantive_ruling_count"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CountyCount:
    """Per-county count of short unsubstantive rulings in the last 7 days."""

    county: str
    count: int


@dataclass
class Breach:
    """A county whose count exceeds the configured threshold."""

    county: str
    count: int
    threshold: int
    message: str


@dataclass
class CheckResult:
    """Full result of a short-unsubstantive-ruling check."""

    breaches: list[Breach]
    county_counts: list[CountyCount]
    checked_count: int


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

# Columns referenced:
#   - `county` lives on `derived.courts`, joined via `r.court_id`.
#   - `motion_type` is the canonical column on `derived.rulings`
#     (not `motion`).
#   - `hearing_date` is the canonical "issued at" column on
#     `derived.rulings` (NOT NULL); `ruling_date` does not exist.
# Fixed in #4278 — the prior query referenced `county`, `motion`, and
# `ruling_date`, all of which do not exist on `derived.rulings`. The
# unqualified-column-reference check (#4271) flagged this; the
# `# sql-check:ignore` marker has been removed now that the columns are
# correct.
SHORT_UNSUBSTANTIVE_QUERY = """
    SELECT ct.county, COUNT(*) AS cnt
    FROM derived.rulings r
    JOIN derived.courts ct ON ct.id = r.court_id
    WHERE char_length(r.ruling_text) < 200
      AND r.motion_type IS NULL
      AND r.outcome IS NULL
      AND r.hearing_date > (NOW() - INTERVAL '7 days')::date
    {county_filter}
    GROUP BY ct.county
    ORDER BY ct.county
"""


def _fetch_county_counts(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    county: str | None,
) -> list[CountyCount]:
    """Return per-county counts of short unsubstantive rulings."""
    params: list[Any] = []
    county_filter = ""
    if county:
        county_filter = "AND ct.county = %s"
        params.append(county)

    rows: list[CountyCount] = []
    with conn.cursor() as cur:
        cur.execute(
            SHORT_UNSUBSTANTIVE_QUERY.format(county_filter=county_filter),
            tuple(params),
        )
        for row in cur.fetchall():
            rows.append(CountyCount(county=row[0], count=int(row[1])))
    return rows


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def check_short_unsubstantive_rulings(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    *,
    threshold: int = DEFAULT_THRESHOLD,
    county: str | None = None,
) -> CheckResult:
    """Run the short-unsubstantive-ruling check.

    Args:
        conn: Database connection (must see ``derived.rulings`` and,
            if persist is requested, ``data_quality_metrics`` via the
            default search_path).
        threshold: Count above which a county is considered in breach.
        county: Optional single county to check.

    Returns:
        ``CheckResult`` with breaches and per-county counts.
    """
    county_counts = _fetch_county_counts(conn, county)

    breaches: list[Breach] = []
    for cc in county_counts:
        if cc.count > threshold:
            breaches.append(
                Breach(
                    county=cc.county,
                    count=cc.count,
                    threshold=threshold,
                    message=(
                        f"{cc.county}: {cc.count} short unsubstantive ruling(s) "
                        f"in last 7 days (threshold: {threshold})"
                    ),
                )
            )

    return CheckResult(
        breaches=breaches,
        county_counts=county_counts,
        checked_count=len(county_counts),
    )


# ---------------------------------------------------------------------------
# Metrics persistence
# ---------------------------------------------------------------------------

INSERT_METRIC_QUERY = """
    INSERT INTO data_quality_metrics (recorded_at, county, metric_name, metric_value, metadata)
    VALUES (%s, %s, %s, %s, %s)
"""


def persist_metrics(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    result: CheckResult,
    now: datetime | None = None,
) -> int:
    """Write per-county counts to data_quality_metrics.

    One row per county with a non-zero count. Failures are logged but do
    not abort the run — metric persistence must never block the check.

    Returns:
        Number of rows inserted, or 0 on failure.
    """
    if now is None:
        now = datetime.now(UTC)

    rows = [
        (now, cc.county, METRIC_NAME, float(cc.count), None)
        for cc in result.county_counts
        if cc.count > 0
    ]

    if not rows:
        logger.info("No non-zero county counts to persist.")
        return 0

    try:
        with conn.cursor() as cur:
            cur.executemany(INSERT_METRIC_QUERY, rows)
        conn.commit()
        logger.info("Persisted %d metric rows to data_quality_metrics.", len(rows))
        return len(rows)
    except Exception:
        logger.exception(
            "Failed to persist %d metrics to data_quality_metrics — "
            "continuing without metrics storage.",
            len(rows),
        )
        return 0


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_json(result: CheckResult) -> str:
    """Machine-readable JSON summary."""
    payload = {
        "breaches": [asdict(b) for b in result.breaches],
        "county_counts": [asdict(cc) for cc in result.county_counts],
        "checked_count": result.checked_count,
        "healthy": len(result.breaches) == 0,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect counties with many short-and-unsubstantive rulings "
            "in the last 7 days."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Machine-readable JSON output (default).",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_THRESHOLD,
        help=f"Breach threshold per county (default: {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument(
        "--persist-metrics",
        action="store_true",
        help="Write per-county counts to data_quality_metrics table.",
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Limit the check to a specific county.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error(
            "DATABASE_URL is not set. Run via scripts/with-secret.sh or "
            "scripts/ecs-run-task.sh so the connection string is injected."
        )
        return 2

    with psycopg.connect(database_url, autocommit=False) as conn:
        result = check_short_unsubstantive_rulings(
            conn,
            threshold=args.threshold,
            county=args.county,
        )

        if args.persist_metrics:
            persist_metrics(conn, result)

    print(format_json(result))

    return 0 if not result.breaches else 1


if __name__ == "__main__":
    raise SystemExit(main())
