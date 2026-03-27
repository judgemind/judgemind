#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill San Bernardino case titles and party names (#1245).

Finds SB cases where case_title contains motion-related keywords that
were incorrectly parsed as party names.  For each affected case:
  1. Clears the garbled case_title.
  2. Re-extracts the case title from ruling_text using the fixed parser.
  3. Deletes garbled party records derived from the old title.
  4. Re-extracts parties from the corrected title.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh).

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_sb_party_names.py

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

# Add the scraper-framework src directory to sys.path so we can import
# the shared extraction utilities from the ingestion package.
_FRAMEWORK_SRC = os.path.join(
    os.path.dirname(__file__),
    "..",
    "packages",
    "scraper-framework",
    "src",
)
sys.path.insert(0, os.path.normpath(_FRAMEWORK_SRC))

from ingestion.extract import (  # noqa: E402
    _looks_like_motion_text,
    extract_case_title,
    extract_parties_from_caption,
    is_plausible_case_title,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# Motion keywords to search for in case_title.  These are the same keywords
# used by _looks_like_motion_text but formatted for a SQL ILIKE query.
_MOTION_SQL_PATTERNS = [
    "%motion%",
    "%granting%",
    "%denying%",
    "%order %v.%",
    "%ruling on%",
    "%demurrer%v.%",
    "%petition%v.%",
    "%application%v.%",
    "%judgment%v.%",
    "%summary%v.%",
    "%disqualify%",
    "%compel%v.%",
    "%strike%v.%",
    "%dismiss%v.%",
    "%vacate%v.%",
    "%sanctions%v.%",
    "%default%v.%",
    "%ex parte%",
]


def _find_affected_cases(
    conn: psycopg.Connection,
    batch_size: int,
    limit: int | None,
) -> list[tuple[str, str, str | None]]:
    """Find SB cases with garbled case titles.

    Returns list of (case_id, case_title, ruling_text) tuples.
    """
    # Build OR clause for all motion patterns
    pattern_clauses = " OR ".join("c.case_title ILIKE %s" for _ in _MOTION_SQL_PATTERNS)
    query = f"""
        SELECT c.id, c.case_title, r.ruling_text
        FROM cases c
        JOIN courts ct ON c.court_id = ct.id
        LEFT JOIN rulings r ON r.case_id = c.id
        WHERE ct.county = 'San Bernardino'
          AND c.case_title IS NOT NULL
          AND ({pattern_clauses})
        ORDER BY c.id
    """
    if limit is not None:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query, _MOTION_SQL_PATTERNS)
        rows = cur.fetchall()

    # Further filter: only cases where _looks_like_motion_text confirms
    results = []
    for case_id, case_title, ruling_text in rows:
        if _looks_like_motion_text(case_title):
            results.append((case_id, case_title, ruling_text))

    return results


def _fix_case(
    conn: psycopg.Connection,
    case_id: str,
    old_title: str,
    ruling_text: str | None,
    dry_run: bool,
) -> dict[str, str | None]:
    """Fix a single case: update case_title and re-extract parties.

    Returns a dict with old_title, new_title, and action taken.
    """
    # Re-extract case title from ruling text and validate (#1974)
    new_title = None
    if ruling_text:
        candidate = extract_case_title(ruling_text)
        if candidate and is_plausible_case_title(candidate):
            new_title = candidate
        elif candidate:
            logger.warning(
                "Rejected implausible title for case %s: %r",
                case_id,
                candidate,
            )

    if dry_run:
        return {
            "case_id": case_id,
            "old_title": old_title,
            "new_title": new_title,
            "action": "dry-run",
        }

    with conn.cursor() as cur:
        # Update case_title (may be None if no valid title found)
        cur.execute(
            "UPDATE cases SET case_title = %s WHERE id = %s",
            (new_title, case_id),
        )

        # Delete existing party associations for this case
        cur.execute(
            "DELETE FROM case_parties WHERE case_id = %s",
            (case_id,),
        )

        # Re-extract and insert parties from the corrected title
        if new_title:
            parties = extract_parties_from_caption(new_title)
            for party in parties:
                raw_name = party["name"]

                # Look up existing alias for this raw name
                cur.execute(
                    """
                    SELECT pa.party_id
                    FROM party_aliases pa
                    WHERE pa.raw_name = %s
                    LIMIT 1
                    """,
                    (raw_name,),
                )
                row = cur.fetchone()
                if row:
                    party_id = row[0]
                else:
                    # No alias found — create new party and alias
                    cur.execute(
                        """
                        INSERT INTO parties (canonical_name, party_type)
                        VALUES (%s, NULL)
                        RETURNING id
                        """,
                        (raw_name,),
                    )
                    party_id = cur.fetchone()[0]
                    cur.execute(
                        """
                        INSERT INTO party_aliases
                            (party_id, raw_name, source, confidence, is_verified)
                        VALUES (%s::uuid, %s, 'backfill', 1.0, FALSE)
                        """,
                        (str(party_id), raw_name),
                    )

                # Link party to case
                cur.execute(
                    """
                    INSERT INTO case_parties (case_id, party_id, role)
                    VALUES (%s::uuid, %s::uuid, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (case_id, str(party_id), party["role"]),
                )

    return {
        "case_id": case_id,
        "old_title": old_title,
        "new_title": new_title,
        "action": "fixed",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill garbled SB case titles and party names (#1245)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Cases per commit batch (default: 100)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max total cases to process (default: unlimited)",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set. Use scripts/with-secret.sh.")
        sys.exit(1)

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        logger.info("Finding affected San Bernardino cases...")
        affected = _find_affected_cases(conn, args.batch_size, args.limit)
        logger.info("Found %d cases with garbled case titles", len(affected))

        if not affected:
            logger.info("No affected cases found. Nothing to do.")
            return

        fixed_count = 0
        cleared_count = 0
        for i, (case_id, old_title, ruling_text) in enumerate(affected):
            result = _fix_case(conn, case_id, old_title, ruling_text, args.dry_run)

            if result["new_title"]:
                fixed_count += 1
                logger.info(
                    "[%d/%d] %s: '%s' -> '%s'",
                    i + 1,
                    len(affected),
                    case_id,
                    old_title,
                    result["new_title"],
                )
            else:
                cleared_count += 1
                logger.info(
                    "[%d/%d] %s: '%s' -> NULL (no valid title in ruling text)",
                    i + 1,
                    len(affected),
                    case_id,
                    old_title,
                )

            # Commit in batches
            if not args.dry_run and (i + 1) % args.batch_size == 0:
                conn.commit()
                logger.info("Committed batch of %d", args.batch_size)

        # Final commit
        if not args.dry_run:
            conn.commit()

        logger.info(
            "Done. Total affected: %d, Fixed with new title: %d, Cleared to NULL: %d",
            len(affected),
            fixed_count,
            cleared_count,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
