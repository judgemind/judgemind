#!/usr/bin/env python3
"""Backfill parties, case_type, and fix garbled case titles for OC split documents.

Targets Orange County documents created on or after 2026-03-20 by the document
splitting backfill (#1174).  These split documents have case titles and hearing
dates but are missing case_parties and case_type.  Some also have garbled case
titles where ruling text leaked into the title.

Three fixes in one script:
  1. Clean garbled case titles (strip ruling/motion text after the case name).
  2. Extract case_type from OC case number prefixes (e.g. 30-2024-... -> civil).
  3. Extract parties from the (cleaned) case title caption.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_oc_split_fields.py
    scripts/ecs-run.sh --script scripts/backfill_oc_split_fields.py -- --dry-run
    scripts/ecs-run.sh --script scripts/backfill_oc_split_fields.py -- --limit 10
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

# ---------------------------------------------------------------------------
# OC court UUID (constant across environments)
# ---------------------------------------------------------------------------
OC_COURT_ID = "785660ca-0169-431c-8cab-34c55ca4d3b0"

# ---------------------------------------------------------------------------
# Case title cleanup patterns — inline since this is an ECS oneshot script
# ---------------------------------------------------------------------------

# Ruling outcome phrases that leak into case titles.
# Two-part: "is GRANTED" (case-insensitive), or standalone ALL-CAPS keywords
# (case-sensitive to avoid matching names like "Grant").
_RULING_OUTCOME_RE = re.compile(
    r"\b(?:is|are|was|were|hereby)\s+(?:GRANTED|DENIED|SUSTAINED|OVERRULED|"
    r"CONTINUED|VACATED|MOOT|AFFIRMED|DISMISSED)\b.*",
    re.IGNORECASE,
)
_RULING_KEYWORD_STANDALONE_RE = re.compile(
    r"\b(?:GRANTED|DENIED|SUSTAINED|OVERRULED|CONTINUED|VACATED|MOOT|AFFIRMED|DISMISSED)\b.*"
)

# Possessive role labels (e.g. "Plaintiff's", "Defendant's") at end of title.
_POSSESSIVE_ROLE_RE = re.compile(
    r"\s+(?:Plaintiffs?|Defendants?|Petitioners?|Respondents?|"
    r"Cross-\s*(?:Complainants?|Defendants?))"
    r"(?:\u2019s|'s|\(s\))?\s*$",
    re.IGNORECASE,
)

# Motion description phrases not caught by standard motion keywords.
_MOTION_PHRASE_RE = re.compile(
    r"\s+(?:for\s+(?:Attorney|Summary|Leave|Relief|Approval|Default)|"
    r"to\s+(?:Compel|Set\s+Aside|Strike|Dismiss|Quash|Vacate|Tax)|"
    r"of\s+(?:Class\s+Action|Default|Settlement)|"
    r"settlement\s+funds)\b.*",
    re.IGNORECASE,
)

# Standard motion keyword prefixes.
_MOTION_KEYWORDS = (
    "Motion for",
    "Motion to",
    "Demurrer",
    "OSC",
    "Application",
    "Petition",
    "Status Conference",
    "Case Management Conference",
    "CMC REMAINS",
    "OFF CALENDAR",
    "HEARING",
)

# OC case number patterns for case_type extraction.
_CASE_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{2,4}-\d{4,8}"), "civil"),
]


def clean_case_title(raw_title: str) -> str:
    """Strip ruling text, motion descriptions, and junk from a case title."""
    # First truncate at motion keywords.
    title = raw_title
    for kw in _MOTION_KEYWORDS:
        pos = title.find(kw)
        if pos > 0:
            title = title[:pos].strip()
            break

    # Truncate at ruling outcome phrases ("is GRANTED", case-insensitive).
    title = _RULING_OUTCOME_RE.sub("", title).strip()

    # Truncate at standalone ALL-CAPS ruling keywords ("CONTINUED TO ...").
    title = _RULING_KEYWORD_STANDALONE_RE.sub("", title).strip()

    # Truncate at possessive role labels.
    title = _POSSESSIVE_ROLE_RE.sub("", title).strip()

    # Truncate at motion description phrases.
    title = _MOTION_PHRASE_RE.sub("", title).strip()

    # Remove trailing punctuation.
    title = title.rstrip(".,;:() ")

    return title


def extract_case_type_from_number(case_number: str) -> str | None:
    """Infer case type from an OC case number prefix.

    OC case numbers like 30-2024-01393434 or 2024-01380242 are civil.
    """
    if not case_number:
        return None
    case_number = case_number.strip()
    for pattern, case_type in _CASE_TYPE_PATTERNS:
        if pattern.match(case_number):
            return case_type
    return None


# Caption regex: "Plaintiff vs. Defendant" or "Plaintiff vs Defendant"
_CAPTION_VS_RE = re.compile(
    r"^(?P<plaintiff>.+?)\s+(?:vs\.?|v\.)\s+(?P<defendant>.+)$",
    re.IGNORECASE,
)


def extract_parties_from_title(case_title: str) -> list[dict[str, str]]:
    """Extract party names from a case title like 'Smith vs. Jones'."""
    if not case_title:
        return []
    m = _CAPTION_VS_RE.match(case_title.strip())
    if not m:
        return []

    parties: list[dict[str, str]] = []
    plaintiff = m.group("plaintiff").strip().rstrip(",.")
    defendant = m.group("defendant").strip().rstrip(",.")

    if plaintiff:
        parties.append({"name": plaintiff.title(), "role": "plaintiff"})
    if defendant:
        parties.append({"name": defendant.title(), "role": "defendant"})

    return parties


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

FETCH_CASES_QUERY = """
    SELECT DISTINCT c.id, c.case_title, c.case_number, c.case_type,
           EXISTS (SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id) AS has_parties
    FROM cases c
    JOIN documents d ON d.case_id = c.id
    WHERE d.court_id = %s::uuid
      AND d.status = 'active'
      AND d.created_at >= '2026-03-20'
    ORDER BY c.id
