#!/usr/bin/env python3
"""Clean up San Diego rulings corrupted by the calendar-HTML LLM path (#2447).

Two bad data patterns appeared in the San Diego subset of the dev database
after ``scripts/rebuild_db.py`` ran with the synthetic ``rebuild-ca-san_diego``
scraper_id:

* **Bug A — swapped identifiers.** The LLM saw a 60-100KB calendar HTML page
  but its prompt assumed a single case.  It extracted one case's
  ``ruling_text`` while the parent document row kept another case's
  ``case_number`` / ``case_title``.  Result: DB rows where the identifiers
  and the ruling body refer to different cases.

* **Bug B — 50000-char raw-HTML dumps.** Where the LLM produced an empty
  ruling, ``convert_extracted_rulings`` substituted the full HTML document
  as ``ruling_text``, hitting the 50000-char truncation cap.  The resulting
  rulings have ``LENGTH(ruling_text) == 50000`` with null case_title /
  judge_name / department and fake historical hearing_dates (2003-2016)
  extracted from HTML body text.

The PR that contains this script routes SD calendar HTML through a
deterministic per-case splitter (``courts.ca.sd_calendar._split_rulings``).
All *new* SD rebuild runs produce clean data.  This script cleans up the
backlog by **null-ing** bad ``ruling_text`` values so:

  * downstream queries stop returning 50000-char raw HTML,
  * cross-contaminated rulings are marked as text-missing instead of
    confidently-wrong,
  * a subsequent reingest can fill ``ruling_text`` from the now-correct
    pipeline without needing to figure out which rows to touch.

The parent document rows are left alone — they still hold the scraper
metadata (case_number, case_title, judge_name from the calendar row) and
are used to recreate the correct ``ruling_text`` on re-run.

Usage (via ECS):

    scripts/ecs-run-task.sh scripts/cleanup_sd_bad_rulings.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_sd_bad_rulings.py
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys

import psycopg
import structlog

from framework.logging import configure_structlog

configure_structlog()
logger = structlog.get_logger()


# Bug B marker: LENGTH(ruling_text) == 50000 AND the text looks like raw HTML.
# The 50000 cap is the database-layer truncation enforced by the worker.
_BUG_B_RULING_TEXT_LEN = 50_000

# SQL fragment detecting "ruling_text is raw HTML".  The check matches any
# of the structural HTML prefixes our guard refuses.  Using LIKE with
# anchored prefixes keeps the check cheap — these columns are stored
# exactly as the worker wrote them.
_RAW_HTML_PREDICATE = (
    "(ruling_text ILIKE '<!DOCTYPE%%' "
    "OR ruling_text ILIKE '<html%%' "
    "OR ruling_text ILIKE '<?xml%%' "
    "OR ruling_text ILIKE '%%<body%%')"
)


def _baseline_counts(cur: psycopg.Cursor) -> dict[str, int]:
    """Return a snapshot of counts for the SD corruption patterns."""
    # Total SD rulings.
    cur.execute(
        """
        SELECT COUNT(*)
        FROM rulings r
        JOIN documents d ON r.document_id = d.id
        JOIN courts ct ON d.court_id = ct.id
        WHERE ct.state = 'CA' AND ct.county = 'San Diego'
        """,
    )
    total = cur.fetchone()[0]

    # Bug B: 50000-char raw-HTML dumps.
    cur.execute(
        f"""
        SELECT COUNT(*)
        FROM rulings r
        JOIN documents d ON r.document_id = d.id
        JOIN courts ct ON d.court_id = ct.id
        WHERE ct.state = 'CA'
          AND ct.county = 'San Diego'
          AND LENGTH(r.ruling_text) = %s
          AND {_RAW_HTML_PREDICATE.replace("ruling_text", "r.ruling_text")}
        """,
        (_BUG_B_RULING_TEXT_LEN,),
    )
    bug_b = cur.fetchone()[0]

    # Bug A: cross-contamination — ruling_text contains a case_number
    # that does NOT match the ruling's own case_number.  This is expensive
    # in the general case so we restrict it to the SD subset and only
    # look for obvious short-form case numbers (e.g. 24CU016153C).
    cur.execute(
        """
        SELECT COUNT(*)
        FROM rulings r
        JOIN documents d ON r.document_id = d.id
        JOIN courts ct ON d.court_id = ct.id
        JOIN cases cs ON r.case_id = cs.id
        WHERE ct.state = 'CA'
          AND ct.county = 'San Diego'
          AND r.ruling_text IS NOT NULL
          AND cs.case_number IS NOT NULL
          AND r.ruling_text ~ '[0-9]{2}C[ULV][0-9]{6}C?'
          AND position(cs.case_number IN r.ruling_text) = 0
        """,
    )
    bug_a = cur.fetchone()[0]

    # Any ruling_text that is raw HTML regardless of length.
    cur.execute(
        f"""
        SELECT COUNT(*)
        FROM rulings r
        JOIN documents d ON r.document_id = d.id
        JOIN courts ct ON d.court_id = ct.id
        WHERE ct.state = 'CA'
          AND ct.county = 'San Diego'
          AND r.ruling_text IS NOT NULL
          AND {_RAW_HTML_PREDICATE.replace("ruling_text", "r.ruling_text")}
        """,
    )
    raw_html_any = cur.fetchone()[0]

    return {
        "total_sd_rulings": total,
        "bug_a_cross_contaminated": bug_a,
        "bug_b_50000_char_raw_html": bug_b,
        "any_raw_html_ruling_text": raw_html_any,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without actually changing.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            before = _baseline_counts(cur)
        logger.info("Baseline counts", **before)

        if (
            before["bug_a_cross_contaminated"] == 0
            and before["any_raw_html_ruling_text"] == 0
        ):
            logger.info("Nothing to clean up — SD data looks healthy")
            return

        if args.dry_run:
            logger.info(
                "DRY RUN — would null ruling_text for %d cross-contaminated "
                "rulings and %d raw-HTML rulings",
                before["bug_a_cross_contaminated"],
                before["any_raw_html_ruling_text"],
            )
            return

        with conn.cursor() as cur:
            # Step 1: null out raw-HTML ruling_text (Bug B + any related).
            cur.execute(
                f"""
                UPDATE rulings r
                SET ruling_text = NULL,
                    ruling_text_html = NULL
                FROM documents d, courts ct
                WHERE r.document_id = d.id
                  AND d.court_id = ct.id
                  AND ct.state = 'CA'
                  AND ct.county = 'San Diego'
                  AND r.ruling_text IS NOT NULL
                  AND {_RAW_HTML_PREDICATE.replace("ruling_text", "r.ruling_text")}
                """,
            )
            bug_b_fixed = cur.rowcount

            # Step 2: null out cross-contaminated ruling_text.  Use the
            # same predicate as the baseline query — ruling_text contains a
            # case-number pattern but NOT the ruling's own case number.
            cur.execute(
                """
                UPDATE rulings r
                SET ruling_text = NULL,
                    ruling_text_html = NULL
                FROM documents d, courts ct, cases cs
                WHERE r.document_id = d.id
                  AND d.court_id = ct.id
                  AND r.case_id = cs.id
                  AND ct.state = 'CA'
                  AND ct.county = 'San Diego'
                  AND r.ruling_text IS NOT NULL
                  AND cs.case_number IS NOT NULL
                  AND r.ruling_text ~ '[0-9]{2}C[ULV][0-9]{6}C?'
                  AND position(cs.case_number IN r.ruling_text) = 0
                """,
            )
            bug_a_fixed = cur.rowcount

        conn.commit()

        logger.info(
            "Cleanup complete",
            bug_b_raw_html_rulings_nulled=bug_b_fixed,
            bug_a_cross_contaminated_nulled=bug_a_fixed,
        )

        # Verification: re-run counts post-update.
        with conn.cursor() as cur:
            after = _baseline_counts(cur)
        logger.info("Post-cleanup counts", **after)

        if (
            after["bug_a_cross_contaminated"] != 0
            or after["any_raw_html_ruling_text"] != 0
        ):
            logger.error(
                "Cleanup finished but residual bad rows remain — investigate",
                **after,
            )
            sys.exit(2)


if __name__ == "__main__":
    main()
