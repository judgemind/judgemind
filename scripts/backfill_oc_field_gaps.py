#!/usr/bin/env python3
"""Backfill missing case_type, parties, and fix case-name-as-case-number for OC.

Fixes data quality regressions (#1524) where:
  1. case_number contains a case title (e.g. "Smith v. Kia") instead of a
     real case number — sets case_title from it and replaces with UNKNOWN-{doc_id}
  2. case_type is NULL — sets to 'civil' (all OC tentative rulings are civil)
  3. parties are missing — extracts from case_title using caption parsing

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_oc_field_gaps.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_oc_field_gaps.py

Usage (local with DB access):
    scripts/with-secret.sh -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- python3 scripts/backfill_oc_field_gaps.py --dry-run
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

# Pattern to detect case titles stored as case numbers.
_VS_RE = re.compile(r"\bvs?\.?\s", re.IGNORECASE)

# Pattern to detect legal citations stored as case numbers.
_CITATION_RE = re.compile(
    r"\bCal\.\s*App\b|\bF\.\s*Supp\b|\bF\.\s*\d",
    re.IGNORECASE,
)

# Caption regex: "Plaintiff v. Defendant"
_CAPTION_VS_RE = re.compile(
    r"^(?P<plaintiff_side>.+?)"
    r"\s+v[Ss]?\.?\s+"
    r"(?P<defendant_side>.+)$",
)

# Corporate suffixes that must not be split from the preceding name.
_CORPORATE_SUFFIX_RE = re.compile(
    r"^(?:Inc|LLC|Corp|Corporation|Ltd|L\.?P\.?|N\.?A\.?"
    r"|Co|PA|PC|PLLC|LLP|DBA|GP)\.?$",
    re.IGNORECASE,
)

# Suffixes to strip (et al.)
_ET_AL_RE = re.compile(r",?\s*et\s+al\.?\s*$", re.IGNORECASE)


def _split_caption_names(text: str) -> list[str]:
    """Split a caption side into individual party names."""
    text = _ET_AL_RE.sub("", text).strip()
    if not text:
        return []
    raw_parts = re.split(r",\s+and\s+|,\s+", text)
    if len(raw_parts) == 1:
        raw_parts = re.split(r"\s+and\s+", text)
    parts: list[str] = []
    for fragment in raw_parts:
        fragment = fragment.strip().strip(")(,.; ")
        if not fragment:
            continue
        if parts and _CORPORATE_SUFFIX_RE.match(fragment):
            parts[-1] = f"{parts[-1]}, {fragment}"
        else:
            parts.append(fragment)
    return [p for p in parts if len(p) >= 2]


def extract_parties(case_title: str) -> list[dict[str, str]]:
    """Extract parties from a case title caption."""
    if not case_title:
        return []
    m = _CAPTION_VS_RE.match(case_title.strip())
    if m is None:
        return []
    parties: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for side, role in [
        (m.group("plaintiff_side"), "plaintiff"),
        (m.group("defendant_side"), "defendant"),
    ]:
        names = _split_caption_names(side)
        for name in names:
            name = name.strip().rstrip(".,;: ")
            if not name or len(name) < 2:
                continue
            key = (name.lower(), role)
            if key in seen:
                continue
            seen.add(key)
            parties.append({"name": name, "role": role})
    return parties


def _is_case_name_as_number(case_number: str) -> bool:
    """Return True if case_number is actually a case title."""
    if _VS_RE.search(case_number):
        return True
    if _CITATION_RE.search(case_number):
        return True
    # Reject very long "case numbers" (real ones are < 25 chars)
    if len(case_number.strip()) > 30:
        return True
    return False


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> dict[str, int]:
    """Run the backfill. Returns counts of fixed records."""
    stats = {
        "case_number_fixed": 0,
        "case_type_set": 0,
        "parties_added": 0,
        "docs_examined": 0,
    }

    # Find all OC documents with gaps.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT d.id AS doc_id,
                   c.id AS case_id,
                   c.case_number,
                   c.case_title,
                   c.case_type
            FROM documents d
            JOIN courts ct ON ct.id = d.court_id
            LEFT JOIN cases c ON c.id = d.case_id
            WHERE d.status = 'active'
              AND ct.county = 'Orange'
              AND d.created_at >= NOW() - INTERVAL '14 days'
              AND (
                  c.case_type IS NULL
                  OR NOT EXISTS (
                      SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id
                  )
              )
        """)
        rows = cur.fetchall()

    logger.info("Found %d OC documents with field gaps", len(rows))
    stats["docs_examined"] = len(rows)

    for doc_id, case_id, case_number, case_title, case_type in rows:
        if not case_id:
            continue

        # 1. Fix case_number that is actually a case title.
        if case_number and _is_case_name_as_number(case_number):
            new_case_number = f"UNKNOWN-{doc_id}"
            effective_title = case_number if not case_title else case_title
            logger.info(
                "Fixing case_number: %r -> %r (title: %r)",
                case_number[:60],
                new_case_number,
                effective_title[:60],
            )
            if not dry_run:
                normalized = (
                    new_case_number.strip().lower().replace(" ", "").replace("-", "")
                )
                with conn.cursor() as cur:
                    # Check if UNKNOWN-{doc_id} case already exists.
                    cur.execute(
                        "SELECT id FROM cases WHERE case_number = %s AND court_id = ("
                        "  SELECT court_id FROM cases WHERE id = %s::uuid"
                        ")",
                        (new_case_number, case_id),
                    )
                    existing = cur.fetchone()
                    if existing:
                        # Already exists — skip to avoid conflict
                        logger.info("UNKNOWN case already exists, skipping rename")
                    else:
                        cur.execute(
                            "UPDATE cases SET case_number = %s, "
                            "case_number_normalized = %s, "
                            "case_title = COALESCE(case_title, %s) "
                            "WHERE id = %s::uuid",
                            (new_case_number, normalized, effective_title, case_id),
                        )
                        stats["case_number_fixed"] += 1
            else:
                stats["case_number_fixed"] += 1

            # After renaming, case_number no longer has type info
            case_number = new_case_number

        # 2. Set case_type = 'civil' if missing.
        if case_type is None:
            logger.info(
                "Setting case_type=civil for case %s (doc %s)",
                case_id,
                doc_id,
            )
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_type = 'civil' WHERE id = %s::uuid "
                        "AND case_type IS NULL",
                        (case_id,),
                    )
            stats["case_type_set"] += 1

        # 3. Add parties if missing.
        with conn.cursor() as check_cur:
            check_cur.execute(
                "SELECT EXISTS (SELECT 1 FROM case_parties cp WHERE cp.case_id = %s::uuid)",
                (case_id,),
            )
            has_parties = check_cur.fetchone()[0]

        if not has_parties:
            effective_title = case_title or case_number
            if effective_title:
                parties = extract_parties(effective_title)
                if parties:
                    logger.info(
                        "Adding %d parties for case %s from title %r",
                        len(parties),
                        case_id,
                        effective_title[:60],
                    )
                    if not dry_run:
                        for party in parties:
                            with conn.cursor() as cur:
                                # Upsert party
                                cur.execute(
                                    """
                                    INSERT INTO parties (canonical_name, party_type)
                                    VALUES (%s, 'unknown')
                                    ON CONFLICT (canonical_name) DO UPDATE
                                        SET updated_at = NOW()
                                    RETURNING id
                                    """,
                                    (party["name"],),
                                )
                                party_row = cur.fetchone()
                                if party_row:
                                    party_id = str(party_row[0])
                                    # Link party to case
                                    cur.execute(
                                        """
                                        INSERT INTO case_parties (case_id, party_id, role)
                                        VALUES (%s::uuid, %s::uuid, %s)
                                        ON CONFLICT (case_id, party_id, role) DO NOTHING
                                        """,
                                        (case_id, party_id, party["role"]),
                                    )
                                    # Add alias
                                    cur.execute(
                                        """
                                        INSERT INTO party_aliases (party_id, raw_name, source,
                                                                   confidence, is_verified)
                                        VALUES (%s::uuid, %s, 'backfill', 1.0, FALSE)
                                        ON CONFLICT DO NOTHING
                                        """,
                                        (party_id, party["name"]),
                                    )
                    stats["parties_added"] += len(parties)

    if not dry_run:
        conn.commit()
    else:
        conn.rollback()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OC field gaps (#1524)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        stats = run_backfill(conn, dry_run=args.dry_run)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    logger.info(
        "Backfill complete (%s): examined=%d, case_numbers_fixed=%d, "
        "case_types_set=%d, parties_added=%d",
        mode,
        stats["docs_examined"],
        stats["case_number_fixed"],
        stats["case_type_set"],
        stats["parties_added"],
    )


if __name__ == "__main__":
    main()
