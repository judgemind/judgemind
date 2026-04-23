#!/usr/bin/env python3
"""One-time backfill: set case_type for Ventura cases using motion type fallback.

Ventura case 202300574258 (P Z vs. City of Santa Paula) is an all-digit case
number with no embedded type code.  The previous backfill (#1694) could not
infer its type.  This script uses the ruling's motion_type as a fallback signal:
civil motion types (demurrer, motion_to_compel, msj, etc.) unambiguously
identify civil cases.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type_motion.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_ventura_case_type_motion.py
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# Motion types that unambiguously identify civil cases.
_CIVIL_MOTION_TYPES: frozenset[str] = frozenset(
    {
        "msj",
        "msj_partial",
        "mtd",
        "demurrer",
        "motion_to_compel",
        "anti_slapp",
        "motion_to_strike",
        "preliminary_injunction",
        "motion_for_protective_order",
        "motion_for_attorney_fees",
        "motion_to_set_aside_default",
        "motion_to_vacate",
        "default_judgment",
        "motion_to_be_relieved_as_counsel",
        "motion_for_leave_to_amend",
        "motion_for_sanctions",
        "motion_for_relief",
        "motion_pro_hac_vice",
        "motion_to_substitute",
        "motion_to_tax_costs",
        "motion_for_new_trial",
        "motion_for_reconsideration",
        "motion_to_quash",
        "class_action_settlement",
        "writ_of_possession",
        "mil",
    }
)

# Motion types that unambiguously identify probate cases.
_PROBATE_MOTION_TYPES: frozenset[str] = frozenset(
    {
        "petition",
    }
)


def _infer_case_type_from_motion(motion_type: str) -> str | None:
    """Infer case type from a normalized motion type."""
    if not motion_type:
        return None
    mt = motion_type.strip()
    if mt in _CIVIL_MOTION_TYPES:
        return "civil"
    if mt in _PROBATE_MOTION_TYPES:
        return "probate"
    return None


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> int:
    """Set case_type for cases with NULL case_type using motion type inference.

    Targets all counties, not just Ventura, but the primary use case is
    Ventura case 202300574258.

    Returns the number of rows updated.
    """
    with conn.cursor() as cur:
        # Find cases with NULL case_type that have rulings with a motion_type.
        cur.execute(
            """
            SELECT DISTINCT c.id, c.case_number, ct.county, r.motion_type
            FROM cases c
            JOIN courts ct ON ct.id = c.court_id
            JOIN rulings r ON r.case_id = c.id
            WHERE c.case_type IS NULL
              AND r.motion_type IS NOT NULL
            ORDER BY ct.county, c.case_number
            """
        )
        rows = cur.fetchall()
        logger.info(
            "Found %d cases with NULL case_type and non-NULL motion_type", len(rows)
        )

        updated = 0
        skipped = 0
        for case_id, case_number, county, motion_type in rows:
            inferred = _infer_case_type_from_motion(motion_type)
            if inferred is None:
                logger.info(
                    "Cannot infer case_type from motion_type=%s "
                    "(case_number=%s, county=%s, case_id=%s)",
                    motion_type,
                    case_number,
                    county,
                    case_id,
                )
                skipped += 1
                continue

            logger.info(
                "%s case_type=%s for case_number=%s (county=%s, motion_type=%s)",
                "Would set" if dry_run else "Setting",
                inferred,
                case_number,
                county,
                motion_type,
            )
            if not dry_run:
                cur.execute(
                    "UPDATE cases SET case_type = %s WHERE id = %s",
                    (inferred, case_id),
                )
            updated += 1

        logger.info(
            "%s %d cases, skipped %d (ambiguous motion type)",
            "Would update" if dry_run else "Updated",
            updated,
            skipped,
        )

    if dry_run:
        conn.rollback()
        logger.info("DRY RUN — no changes committed.")
    else:
        conn.commit()
        logger.info("Changes committed.")

    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case_type from motion type for unparseable case numbers (#1702)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        updated = run_backfill(conn, dry_run=args.dry_run)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info("Backfill complete (%s): %d cases updated", mode, updated)


if __name__ == "__main__":
    main()
