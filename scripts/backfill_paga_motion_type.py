#!/usr/bin/env python3
"""Backfill motion_type for the PAGA settlement ruling.

Reads the ruling text from the database for the specified ruling ID,
applies extract_motion_type() to derive the motion type, and updates
the record.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_paga_motion_type.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_paga_motion_type.py

Issue: #1818
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
# Motion type extraction — inlined from ingestion/extract.py to satisfy the
# ECS oneshot constraint (single file, no local imports).
# ---------------------------------------------------------------------------

_MOTION_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(?:motion\s+for\s+)?summary\s+adjudication\b", re.IGNORECASE),
        "msj_partial",
    ),
    (re.compile(r"\bpartial\s+summary\s+judgment\b", re.IGNORECASE), "msj_partial"),
    (
        re.compile(r"\b(?:motion\s+for\s+)?summary\s+judgment\b", re.IGNORECASE),
        "msj",
    ),
    (re.compile(r"\bmotion\s+to\s+dismiss\b", re.IGNORECASE), "mtd"),
    (re.compile(r"\bmotion\s+in\s+limine\b", re.IGNORECASE), "mil"),
    (re.compile(r"\bdemurrer\b", re.IGNORECASE), "demurrer"),
    (re.compile(r"\bmotions?\s+to\s+compel\b", re.IGNORECASE), "motion_to_compel"),
    (re.compile(r"\banti[- ]?slapp\b", re.IGNORECASE), "anti_slapp"),
    (re.compile(r"\bmotion\s+to\s+strike\b", re.IGNORECASE), "motion_to_strike"),
    (
        re.compile(r"\bpreliminary\s+injunction\b", re.IGNORECASE),
        "preliminary_injunction",
    ),
    (
        re.compile(r"\bex\s+parte\s+application\b", re.IGNORECASE),
        "ex_parte_application",
    ),
    (re.compile(r"\bex\s+parte\s+motion\b", re.IGNORECASE), "ex_parte_application"),
    (
        re.compile(
            r"\bpetition\s+for\s+writ\s+of\s+(?:mandate|mandamus)\b", re.IGNORECASE
        ),
        "petition_writ_of_mandate",
    ),
    (
        re.compile(r"\bpetition\s+for\s+writ\s+of\s+habeas\s+corpus\b", re.IGNORECASE),
        "petition_habeas_corpus",
    ),
    (
        re.compile(
            r"\bclass\s+action\s+settlement\b|\bpreliminary\s+approval\b",
            re.IGNORECASE,
        ),
        "class_action_settlement",
    ),
    (
        re.compile(r"\bPAGA\s+settlement\b|\bapproval\s+of\s+PAGA\b", re.IGNORECASE),
        "paga_settlement",
    ),
    (
        re.compile(
            r"\bsettlement\s+(?:agreement|approval|hearing)\b"
            r"|\bapproval\s+of\s+(?:\w+\s+)*?settlement\b",
            re.IGNORECASE,
        ),
        "settlement_approval",
    ),
    (
        re.compile(
            r"\bpetition\s+(?:for\s+)?(?:probate|to\s+administer\s+estate"
            r"|for\s+letters)\b",
            re.IGNORECASE,
        ),
        "petition_for_probate",
    ),
    (
        re.compile(
            r"\b(?:guardianship\s+petition|petition\s+for\s+"
            r"(?:guardianship|conservatorship))\b",
            re.IGNORECASE,
        ),
        "guardianship_petition",
    ),
    (re.compile(r"\baccounting\b", re.IGNORECASE), "accounting"),
    (re.compile(r"\bshow\s+cause\s+hearing\b", re.IGNORECASE), "show_cause_hearing"),
    (re.compile(r"\btrust\s+petition\b", re.IGNORECASE), "trust_petition"),
    (re.compile(r"\bpetition\b", re.IGNORECASE), "petition"),
    (re.compile(r"\border\s+to\s+show\s+cause\b", re.IGNORECASE), "osc"),
    (re.compile(r"\bmotion\s+to\s+quash\b", re.IGNORECASE), "motion_to_quash"),
    (
        re.compile(r"\bmotion\s+for\s+reconsideration\b", re.IGNORECASE),
        "motion_for_reconsideration",
    ),
    (
        re.compile(r"\bmotion\s+for\s+protective\s+order\b", re.IGNORECASE),
        "motion_for_protective_order",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+attorney['\u2018\u2019]?s?['\u2018\u2019]?\s*fees\b",
            re.IGNORECASE,
        ),
        "motion_for_attorney_fees",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+set\s+aside\s+(?:the\s+)?default\b", re.IGNORECASE
        ),
        "motion_to_set_aside_default",
    ),
    (re.compile(r"\bmotion\s+to\s+vacate\b", re.IGNORECASE), "motion_to_vacate"),
    (re.compile(r"\bdefault\s+judgment\b", re.IGNORECASE), "default_judgment"),
    (
        re.compile(r"\bto\s+be\s+relieved\s+as\s+counsel\b", re.IGNORECASE),
        "motion_to_be_relieved_as_counsel",
    ),
    (
        re.compile(r"\bmotion\s+for\s+leave\b", re.IGNORECASE),
        "motion_for_leave_to_amend",
    ),
    (
        re.compile(r"\bmotion\s+for\s+sanctions\b", re.IGNORECASE),
        "motion_for_sanctions",
    ),
    (re.compile(r"\bmotion\s+for\s+relief\b", re.IGNORECASE), "motion_for_relief"),
    (
        re.compile(r"\bmotion\s+for\s+pro\s+hac\s+vice\b", re.IGNORECASE),
        "motion_pro_hac_vice",
    ),
    (
        re.compile(r"\bmotion\s+to\s+substitute\b", re.IGNORECASE),
        "motion_to_substitute",
    ),
    (re.compile(r"\bMILs?\b"), "mil"),
    (
        re.compile(r"\bmotion\s+to\s+tax\s+costs\b", re.IGNORECASE),
        "motion_to_tax_costs",
    ),
    (re.compile(r"\bwrit\s+of\s+possession\b", re.IGNORECASE), "writ_of_possession"),
    (
        re.compile(r"\bmotion\s+for\s+new\s+trial\b", re.IGNORECASE),
        "motion_for_new_trial",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+judgment\s+on\s+the\s+pleadings\b", re.IGNORECASE
        ),
        "motion_for_judgment_on_the_pleadings",
    ),
    (
        re.compile(
            r"\bmotion\s+to\s+deem\b.*\badmissions?\s+admitted\b", re.IGNORECASE
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(r"\bmotion\s+to\s+deem\s+requests?\b", re.IGNORECASE),
        "deem_admissions_admitted",
    ),
    (re.compile(r"\bex\s+parte\b", re.IGNORECASE), "ex_parte_application"),
]


def extract_motion_type(ruling_text: str) -> str | None:
    """Extract a motion type from text using regex patterns."""
    for pattern, value in _MOTION_TYPE_PATTERNS:
        if pattern.search(ruling_text):
            return value
    return None


# Target ruling ID from issue #1818
RULING_ID = "fdf1c80c-cf42-41cc-8e8b-007a2afa8358"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill motion_type for PAGA settlement ruling (#1818)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to DB",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        # Read current ruling
        row = conn.execute(
            "SELECT id, motion_type, LEFT(ruling_text, 500) AS text_preview "
            "FROM rulings WHERE id = %s",
            (RULING_ID,),
        ).fetchone()

        if row is None:
            logger.error("Ruling %s not found", RULING_ID)
            sys.exit(1)

        ruling_id, current_mt, text_preview = row
        logger.info(
            "Ruling %s: current motion_type=%s, text_preview=%s",
            ruling_id,
            current_mt,
            text_preview[:120] if text_preview else "(empty)",
        )

        if current_mt is not None:
            logger.info("motion_type already populated (%s), nothing to do", current_mt)
            return

        # Read full ruling text for extraction
        full_row = conn.execute(
            "SELECT ruling_text FROM rulings WHERE id = %s", (RULING_ID,)
        ).fetchone()
        if full_row is None or not full_row[0]:
            logger.error("Ruling %s has no ruling_text", RULING_ID)
            sys.exit(1)

        ruling_text = full_row[0]
        new_mt = extract_motion_type(ruling_text)
        logger.info("Extracted motion_type: %s", new_mt)

        if new_mt is None:
            logger.warning("Could not extract motion_type from ruling text")
            sys.exit(1)

        if args.dry_run:
            logger.info(
                "[DRY RUN] Would update ruling %s: motion_type=%s", ruling_id, new_mt
            )
            return

        conn.execute(
            "UPDATE rulings SET motion_type = %s, updated_at = NOW() WHERE id = %s",
            (new_mt, RULING_ID),
        )
        conn.commit()
        logger.info("Updated ruling %s: motion_type=%s", ruling_id, new_mt)

        # Verify
        verify_row = conn.execute(
            "SELECT motion_type FROM rulings WHERE id = %s", (RULING_ID,)
        ).fetchone()
        logger.info(
            "Verification: motion_type=%s", verify_row[0] if verify_row else "NOT FOUND"
        )


if __name__ == "__main__":
    main()
