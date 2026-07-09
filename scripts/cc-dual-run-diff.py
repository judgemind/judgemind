#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""CC Dual-Run Diff — compare Contra Costa scraper coverage by department.

Queries ``derived.documents`` (joined to ``derived.cases`` for case_number)
for both ``ca-cc-tentatives`` (retired) and ``ca-cc-tentatives-portal`` for a
given date.  For each scraper, derives the department:

  * Retired scraper (``ca-cc-tentatives``): uses the ``department`` column in
    ``derived.rulings`` (populated by the retired scraper's parsing logic).
  * Portal scraper (``ca-cc-tentatives-portal``): derives department from the
    PDF filename in the ``s3_key`` column using the ``NN_MMDDYY.pdf`` pattern
    (e.g. ``/system/files/general/16_012925.pdf`` → dept ``"16"``).

The diff computes per-department coverage gaps:

  * ``missing_portal``      — department captured by retired scraper but absent
                              from portal (potential coverage regression).
  * ``new_portal_coverage`` — department captured by portal but absent from
                              retired scraper (portal ahead).
  * ``mismatch``            — both present, different case sets.
  * ``equal``               — both present, same case set.

One row is written to ``telemetry.data_quality_metrics`` with
``metric_name='cc_dual_run_diff'``, ``county='contra_costa'``, and the full
diff in ``metadata`` JSONB.

Exits 1 if any ``flag='missing_portal'`` row exists (coverage regression).
Exits 0 if healthy.
Exits 2 if DATABASE_URL is not set.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
        scripts/cc-dual-run-diff.py --date 2026-04-15

Options:
    --date YYYY-MM-DD   Hearing date to compare (required).
    --json              Machine-readable JSON output (default).
    --text              Human-readable text output.

Note:
    The dev database is in a private VPC. Run via
    ``scripts/ecs-run-task.sh`` or the GH Actions workflow
    ``cc-dual-run-diff.yml``, not locally.

See: https://github.com/judgemind/judgemind/issues/2610
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg

# ---------------------------------------------------------------------------
# sys.path setup — import the framework package from the venv.
# When running as an ECS oneshot the script is uploaded to /tmp; the
# package lives at /app/packages/scraper-framework/src.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_SF_SRC = _REPO_ROOT / "packages" / "scraper-framework" / "src"
if _SF_SRC.is_dir() and str(_SF_SRC) not in sys.path:
    sys.path.insert(0, str(_SF_SRC))

from courts.ca.cc_tentatives_portal import _cc_dept_from_filename  # noqa: E402
from framework.dual_run_diff import (  # noqa: E402
    Capture,
    compute_per_department_diff,
    compute_total_coverage_signal,
    format_summary,
)

# Use the same structlog configuration as scripts/drain_splitter_carry_forward_clusters.py
# so ``extra=`` fields passed to ``logger.info(...)`` calls surface in CloudWatch Logs
# Insights output. ``stdlib_bridge=True`` routes stdlib ``logging.getLogger(__name__)``
# calls through structlog's ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord
# plus its extras as one event per line. The previous ``logging.basicConfig`` format
# string silently dropped every ``extra=`` field — see #4368/#4373/#4400.
from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scraper IDs
# ---------------------------------------------------------------------------

SCRAPER_RETIRED = "ca-cc-tentatives"
SCRAPER_PORTAL = "ca-cc-tentatives-portal"

# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

# Fetch all documents for both CC scrapers on a given hearing_date.
# Returns: (scraper_id, s3_key, case_number, hearing_date, department)
# - department comes from derived.rulings (nullable — populated for retired
#   scraper; may be null for portal scraper whose dept is derived from s3_key).
FETCH_CAPTURES_QUERY = """
    SELECT
        d.scraper_id,
        d.s3_key,
        c.case_number,
        d.hearing_date,
        r.department
    FROM derived.documents d
    JOIN derived.cases c ON c.id = d.case_id
    LEFT JOIN derived.rulings r ON r.document_id = d.id
    WHERE d.scraper_id IN (%s, %s)
      AND d.hearing_date = %s
      AND d.status = 'active'
"""

INSERT_METRICS_QUERY = """
    INSERT INTO telemetry.data_quality_metrics
        (recorded_at, county, metric_name, metric_value, metadata)
    VALUES (%s, %s, %s, %s, %s)
"""


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def _fetch_captures(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    target_date: date,
) -> list[Capture]:
    """Fetch captures for both CC scrapers on ``target_date``.

    For each document row, derives department as follows:

    * Retired scraper: uses the ``department`` column from ``derived.rulings``
      (populated by the retired scraper's parsing logic).
    * Portal scraper: derives dept from the PDF filename in ``s3_key`` via
      ``_cc_dept_from_filename(s3_key)``.
    * Rows where department cannot be determined are logged and skipped.
    """
    captures: list[Capture] = []
    skipped = 0

    with conn.cursor() as cur:
        cur.execute(
            FETCH_CAPTURES_QUERY,
            (SCRAPER_RETIRED, SCRAPER_PORTAL, target_date),
        )
        rows = cur.fetchall()

    for scraper_id, s3_key, case_number, hearing_date, dept_from_db in rows:
        # Determine department
        if scraper_id == SCRAPER_PORTAL:
            department = _cc_dept_from_filename(s3_key)
        else:
            # Retired scraper — use rulings.department
            department = dept_from_db

        if not department:
            logger.warning(
                "Could not derive department for %s case=%s s3_key=%s — skipping",
                scraper_id,
                case_number,
                s3_key,
            )
            skipped += 1
            continue

        # hearing_date may come back as a datetime.date or datetime.datetime
        if isinstance(hearing_date, datetime):
            hearing_date = hearing_date.date()

        captures.append(
            Capture(
                scraper_id=scraper_id,
                department=str(department),
                case_number=case_number,
                hearing_date=hearing_date,
            )
        )

    if skipped:
        logger.warning("Skipped %d row(s) with undetermined department.", skipped)

    logger.info(
        "Fetched %d captures for %s (retired=%d, portal=%d, skipped=%d).",
        len(captures),
        target_date,
        sum(1 for c in captures if c.scraper_id == SCRAPER_RETIRED),
        sum(1 for c in captures if c.scraper_id == SCRAPER_PORTAL),
        skipped,
    )
    return captures


# ---------------------------------------------------------------------------
# Metrics write
# ---------------------------------------------------------------------------


def _write_metrics_row(
    conn: psycopg.Connection,  # type: ignore[type-arg]
    target_date: date,
    diff_payload: list[dict[str, Any]],
    missing_portal_count: int,
    now: datetime,
    portal_total_zero: bool,
    retired_total: int,
    portal_total: int,
    total_coverage_headline: str,
    agent_actionable: bool,
) -> None:
    """Insert one row into ``telemetry.data_quality_metrics``."""
    metadata_obj: dict[str, Any] = {
        "date": target_date.isoformat(),
        "diff": diff_payload,
        "missing_portal_count": missing_portal_count,
        "portal_total_zero": portal_total_zero,
        "retired_total": retired_total,
        "portal_total": portal_total,
        "total_coverage_headline": total_coverage_headline,
        "agent_actionable": agent_actionable,
    }
    metadata_json = json.dumps(metadata_obj)

    try:
        with conn.cursor() as cur:
            cur.execute(
                INSERT_METRICS_QUERY,
                (
                    now,
                    "contra_costa",
                    "cc_dual_run_diff",
                    float(missing_portal_count),
                    metadata_json,
                ),
            )
        conn.commit()
        logger.info(
            "Wrote cc_dual_run_diff metrics row to telemetry.data_quality_metrics."
        )
    except Exception:
        logger.exception("Failed to write cc_dual_run_diff metrics row — continuing.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date format '{value}'. Expected YYYY-MM-DD."
        ) from exc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare CC tentative-ruling coverage between retired and portal scrapers."
        ),
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        required=True,
        metavar="YYYY-MM-DD",
        help="Hearing date to compare (required).",
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument(
        "--json",
        action="store_false",
        dest="text",
        help="Machine-readable JSON output (default).",
    )
    out.add_argument(
        "--text",
        action="store_true",
        dest="text",
        help="Human-readable text output.",
    )
    parser.set_defaults(text=False)
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

    now = datetime.now(UTC)
    target_date: date = args.date

    with psycopg.connect(database_url, autocommit=False) as conn:
        captures = _fetch_captures(conn, target_date)

        captures_a = [c for c in captures if c.scraper_id == SCRAPER_RETIRED]
        captures_b = [c for c in captures if c.scraper_id == SCRAPER_PORTAL]

        diff_rows = compute_per_department_diff(captures_a, captures_b, target_date)

        missing_portal_count = sum(1 for r in diff_rows if r.flag == "missing_portal")

        # Distinguish a total-coverage failure (portal captured nothing) from
        # per-department gaps — see issue #4600.
        signal = compute_total_coverage_signal(diff_rows)

        # Whether this breach is actionable by an autonomous agent (#4631).
        #
        # A *per-department gap* (portal is live and capturing current rulings
        # but misses one or more departments) is a genuine scraper regression
        # an agent can fix — it stays agent-actionable.
        #
        # A *total-coverage failure* (``portal_total_zero`` — portal captured
        # nothing at all while the retired scraper captured something) is NOT
        # a per-department regression. During the Phase 2 dual-run, this is the
        # expected "portal not yet the authoritative publication channel"
        # signal: the Contra Costa court still publishes current rulings only
        # on the retired site (the public /online-services/tentative-rulings
        # page still iframes retired.cc-courts.org), so the portal's Views
        # endpoint exposes only stale seed data and there is nothing current
        # for the portal scraper to capture. No agent code change can conjure
        # data the upstream portal does not serve, so filing it as an
        # ``agent/ready`` p1 bug just churns the work queue every day. It is
        # surfaced to humans as a low-priority tracker instead (the workflow
        # keys issue labels/body on this flag).
        agent_actionable = missing_portal_count > 0 and not signal.portal_total_zero

        # Serialize diff rows for JSON output and metrics storage
        diff_payload: list[dict[str, Any]] = [
            {
                "department": r.department,
                "count_a": r.count_a,
                "count_b": r.count_b,
                "cases_only_a": r.cases_only_a,
                "cases_only_b": r.cases_only_b,
                "flag": r.flag,
            }
            for r in diff_rows
        ]

        _write_metrics_row(
            conn,
            target_date,
            diff_payload,
            missing_portal_count,
            now,
            portal_total_zero=signal.portal_total_zero,
            retired_total=signal.retired_total,
            portal_total=signal.portal_total,
            total_coverage_headline=signal.headline,
            agent_actionable=agent_actionable,
        )

    healthy = missing_portal_count == 0

    if args.text:
        print(format_summary(diff_rows))
    else:
        payload: dict[str, Any] = {
            "agent_actionable": agent_actionable,
            "date": target_date.isoformat(),
            "diff": diff_payload,
            "healthy": healthy,
            "missing_portal_count": missing_portal_count,
            "portal_total": signal.portal_total,
            "portal_total_zero": signal.portal_total_zero,
            "retired_total": signal.retired_total,
            "total_coverage_headline": signal.headline,
            "total_departments": len(diff_rows),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))

    if healthy:
        logger.info(
            "CC dual-run diff: healthy — no missing_portal departments on %s.",
            target_date,
        )
    else:
        logger.warning(
            "CC dual-run diff: %d department(s) missing from portal on %s.",
            missing_portal_count,
            target_date,
        )

    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
