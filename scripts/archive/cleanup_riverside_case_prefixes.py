#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Strip case number prefixes from Riverside case titles and party names.

After the Riverside data remediation (#1985), some case titles and party
names still contain prepended case numbers (e.g. "Cvri2306607 Cao v. Hu").
This script finds and strips those prefixes.

Usage via ECS (preferred — dev DB is in a private VPC):
    scripts/ecs-run-task.sh scripts/cleanup_riverside_case_prefixes.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_riverside_case_prefixes.py -- --apply

Options:
    --dry-run   (default) Show what would change without writing.
    --apply     Actually execute the updates.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Case number prefix pattern
# ---------------------------------------------------------------------------

# Riverside case numbers:
#   CV-prefixed: CV + 2-4 letter location code + 6-10 digits (e.g. CVPS2306157)
#   Location-prefixed: RIC, MCC, PSC, SWC, INC + 0-4 letters + 6-10 digits
#   Additional prefixes (#2192): CIV (civil), MVC (motor vehicle collision),
#     TEC (Temecula), UDPS (unlawful detainer Palm Springs)
# The prefix may appear in mixed case (e.g. Cvps2405799).
_CASE_NUMBER_PREFIX_RE = re.compile(
    r"^(?:CV[A-Za-z]{2,4}|(?:RIC|MCC|PSC|SWC|INC|CIV|MVC|TEC|UDPS)[A-Za-z]{0,4})\d{6,10}\s+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pure functions (testable without DB)
# ---------------------------------------------------------------------------


def strip_case_number_prefix(text: str) -> str | None:
    """Strip a Riverside case number prefix from the start of text.

    Returns the cleaned text (leading/trailing whitespace stripped) if a
    prefix was found, or ``None`` if no prefix was detected.
    """
    if not text:
        return None
    match = _CASE_NUMBER_PREFIX_RE.match(text)
    if match:
        cleaned = text[match.end() :].strip()
        # If stripping leaves an empty string, return None (nothing useful)
        return cleaned if cleaned else None
    return None


def is_riverside_case_number_prefix(text: str) -> bool:
    """Check whether *text* starts with a Riverside case number prefix."""
    if not text:
        return False
    return _CASE_NUMBER_PREFIX_RE.match(text) is not None


# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

# Find Riverside case titles with case number prefixes.
FIND_CASE_TITLES_QUERY = """
    SELECT c.id, c.case_title
    FROM cases c
    JOIN courts ct ON c.court_id = ct.id
    WHERE ct.county = 'Riverside'
      AND c.case_title IS NOT NULL
      AND c.case_title ~ '^[A-Za-z]*\\d{5,}'
    ORDER BY c.id
"""

UPDATE_CASE_TITLE_QUERY = """
    UPDATE cases SET case_title = %s, updated_at = NOW()
    WHERE id = %s::uuid
"""

# Find Riverside party canonical_names with case number prefixes.
# Parties link to cases via the case_parties junction table.
FIND_PARTY_NAMES_QUERY = """
    SELECT DISTINCT p.id, p.canonical_name
    FROM parties p
    JOIN case_parties cp ON cp.party_id = p.id
    JOIN cases c ON cp.case_id = c.id
    JOIN courts ct ON c.court_id = ct.id
    WHERE ct.county = 'Riverside'
      AND p.canonical_name ~ '^[A-Za-z]*\\d{5,}'
    ORDER BY p.id
"""

UPDATE_PARTY_NAME_QUERY = """
    UPDATE parties SET canonical_name = %s, updated_at = NOW()
    WHERE id = %s::uuid
"""

# Find Riverside party alias raw_names with case number prefixes.
FIND_ALIAS_NAMES_QUERY = """
    SELECT DISTINCT pa.id, pa.raw_name
    FROM party_aliases pa
    JOIN parties p ON pa.party_id = p.id
    JOIN case_parties cp ON cp.party_id = p.id
    JOIN cases c ON cp.case_id = c.id
    JOIN courts ct ON c.court_id = ct.id
    WHERE ct.county = 'Riverside'
      AND pa.raw_name ~ '^[A-Za-z]*\\d{5,}'
    ORDER BY pa.id
"""

UPDATE_ALIAS_NAME_QUERY = """
    UPDATE party_aliases SET raw_name = %s
    WHERE id = %s::uuid
"""


# ---------------------------------------------------------------------------
# Core cleanup logic
# ---------------------------------------------------------------------------


def cleanup_case_titles(
    conn: psycopg.Connection,
    *,
    dry_run: bool = True,
) -> dict[str, int]:
    """Find and strip case number prefixes from Riverside case titles.

    Returns dict with 'found', 'cleaned', and 'skipped' counts.
    """
    found = 0
    cleaned = 0
    skipped = 0

    with conn.cursor() as cur:
        cur.execute(FIND_CASE_TITLES_QUERY)
        rows = cur.fetchall()

    found = len(rows)
    logger.info("Found %d case titles with potential case number prefixes", found)

    for row_id, case_title in rows:
        new_title = strip_case_number_prefix(case_title)
        if new_title is None:
            # The regex in SQL is broader than our Python regex — this row
            # matched the SQL pattern but not the Python one.  Skip it.
            logger.debug("Skipping case %s — no Python match: %r", row_id, case_title)
            skipped += 1
            continue

        if dry_run:
            logger.info("[DRY-RUN] Case %s: %r -> %r", row_id, case_title, new_title)
        else:
            with conn.cursor() as cur:
                cur.execute(UPDATE_CASE_TITLE_QUERY, (new_title, str(row_id)))
            logger.info("Updated case %s: %r -> %r", row_id, case_title, new_title)
        cleaned += 1

    if not dry_run and cleaned > 0:
        conn.commit()
    elif dry_run:
        conn.rollback()

    return {"found": found, "cleaned": cleaned, "skipped": skipped}


def cleanup_party_names(
    conn: psycopg.Connection,
    *,
    dry_run: bool = True,
) -> dict[str, int]:
    """Find and strip case number prefixes from Riverside party canonical_names.

    Returns dict with 'found', 'cleaned', and 'skipped' counts.
    """
    found = 0
    cleaned = 0
    skipped = 0

    with conn.cursor() as cur:
        cur.execute(FIND_PARTY_NAMES_QUERY)
        rows = cur.fetchall()

    found = len(rows)
    logger.info("Found %d party names with potential case number prefixes", found)

    for row_id, canonical_name in rows:
        new_name = strip_case_number_prefix(canonical_name)
        if new_name is None:
            logger.debug(
                "Skipping party %s — no Python match: %r", row_id, canonical_name
            )
            skipped += 1
            continue

        if dry_run:
            logger.info(
                "[DRY-RUN] Party %s: %r -> %r", row_id, canonical_name, new_name
            )
        else:
            with conn.cursor() as cur:
                cur.execute(UPDATE_PARTY_NAME_QUERY, (new_name, str(row_id)))
            logger.info("Updated party %s: %r -> %r", row_id, canonical_name, new_name)
        cleaned += 1

    if not dry_run and cleaned > 0:
        conn.commit()
    elif dry_run:
        conn.rollback()

    return {"found": found, "cleaned": cleaned, "skipped": skipped}


def cleanup_alias_names(
    conn: psycopg.Connection,
    *,
    dry_run: bool = True,
) -> dict[str, int]:
    """Find and strip case number prefixes from Riverside party_aliases raw_names.

    Returns dict with 'found', 'cleaned', and 'skipped' counts.
    """
    found = 0
    cleaned = 0
    skipped = 0

    with conn.cursor() as cur:
        cur.execute(FIND_ALIAS_NAMES_QUERY)
        rows = cur.fetchall()

    found = len(rows)
    logger.info("Found %d alias names with potential case number prefixes", found)

    for row_id, raw_name in rows:
        new_name = strip_case_number_prefix(raw_name)
        if new_name is None:
            logger.debug("Skipping alias %s — no Python match: %r", row_id, raw_name)
            skipped += 1
            continue

        if dry_run:
            logger.info("[DRY-RUN] Alias %s: %r -> %r", row_id, raw_name, new_name)
        else:
            with conn.cursor() as cur:
                cur.execute(UPDATE_ALIAS_NAME_QUERY, (new_name, str(row_id)))
            logger.info("Updated alias %s: %r -> %r", row_id, raw_name, new_name)
        cleaned += 1

    if not dry_run and cleaned > 0:
        conn.commit()
    elif dry_run:
        conn.rollback()

    return {"found": found, "cleaned": cleaned, "skipped": skipped}


def run_cleanup(
    dsn: str,
    *,
    dry_run: bool = True,
) -> dict[str, dict[str, int]]:
    """Run the full cleanup.  Returns summary stats per entity type."""
    with psycopg.connect(dsn) as conn:
        title_stats = cleanup_case_titles(conn, dry_run=dry_run)
        party_stats = cleanup_party_names(conn, dry_run=dry_run)
        alias_stats = cleanup_alias_names(conn, dry_run=dry_run)

    return {
        "case_titles": title_stats,
        "party_names": party_stats,
        "alias_names": alias_stats,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Strip case number prefixes from Riverside case titles and party names.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually execute updates (default is dry-run).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would change without writing (this is the default).",
    )
    args = parser.parse_args()

    # Default is dry-run; --apply overrides it
    dry_run = not args.apply

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info("Starting Riverside case prefix cleanup (%s mode)", mode)

    t0 = time.monotonic()
    stats = run_cleanup(dsn, dry_run=dry_run)
    elapsed = time.monotonic() - t0

    logger.info("--- Summary (%s mode) ---", mode)
    for entity, counts in stats.items():
        logger.info(
            "  %s: found=%d cleaned=%d skipped=%d",
            entity,
            counts["found"],
            counts["cleaned"],
            counts["skipped"],
        )
    logger.info("Completed in %.1fs", elapsed)

    total_cleaned = sum(c["cleaned"] for c in stats.values())
    if total_cleaned == 0:
        logger.info("No rows needed cleaning.")


if __name__ == "__main__":
    main()
