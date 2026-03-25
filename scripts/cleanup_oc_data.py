#!/usr/bin/env python3
# venv: scraper-framework
"""Clean up OC data: UNKNOWN case numbers and contaminated titles (#1929).

Handles two classes of OC data quality issues:

1. **UNKNOWN case numbers** (240 cases): Cases with placeholder
   ``UNKNOWN-{uuid}`` case numbers from before multimodal extraction.
   - Attempts to extract real case number from ruling text or case title.
   - If the extracted number already exists (another case has it),
     merges rulings/documents into the existing case and deletes the duplicate.
   - If the extracted number is new, updates the case number in place.

2. **Contaminated titles** (31 cases): Titles containing ruling text
   fragments like "Before the Court" or case citations.
   - Extracts the clean "Plaintiff v. Defendant" portion from the title.
   - Falls back to extracting from ruling text if the title is too garbled.

Usage:
    scripts/ecs-run-task.sh scripts/cleanup_oc_data.py -- --dry-run
    scripts/ecs-run-task.sh scripts/cleanup_oc_data.py
"""

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
# Case number extraction from title/ruling text
# ---------------------------------------------------------------------------

# Pattern for OC case numbers: e.g. 30-2025-01476705, 24AVCV01623
_CASE_NUMBER_PATTERNS = [
    re.compile(r"\b(\d{2}-\d{4}-\d{7,})\b"),
    re.compile(r"\b(\d{2}[A-Z]{2,4}\d{4,})\b"),
]


def _extract_case_number_from_text(text: str) -> str | None:
    """Extract a case number from text using regex patterns."""
    if not text:
        return None
    for pattern in _CASE_NUMBER_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Title cleanup
# ---------------------------------------------------------------------------

# Pattern: "Name v. Name Before the Court..." or similar trailing content
_TITLE_CONTAMINATION_PATTERN = re.compile(
    r"^(.*?\b(?:vs?\.?|versus)\s+\S+(?:\s+\S+){0,5}?)"
    r"\s+(?:Before the [Cc]ourt|Discussion|Southern California|"
    r"Cal\.App\.\d+th|moves the|(?:The )?(?:Motion|Petition|Demurrer))",
    re.IGNORECASE,
)

