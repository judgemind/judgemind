#!/usr/bin/env python3
"""One-time backfill: normalize un-normalized motion_type values in the rulings table.

Some scrapers (San Diego calendar, San Diego tentatives, Riverside) stored
motion_type in title case or as raw calendar event names instead of the
canonical snake_case format (e.g. "Motion Hearing" instead of "motion_to_compel").

This script:
1. Finds all distinct un-normalized motion_type values (containing uppercase)
2. Maps each to its normalized snake_case equivalent using the same regex
   patterns used by the enrichment pipeline
3. Updates the rulings table
4. Re-derives case_type for cases with NULL case_type where the newly-normalized
   motion_type maps to a case type

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_normalize_motion_type.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_normalize_motion_type.py

Issue: #1712
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
# Normalization mapping — inlined from ingestion/extract.py to satisfy
# the ECS oneshot constraint (single file, no local imports).
# ---------------------------------------------------------------------------

# Regex patterns for matching motion types, ordered so more specific
# patterns match first.  Each tuple is (pattern, snake_case_value).
_MOTION_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(?:motion\s+for\s+)?summary\s+adjudication\b", re.I),
        "msj_partial",
    ),
    (re.compile(r"\bpartial\s+summary\s+judgment\b", re.I), "msj_partial"),
    (re.compile(r"\b(?:motion\s+for\s+)?summary\s+judgment\b", re.I), "msj"),
    (re.compile(r"\bmotion\s+to\s+dismiss\b", re.I), "mtd"),
    (re.compile(r"\bmotion\s+in\s+limine\b", re.I), "mil"),
    (re.compile(r"\bdemurrer\b", re.I), "demurrer"),
    (re.compile(r"\bmotions?\s+to\s+compel\b", re.I), "motion_to_compel"),
    (re.compile(r"\banti[- ]?slapp\b", re.I), "anti_slapp"),
    (re.compile(r"\bmotion\s+to\s+strike\b", re.I), "motion_to_strike"),
    (re.compile(r"\bpreliminary\s+injunction\b", re.I), "preliminary_injunction"),
    (re.compile(r"\bex\s+parte\s+application\b", re.I), "ex_parte_application"),
    (re.compile(r"\bex\s+parte\s+motion\b", re.I), "ex_parte_application"),
    (
        re.compile(r"\bpetition\s+for\s+writ\s+of\s+(?:mandate|mandamus)\b", re.I),
        "petition_writ_of_mandate",
    ),
    (
        re.compile(r"\bpetition\s+for\s+writ\s+of\s+habeas\s+corpus\b", re.I),
        "petition_habeas_corpus",
    ),
    (
        re.compile(r"\bclass\s+action\s+settlement\b|\bpreliminary\s+approval\b", re.I),
        "class_action_settlement",
    ),
    (re.compile(r"\bpetition\b", re.I), "petition"),
    (re.compile(r"\border\s+to\s+show\s+cause\b", re.I), "osc"),
    (re.compile(r"\bmotion\s+to\s+quash\b", re.I), "motion_to_quash"),
    (
        re.compile(r"\bmotion\s+for\s+reconsideration\b", re.I),
        "motion_for_reconsideration",
    ),
    (
        re.compile(r"\bmotion\s+for\s+protective\s+order\b", re.I),
        "motion_for_protective_order",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+attorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees\b",
            re.I,
        ),
        "motion_for_attorney_fees",
    ),
    (
        re.compile(r"\bmotion\s+to\s+set\s+aside\s+(?:the\s+)?default\b", re.I),
        "motion_to_set_aside_default",
    ),
    (re.compile(r"\bmotion\s+to\s+vacate\b", re.I), "motion_to_vacate"),
    (re.compile(r"\bdefault\s+judgment\b", re.I), "default_judgment"),
    (
        re.compile(r"\bto\s+be\s+relieved\s+as\s+counsel\b", re.I),
        "motion_to_be_relieved_as_counsel",
    ),
    (re.compile(r"\bmotion\s+for\s+leave\b", re.I), "motion_for_leave_to_amend"),
    (re.compile(r"\bmotion\s+for\s+sanctions\b", re.I), "motion_for_sanctions"),
    (re.compile(r"\bmotion\s+for\s+relief\b", re.I), "motion_for_relief"),
    (re.compile(r"\bmotion\s+for\s+pro\s+hac\s+vice\b", re.I), "motion_pro_hac_vice"),
    (re.compile(r"\bmotion\s+to\s+substitute\b", re.I), "motion_to_substitute"),
    (re.compile(r"\bMILs?\b"), "mil"),
    (re.compile(r"\bmotion\s+to\s+tax\s+costs\b", re.I), "motion_to_tax_costs"),
    (re.compile(r"\bwrit\s+of\s+possession\b", re.I), "writ_of_possession"),
    (re.compile(r"\bmotion\s+for\s+new\s+trial\b", re.I), "motion_for_new_trial"),
    (re.compile(r"\bex\s+parte\b", re.I), "ex_parte_application"),
]

_KNOWN_MOTION_TYPES: frozenset[str] = frozenset(v for _, v in _MOTION_TYPE_PATTERNS)

# Maps normalized motion_type to case_type (same as extract.py).
_MOTION_TYPE_CASE_TYPE_MAP: dict[str, str] = {
    "msj": "civil",
    "msj_partial": "civil",
    "mtd": "civil",
    "demurrer": "civil",
    "motion_to_compel": "civil",
    "anti_slapp": "civil",
    "motion_to_strike": "civil",
    "preliminary_injunction": "civil",
    "motion_for_protective_order": "civil",
    "motion_for_attorney_fees": "civil",
    "motion_to_set_aside_default": "civil",
    "motion_to_vacate": "civil",
    "default_judgment": "civil",
    "motion_to_be_relieved_as_counsel": "civil",
    "motion_for_leave_to_amend": "civil",
    "motion_for_sanctions": "civil",
    "motion_for_relief": "civil",
    "motion_pro_hac_vice": "civil",
    "motion_to_substitute": "civil",
    "motion_to_tax_costs": "civil",
    "motion_for_new_trial": "civil",
    "motion_for_reconsideration": "civil",
    "motion_to_quash": "civil",
    "class_action_settlement": "civil",
    "writ_of_possession": "civil",
    "mil": "civil",
    "petition": "probate",
}


def normalize_motion_type(raw: str) -> str | None:
    """Normalize a raw motion type string to snake_case.

    Returns the normalized value, or None if unmappable.
    """
    if not raw:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped in _KNOWN_MOTION_TYPES:
        return stripped
    for pattern, value in _MOTION_TYPE_PATTERNS:
        if pattern.search(stripped):
            return value
    return None


def run_backfill(conn: psycopg.Connection, *, dry_run: bool = True) -> dict[str, int]:
    """Normalize motion_type values and re-derive case_type where possible.

    Returns a dict with counts: normalized, set_to_null, case_type_updated.
    """
    stats: dict[str, int] = {"normalized": 0, "set_to_null": 0, "case_type_updated": 0}

    with conn.cursor() as cur:
        # Step 1: Find all distinct un-normalized motion_type values.
        # Un-normalized values contain uppercase letters beyond the first
        # character (snake_case values like "msj" or "osc" are fine).
        cur.execute(
            """
            SELECT DISTINCT motion_type
            FROM rulings
            WHERE motion_type IS NOT NULL
              AND motion_type ~ '[A-Z].*[A-Z]'
            ORDER BY motion_type
            """
        )
        un_normalized = [row[0] for row in cur.fetchall()]
        logger.info(
            "Found %d distinct un-normalized motion_type values", len(un_normalized)
        )

        for raw_value in un_normalized:
            normalized = normalize_motion_type(raw_value)
            if normalized == raw_value:
                continue  # Already normalized

            cur.execute(
                "SELECT COUNT(*) FROM rulings WHERE motion_type = %s",
                (raw_value,),
            )
            count = cur.fetchone()[0]

            if normalized is not None:
                logger.info(
                    "%s motion_type %r -> %r (%d rulings)",
                    "Would normalize" if dry_run else "Normalizing",
                    raw_value,
                    normalized,
                    count,
                )
                if not dry_run:
                    cur.execute(
                        "UPDATE rulings SET motion_type = %s WHERE motion_type = %s",
                        (normalized, raw_value),
                    )
                stats["normalized"] += count
            else:
                logger.info(
                    "%s motion_type %r -> NULL (%d rulings)",
                    "Would set" if dry_run else "Setting",
                    raw_value,
                    count,
                )
                if not dry_run:
                    cur.execute(
                        "UPDATE rulings SET motion_type = NULL WHERE motion_type = %s",
                        (raw_value,),
                    )
                stats["set_to_null"] += count

        # Step 2: Re-derive case_type for cases with NULL case_type where
        # motion_type now maps to a known case type.
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
        null_case_type_rows = cur.fetchall()
        logger.info(
            "Found %d cases with NULL case_type and non-NULL motion_type",
            len(null_case_type_rows),
        )

        # Group by case_id to handle cases with multiple rulings that
        # might map to different case types.  Only update when all rulings
        # for a case agree on the same inferred type.
        case_inferred: dict[str, dict[str, set[str]]] = {}
        case_meta: dict[str, tuple[str, str]] = {}  # case_id -> (case_number, county)
        for case_id, case_number, county, motion_type in null_case_type_rows:
            inferred = _MOTION_TYPE_CASE_TYPE_MAP.get(motion_type)
            if inferred is None:
                continue
            if case_id not in case_inferred:
                case_inferred[case_id] = {"types": set()}
                case_meta[case_id] = (case_number, county)
            case_inferred[case_id]["types"].add(inferred)

        for case_id, info in case_inferred.items():
            inferred_types = info["types"]
            case_number, county = case_meta[case_id]
            if len(inferred_types) > 1:
                logger.warning(
                    "Skipping case_number=%s (county=%s, case_id=%s) — "
                    "conflicting inferred case_types: %s",
                    case_number,
                    county,
                    case_id,
                    inferred_types,
                )
                continue

            inferred = next(iter(inferred_types))
            logger.info(
                "%s case_type=%s for case_number=%s (county=%s)",
                "Would set" if dry_run else "Setting",
                inferred,
                case_number,
                county,
            )
            if not dry_run:
                cur.execute(
                    "UPDATE cases SET case_type = %s WHERE id = %s",
                    (inferred, case_id),
                )
            stats["case_type_updated"] += 1

    if dry_run:
        conn.rollback()
        logger.info("DRY RUN -- no changes committed.")
    else:
        conn.commit()
        logger.info("Changes committed.")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize un-normalized motion_type values in the rulings table (#1712)"
    )
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
        "Backfill complete (%s): %d normalized, %d set to NULL, %d case_type derived",
        mode,
        stats["normalized"],
        stats["set_to_null"],
        stats["case_type_updated"],
    )


if __name__ == "__main__":
    main()
