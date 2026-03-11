#!/usr/bin/env python3
"""Clean up judge records that are actually organization/party names.

PR #333 fixed `extract_judge_name()` to reject organization/party names going
forward, but existing records in the database where party names were
incorrectly stored as judge names need to be cleaned up.

This script:
1. Finds judges whose canonical_name matches the organization keyword regex
   (LLC, Inc, Corp, Medical, Group, Hospital, Bank, etc.) or other
   non-person-name patterns (case titles with "vs"/"v.", too-long names).
2. Nullifies the judge_id on rulings linked to those judges.
3. Removes the corresponding case_judges links.
4. Deletes orphaned judge records (judges with no remaining rulings).
5. Cleans up judge_aliases for deleted judges.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/cleanup_org_judges.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/cleanup_org_judges.py --dry-run

Options:
    --dry-run       Print what would be changed without writing to the database.
    --batch-size N  Number of judges to process per batch (default: 100).
    --limit N       Maximum total judges to process (default: unlimited).

The script is idempotent — re-running it will find no matching judges if all
organization names have already been cleaned up.
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

import argparse
import logging
import os
import re
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Organization / non-person-name detection
# ---------------------------------------------------------------------------

# Mirrors _ORG_KEYWORD_RE from ingestion/extract.py — kept in sync so that
# this cleanup catches the same names the extractor now rejects.
_ORG_KEYWORD_RE = re.compile(
    r"\b(?:"
    r"LLC|INC|CORP|CORPORATION|LTD|L\.P\.|N\.A\."
    r"|GROUP|MEDICAL|HOSPITAL|CLINIC|INSURANCE|BANK"
    r"|ASSOCIATES|PARTNERS|FOUNDATION|UNIVERSITY|COLLEGE"
    r"|SCHOOL|DISTRICT|AUTHORITY|COMPANY|ENTERPRISES"
    r"|SERVICES|SOLUTIONS|INDUSTRIES|HOLDINGS|PROPERTIES"
    r"|MANAGEMENT|HEALTHCARE|FINANCIAL|INVESTMENTS"
    r"|TRUST|ESTATE|ASSOCIATION|SOCIETY|UNION"
    r"|COUNTY\s+OF|CITY\s+OF|STATE\s+OF|PEOPLE\s+OF|DEPARTMENT\s+OF"
    r"|D/B/A|DBA"
    r")\b",
    re.IGNORECASE,
)


def looks_like_org_name(name: str) -> bool:
    """Return True if *name* looks like an organization or party name, not a judge.

    This mirrors the logic in ``_looks_like_person_name()`` from
    ``ingestion/extract.py`` but inverted: returns True when the name is NOT
    a plausible person name.
    """
    if not name:
        return False

    upper = name.upper()

    # Organization keywords
    if _ORG_KEYWORD_RE.search(upper):
        return True

    # Case title leaked into judge name: contains "VS" or "V." separator
    if re.search(r"\bVS\b", upper) or re.search(r"\bV\.", upper):
        return True

    # "A CALIFORNIA CORPORATION" or similar entity descriptors
    if re.search(r"\bA\s+CALIFORNIA\b", upper):
        return True

    # Too long to be a real judge name (> 60 chars)
    if len(name) > 60:
        return True

    return False


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"

# Fetch judges using keyset pagination on judge id.
FETCH_JUDGES_QUERY = """
    SELECT j.id, j.canonical_name
    FROM judges j
    WHERE j.id > %s::uuid
    ORDER BY j.id
    LIMIT %s
"""

# Nullify judge_id on all rulings linked to a specific judge.
NULLIFY_RULINGS_QUERY = """
    UPDATE rulings
    SET judge_id = NULL
    WHERE judge_id = %s::uuid
"""

# Count rulings affected (for logging).
COUNT_RULINGS_QUERY = """
    SELECT COUNT(*) FROM rulings WHERE judge_id = %s::uuid
"""

# Delete case_judges links for a specific judge.
DELETE_CASE_JUDGES_QUERY = """
    DELETE FROM case_judges
    WHERE judge_id = %s::uuid