# Pattern for extracting case title from ruling text
_TITLE_VS_PATTERN = re.compile(
    r"(?P<plaintiff>[A-Z][A-Za-z,.'\-\s]{1,60}?)"
    r"\s+(?:vs?\.?)\s+"
    r"(?P<defendant>[A-Z][A-Za-z,.'\-\s]{1,60})",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str | None:
    """Remove ruling text contamination from a case title.

    Returns the cleaned title, or None if the title is too garbled.
    """
    m = _TITLE_CONTAMINATION_PATTERN.match(title)
    if m:
        cleaned = m.group(1).strip().rstrip(".,;:() ")
        # Must have at least a "v." or "vs" separator to be a valid title
        if not re.search(r"\bvs?\.?\b", cleaned, re.IGNORECASE):
            return None
        # Validate that the defendant part (after "v.") is a real name,
        # not ruling text that leaked through the regex.
        vs_match = re.search(r"\b(?:vs?\.?)\s+(.*)", cleaned, re.IGNORECASE)
        if vs_match:
            defendant = vs_match.group(1).strip()
            # "Before the Court" is ruling text, not a defendant name
            if re.search(r"(?i)^Before the", defendant):
                return None
            # Must have at least 2 chars of defendant name
            if len(defendant) < 2:
                return None
        return cleaned
    return None


def _extract_title_from_ruling(ruling_text: str) -> str | None:
    """Extract case title from ruling text."""
    if not ruling_text:
        return None
    # Search in the first 500 chars (case header area)
    search_area = ruling_text[:500]
    m = _TITLE_VS_PATTERN.search(search_area)
    if m:
        p = " ".join(m.group("plaintiff").split()).strip().rstrip(".,;:() ")
        d = " ".join(m.group("defendant").split()).strip().rstrip(".,;:() ")
        if p and d and len(p) < 80 and len(d) < 80:
            # Reject if the defendant part is ruling text, not a real name
            if re.search(r"(?i)^Before the", d):
                return None
            return f"{p} vs. {d}"
    return None


def _extract_defendant_from_ruling(ruling_text: str) -> str | None:
    """Extract defendant name from ruling text patterns like 'by defendant X'.

    Used as a last-resort fallback when the title has a plaintiff name
    but the defendant portion is garbled with ruling text.
    """
    if not ruling_text:
        return None
    m = re.search(
        r"(?:filed by|by)\s+defendant\s+([A-Z][A-Za-z,.'&\-\s]{2,60}?)"
        r"\s*(?:\(|on\b|against\b|,\s*(?:LLC|Inc|Corp))",
        ruling_text[:600],
        re.IGNORECASE,
    )
    if m:
        defendant = " ".join(m.group(1).split()).strip().rstrip(".,;:() ")
        if defendant and len(defendant) >= 2:
            return defendant
    return None


# ---------------------------------------------------------------------------
# Phase 1: Fix UNKNOWN case numbers
# ---------------------------------------------------------------------------

FETCH_UNKNOWN_CASES = """
    SELECT c.id, c.case_number, c.case_title, c.court_id,
           r.ruling_text,
           (SELECT COUNT(*) FROM rulings r3 WHERE r3.case_id = c.id) AS ruling_count,
           (SELECT COUNT(*) FROM documents d WHERE d.case_id = c.id) AS document_count
    FROM cases c
    LEFT JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.court_id = %s
      AND c.case_number LIKE 'UNKNOWN-%%'
    ORDER BY c.id
"""

CHECK_EXISTING_CASE = """
    SELECT id FROM cases
    WHERE court_id = %s AND case_number = %s
    LIMIT 1
"""

DELETE_DUPLICATE_RULINGS = """
    DELETE FROM rulings
    WHERE case_id = %s
      AND ruling_text_hash IN (
          SELECT ruling_text_hash FROM rulings WHERE case_id = %s
      )
"""

MERGE_RULINGS = """
    UPDATE rulings SET case_id = %s, updated_at = NOW()
    WHERE case_id = %s
"""

MERGE_DOCUMENTS = """
    UPDATE documents SET case_id = %s
    WHERE case_id = %s
"""

DELETE_DUPLICATE_CASE = """
    DELETE FROM cases WHERE id = %s
"""

UPDATE_CASE_NUMBER = """
    UPDATE cases
    SET case_number = %s, updated_at = NOW()
    WHERE id = %s AND case_number LIKE 'UNKNOWN-%%'
"""


def _fix_unknown_case_numbers(
    conn: psycopg.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Fix UNKNOWN case numbers by extracting from ruling text or title."""
    stats = {
        "total": 0,
        "updated": 0,
        "merged": 0,
        "deleted_orphans": 0,
        "no_number": 0,
        "errors": 0,
    }

    with conn.cursor() as cur:
        cur.execute(FETCH_UNKNOWN_CASES, (OC_COURT_ID,))
        rows = cur.fetchall()

    logger.info("Found %d cases with UNKNOWN case numbers", len(rows))

    for (
        case_id,
        old_case_number,
        case_title,
        court_id,
        ruling_text,
        ruling_count,
        document_count,
    ) in rows:
        stats["total"] += 1

        # Try to extract case number from ruling text first, then title
        case_number = _extract_case_number_from_text(ruling_text or "")
        if case_number is None and case_title:
            case_number = _extract_case_number_from_text(case_title)

        if case_number is None:
            # If this case has no rulings AND no documents, it's an orphan
            # that can be safely deleted.
            if ruling_count == 0 and document_count == 0:
                logger.info(
                    "DELETE ORPHAN: Case %s (%s) — no rulings, no documents",
                    case_id,
                    old_case_number,
                )
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(DELETE_DUPLICATE_CASE, (str(case_id),))
                stats["deleted_orphans"] += 1
                continue

            logger.debug(
                "No case number found for %s (title: %s, rulings: %d, docs: %d)",
                case_id,
                case_title,
                ruling_count,
                document_count,
            )
            stats["no_number"] += 1
            continue

        # Check if a case with this number already exists
        with conn.cursor() as cur:
            cur.execute(CHECK_EXISTING_CASE, (court_id, case_number))
            existing = cur.fetchone()

        if existing:
            existing_id = existing[0]
            if str(existing_id) == str(case_id):
                # Same case, shouldn't happen but skip
                continue

            logger.info(
                "MERGE: Case %s (%s) -> existing case %s (%s)",
                case_id,
                old_case_number,
                existing_id,
                case_number,
            )

            if not dry_run:
                with conn.cursor() as cur:
                    # Delete rulings that already exist in the target case
                    # (same ruling_text_hash) to avoid unique constraint violations
                    cur.execute(
                        DELETE_DUPLICATE_RULINGS,
                        (str(case_id), str(existing_id)),
                    )
                    # Move remaining rulings and documents to the existing case
                    cur.execute(MERGE_RULINGS, (str(existing_id), str(case_id)))
                    cur.execute(MERGE_DOCUMENTS, (str(existing_id), str(case_id)))
                    cur.execute(DELETE_DUPLICATE_CASE, (str(case_id),))

            stats["merged"] += 1
        else:
            logger.info(
                "UPDATE: Case %s: %s -> %s",
                case_id,
                old_case_number,
                case_number,
            )

            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(UPDATE_CASE_NUMBER, (case_number, str(case_id)))

            stats["updated"] += 1

    if dry_run:
        conn.rollback()
        logger.info("Phase 1 dry run — rolled back")
    else:
        conn.commit()
        logger.info(
            "Phase 1 committed: %d updated, %d merged, %d orphans deleted, %d no number found",
            stats["updated"],
            stats["merged"],
            stats["deleted_orphans"],
            stats["no_number"],
        )

    return stats


# ---------------------------------------------------------------------------
# Phase 2: Fix contaminated titles
# ---------------------------------------------------------------------------

FETCH_CONTAMINATED_TITLES = """
    SELECT c.id, c.case_title, r.ruling_text
    FROM cases c
    LEFT JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.court_id = %s
      AND c.case_title ILIKE '%%Before the court%%'
    ORDER BY c.id
"""

UPDATE_TITLE = """
    UPDATE cases
    SET case_title = %s, updated_at = NOW()
    WHERE id = %s
"""


def _fix_contaminated_titles(
    conn: psycopg.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Fix case titles that contain ruling text fragments."""
    stats = {
        "total": 0,
        "cleaned": 0,
        "from_ruling": 0,
        "skipped": 0,
    }

    with conn.cursor() as cur:
        cur.execute(FETCH_CONTAMINATED_TITLES, (OC_COURT_ID,))
        rows = cur.fetchall()

    logger.info("Found %d cases with contaminated titles", len(rows))

    for case_id, case_title, ruling_text in rows:
        stats["total"] += 1

        # Try to clean the title by removing trailing contamination
        new_title = _clean_title(case_title)

        # If that didn't work, try extracting from ruling text
        if new_title is None and ruling_text:
            new_title = _extract_title_from_ruling(ruling_text)
            if new_title:
                stats["from_ruling"] += 1

        # Last resort: extract plaintiff from title + defendant from ruling
        if new_title is None and ruling_text:
            plaintiff_match = re.match(
                r"^([A-Z][A-Za-z,.'&\-\s]{1,60}?)\s+(?:vs?\.?)\s",
                case_title,
                re.IGNORECASE,
            )
            if plaintiff_match:
                plaintiff = plaintiff_match.group(1).strip()
                defendant = _extract_defendant_from_ruling(ruling_text)
                if defendant:
                    new_title = f"{plaintiff} v. {defendant}"
                    stats["from_ruling"] += 1

        if new_title is None:
            logger.warning(
                "Could not clean title for case %s: '%s'",
                case_id,
                case_title,
            )
            stats["skipped"] += 1
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

        stats["cleaned"] += 1

    if dry_run:
        conn.rollback()
        logger.info("Phase 2 dry run — rolled back")
    else:
        conn.commit()
        logger.info(
            "Phase 2 committed: %d cleaned (%d from ruling text), %d skipped",
            stats["cleaned"],
            stats["from_ruling"],
            stats["skipped"],
        )

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean up OC data: UNKNOWN case numbers + contaminated titles (#1929).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing to the database.",
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[1, 2],
        default=None,
        help="Run only this phase (1=case numbers, 2=titles). Default: both.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    with psycopg.connect(dsn) as conn:
        if args.phase is None or args.phase == 1:
            logger.info("=== Phase 1: Fix UNKNOWN case numbers ===")
            phase1_stats = _fix_unknown_case_numbers(conn, dry_run=args.dry_run)
            logger.info("Phase 1 results: %s", phase1_stats)

        if args.phase is None or args.phase == 2:
            logger.info("=== Phase 2: Fix contaminated titles ===")
            phase2_stats = _fix_contaminated_titles(conn, dry_run=args.dry_run)
            logger.info("Phase 2 results: %s", phase2_stats)

    logger.info("Cleanup complete.")


if __name__ == "__main__":
    main()
