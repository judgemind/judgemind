#!/usr/bin/env python3
"""Fix OC case titles truncated at the 'vs' separator (#1360).

Finds cases with titles ending in 'vs'/'vs.' (missing defendant) or starting
with 'vs'/'vs.' (missing plaintiff), and attempts to recover the full title
from the ruling text.

The truncation happened because pdfplumber's table extraction split multi-line
case names across rows in the case-name column, and the text reconstruction
put each row on a separate line.  The title extractor then only saw the first
line (e.g. "Saetern vs.") without the defendant on the next line.

Recovery strategy:
  1. Search ruling_text for the full "Plaintiff vs. Defendant" pattern near the
     start of the text (within the first ~500 chars, where the case header is).
  2. If the existing title fragment matches part of the discovered title, adopt it.
  3. Skip cases where no confident match is found (manual review needed).

Usage:
    scripts/ecs-run-task.sh scripts/backfill_oc_truncated_titles.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_oc_truncated_titles.py
"""

# venv: scraper-framework
from __future__ import annotations

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

# OC court UUID (constant across environments)
OC_COURT_ID = "785660ca-0169-431c-8cab-34c55ca4d3b0"

# ---------------------------------------------------------------------------
# Find truncated titles
# ---------------------------------------------------------------------------

FETCH_TRUNCATED = """
    SELECT c.id, c.case_title, r.ruling_text
    FROM cases c
    JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.court_id = %s
      AND c.case_title IS NOT NULL
      AND (
          c.case_title ~ '(vs\\.?|v\\.)$'
          OR c.case_title ~ '^(vs\\.?|v\\.) '
      )
    ORDER BY c.id
"""

UPDATE_TITLE = """
    UPDATE cases
    SET case_title = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
"""

# ---------------------------------------------------------------------------
# Title recovery from ruling text
# ---------------------------------------------------------------------------

# Matches "Name vs. Name" or "Name v. Name" patterns in ruling text.
# Captures both plaintiff and defendant groups.
_VS_PATTERN = re.compile(
    r"(?P<plaintiff>[A-Z][A-Za-z,.'\-\s]{1,80}?)"
    r"\s+(?:vs?\.?)\s+"
    r"(?P<defendant>[A-Z][A-Za-z,.'\-\s]{1,80})",
    re.IGNORECASE,
)


def _clean_name(name: str) -> str:
    """Clean up a party name extracted from ruling text."""
    name = " ".join(name.split()).strip()
    # Strip trailing punctuation and common suffixes
    name = name.rstrip(".,;:() ")
    # Strip "et al." suffix
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    return name


def recover_title(
    existing_title: str,
    ruling_text: str,
) -> str | None:
    """Try to recover a complete case title from ruling text.

    Uses the existing (truncated) title as a hint to find the right match
    in the ruling text.

    Returns the recovered title, or None if no confident match found.
    """
    if not ruling_text:
        return None

    existing_lower = existing_title.lower().strip()

    # Determine what we have: title ending in "vs" (missing defendant)
    # or starting with "vs" (missing plaintiff).
    ends_with_vs = bool(re.search(r"(?:vs\.?|v\.)\s*$", existing_lower))
    starts_with_vs = bool(re.search(r"^(?:vs\.?|v\.)\s", existing_lower))

    if ends_with_vs:
        # Extract the plaintiff fragment from existing title
        plaintiff_fragment = re.sub(
            r"\s*(?:vs\.?|v\.)\s*$", "", existing_title, flags=re.IGNORECASE
        ).strip()
        if not plaintiff_fragment:
            return None

        # Search for this plaintiff in the ruling text + "vs" + defendant
        # Look in the first 1000 chars (case header area)
        search_area = ruling_text[:1000]
        for m in _VS_PATTERN.finditer(search_area):
            p = _clean_name(m.group("plaintiff"))
            d = _clean_name(m.group("defendant"))
            # Check if the plaintiff fragment matches
            if (
                plaintiff_fragment.lower() in p.lower()
                or p.lower() in plaintiff_fragment.lower()
            ):
                title = f"{p} vs. {d}"
                if len(title) <= 200:
                    return title

    elif starts_with_vs:
        # Extract the defendant fragment from existing title
        defendant_fragment = re.sub(
            r"^(?:vs\.?|v\.)\s*", "", existing_title, flags=re.IGNORECASE
        ).strip()
        if not defendant_fragment:
            return None

        # Search for plaintiff + "vs" + this defendant in the ruling text
        search_area = ruling_text[:1000]
        for m in _VS_PATTERN.finditer(search_area):
            p = _clean_name(m.group("plaintiff"))
            d = _clean_name(m.group("defendant"))
            # Check if the defendant fragment matches
            if (
                defendant_fragment.lower() in d.lower()
                or d.lower() in defendant_fragment.lower()
            ):
                title = f"{p} vs. {d}"
                if len(title) <= 200:
                    return title

    return None


# ---------------------------------------------------------------------------
# Main backfill
# ---------------------------------------------------------------------------


def run_backfill(
    dsn: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Run the truncated title backfill."""
    total = 0
    updated = 0
    skipped = 0

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(FETCH_TRUNCATED, (OC_COURT_ID,))
            rows = cur.fetchall()

        logger.info("Found %d cases with truncated titles", len(rows))

        for case_id, case_title, ruling_text in rows:
            if limit is not None and total >= limit:
                break

            total += 1
            new_title = recover_title(case_title, ruling_text)

            if new_title is None:
                logger.warning(
                    "Could not recover title for case %s: '%s'",
                    case_id,
                    case_title,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s: '%s' -> '%s'",
                case_id,
                case_title,
                new_title,
            )

            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(UPDATE_TITLE, (new_title, str(case_id)))

            updated += 1

        if dry_run:
            conn.rollback()
            logger.info("Dry run — rolled back all changes")
        else:
            conn.commit()
            logger.info("Committed %d title updates", updated)

    return {
        "total": total,
        "updated": updated,
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix OC case titles truncated at 'vs' separator (#1360).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing to the database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum cases to process.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(dsn, dry_run=args.dry_run, limit=args.limit)

    logger.info(
        "Backfill complete: %d processed, %d updated, %d skipped",
        stats["total"],
        stats["updated"],
        stats["skipped"],
    )


if __name__ == "__main__":
    main()