"""

# Count case_judges links (for logging).
COUNT_CASE_JUDGES_QUERY = """
    SELECT COUNT(*) FROM case_judges WHERE judge_id = %s::uuid
"""

# Delete judge_aliases for a specific judge.
DELETE_JUDGE_ALIASES_QUERY = """
    DELETE FROM judge_aliases
    WHERE judge_id = %s::uuid
"""

# Delete the judge record itself.
DELETE_JUDGE_QUERY = """
    DELETE FROM judges
    WHERE id = %s::uuid
"""


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------


def cleanup_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: str = _CURSOR_MIN_UUID,
) -> tuple[int, int, int, int, str]:
    """Process one batch of judges.

    Returns (processed, judges_cleaned, rulings_nullified,
             case_judges_removed, next_cursor).
    """
    processed = 0
    judges_cleaned = 0
    rulings_nullified = 0
    case_judges_removed = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_JUDGES_QUERY, (cursor, batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, 0, 0, cursor

    for judge_id, canonical_name in rows:
        judge_id_str = str(judge_id)
        next_cursor = judge_id_str
        processed += 1

        if not looks_like_org_name(canonical_name):
            continue

        # Count affected rows for logging
        with conn.cursor() as cur:
            cur.execute(COUNT_RULINGS_QUERY, (judge_id_str,))
            row = cur.fetchone()
            ruling_count = row[0] if row else 0

        with conn.cursor() as cur:
            cur.execute(COUNT_CASE_JUDGES_QUERY, (judge_id_str,))
            row = cur.fetchone()
            case_judge_count = row[0] if row else 0

        logger.info(
            "Cleaning org judge: %r (id=%s, rulings=%d, case_judges=%d)",
            canonical_name,
            judge_id_str,
            ruling_count,
            case_judge_count,
        )

        # 1. Nullify judge_id on rulings
        with conn.cursor() as cur:
            cur.execute(NULLIFY_RULINGS_QUERY, (judge_id_str,))
        rulings_nullified += ruling_count

        # 2. Remove case_judges links
        with conn.cursor() as cur:
            cur.execute(DELETE_CASE_JUDGES_QUERY, (judge_id_str,))
        case_judges_removed += case_judge_count

        # 3. Delete judge_aliases
        with conn.cursor() as cur:
            cur.execute(DELETE_JUDGE_ALIASES_QUERY, (judge_id_str,))

        # 4. Delete the judge record
        with conn.cursor() as cur:
            cur.execute(DELETE_JUDGE_QUERY, (judge_id_str,))

        judges_cleaned += 1

    return (
        processed,
        judges_cleaned,
        rulings_nullified,
        case_judges_removed,
        next_cursor,
    )


def run_cleanup(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full cleanup. Returns summary stats."""
    total_processed = 0
    total_judges_cleaned = 0
    total_rulings_nullified = 0
    total_case_judges_removed = 0
    cursor: str = _CURSOR_MIN_UUID

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            (
                processed,
                judges_cleaned,
                rulings_nullified,
                case_judges_removed,
                cursor,
            ) = cleanup_batch(conn, effective_batch, cursor)
            total_processed += processed
            total_judges_cleaned += judges_cleaned
            total_rulings_nullified += rulings_nullified
            total_case_judges_removed += case_judges_removed

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d cleaned=%d "
                "(total: judges=%d/%d, rulings_nullified=%d, case_judges_removed=%d)%s",
                processed,
                judges_cleaned,
                total_processed,
                total_judges_cleaned,
                total_rulings_nullified,
                total_case_judges_removed,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                break

    return {
        "total_processed": total_processed,
        "total_judges_cleaned": total_judges_cleaned,
        "total_rulings_nullified": total_rulings_nullified,
        "total_case_judges_removed": total_case_judges_removed,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up judge records that are actually organization/party names.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of judges per batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total judges to process.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_cleanup(
        dsn,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Cleanup complete: %d judges processed, %d cleaned up "
        "(%d rulings nullified, %d case_judges removed)",
        stats["total_processed"],
        stats["total_judges_cleaned"],
        stats["total_rulings_nullified"],
        stats["total_case_judges_removed"],
    )


if __name__ == "__main__":
    main()