"""


def _normalize_party_name(raw_name: str) -> str:
    """Normalize a raw party name for the canonical_name column."""
    name = raw_name.strip()
    name = re.sub(r"\s+", " ", name)
    return name.title()


def _upsert_party_and_link(
    conn: psycopg.Connection,
    case_id: str,
    name: str,
    role: str,
) -> None:
    """Upsert a party record and link it to a case."""
    canonical = _normalize_party_name(name)

    with conn.cursor() as cur:
        # Check for existing alias
        cur.execute(
            "SELECT pa.party_id FROM party_aliases pa WHERE pa.raw_name = %s LIMIT 1",
            (name,),
        )
        row = cur.fetchone()

        if row is not None:
            party_id = str(row[0])
        else:
            # Create new party
            cur.execute(
                "INSERT INTO parties (canonical_name) VALUES (%s) RETURNING id",
                (canonical,),
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("Failed to insert party: %s", canonical)
                return
            party_id = str(row[0])

            # Create alias
            cur.execute(
                """INSERT INTO party_aliases (party_id, raw_name, source, confidence, is_verified)
                   VALUES (%s::uuid, %s, 'backfill', 1.0, FALSE)
                   ON CONFLICT DO NOTHING""",
                (party_id, name),
            )

        # Link party to case
        cur.execute(
            """INSERT INTO case_parties (case_id, party_id, role)
               VALUES (%s::uuid, %s::uuid, %s)
               ON CONFLICT (case_id, party_id, role) DO NOTHING""",
            (case_id, party_id, role),
        )


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


def run_backfill(
    dsn: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Run the backfill.  Returns summary stats."""
    stats = {
        "total_cases": 0,
        "titles_cleaned": 0,
        "case_types_set": 0,
        "parties_added": 0,
    }

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(FETCH_CASES_QUERY, (OC_COURT_ID,))
            rows = cur.fetchall()

        logger.info("Found %d OC split cases to process", len(rows))

        for case_id, case_title, case_number, case_type, has_parties in rows:
            if limit is not None and stats["total_cases"] >= limit:
                break

            stats["total_cases"] += 1
            case_id_str = str(case_id)

            # 1. Fix garbled case title
            new_title = None
            if case_title:
                cleaned = clean_case_title(case_title)
                if cleaned != case_title:
                    new_title = cleaned
                    stats["titles_cleaned"] += 1
                    logger.info(
                        "Cleaning title: %r -> %r (case %s)",
                        case_title,
                        cleaned,
                        case_id_str,
                    )

            # 2. Extract case_type from case number if missing
            new_case_type = None
            if case_type is None and case_number:
                new_case_type = extract_case_type_from_number(case_number)
                if new_case_type:
                    stats["case_types_set"] += 1

            # 3. Extract parties from (cleaned) case title if missing
            effective_title = new_title or case_title
            new_parties: list[dict[str, str]] = []
            if not has_parties and effective_title:
                new_parties = extract_parties_from_title(effective_title)
                if new_parties:
                    stats["parties_added"] += 1

            # Apply updates
            if not dry_run:
                if new_title or new_case_type:
                    with conn.cursor() as cur:
                        if new_title and new_case_type:
                            cur.execute(
                                "UPDATE cases SET case_title = %s, case_type = %s WHERE id = %s::uuid",
                                (new_title, new_case_type, case_id_str),
                            )
                        elif new_title:
                            cur.execute(
                                "UPDATE cases SET case_title = %s WHERE id = %s::uuid",
                                (new_title, case_id_str),
                            )
                        elif new_case_type:
                            cur.execute(
                                "UPDATE cases SET case_type = %s WHERE id = %s::uuid",
                                (new_case_type, case_id_str),
                            )

                for party in new_parties:
                    _upsert_party_and_link(
                        conn, case_id_str, party["name"], party["role"]
                    )

            if stats["total_cases"] % 100 == 0:
                if not dry_run:
                    conn.commit()
                logger.info("Progress: %d cases processed", stats["total_cases"])

        if not dry_run:
            conn.commit()

    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill parties, case_type, and fix garbled titles for OC split documents.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum total cases to process.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    stats = run_backfill(
        dsn,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    logger.info(
        "Backfill complete: %d cases processed, %d titles cleaned, "
        "%d case_types set, %d cases with parties added%s",
        stats["total_cases"],
        stats["titles_cleaned"],
        stats["case_types_set"],
        stats["parties_added"],
        " [dry-run]" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
