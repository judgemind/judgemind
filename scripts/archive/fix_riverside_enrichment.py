#!/usr/bin/env python3
"""Fix Riverside enrichment pipeline gaps (#2022).

Re-extracts motion_type and outcome from ruling_text for Riverside rulings
that are missing these fields. Also cleans contaminated case titles.

Usage:
    scripts/ecs-run-task.sh scripts/fix_riverside_enrichment.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_riverside_enrichment.py
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome extraction (inline — ECS oneshot cannot import from packages)
# ---------------------------------------------------------------------------

_OUTCOME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgranted\s+in\s+part\b", re.IGNORECASE), "granted_in_part"),
    (re.compile(r"\bdenied\s+in\s+part\b", re.IGNORECASE), "denied_in_part"),
    (re.compile(r"\bgranted\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bdenied\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bsustain(?:ed)?\b", re.IGNORECASE), "granted"),
    (re.compile(r"\boverrule[d]?\b", re.IGNORECASE), "denied"),
    (re.compile(r"\bmoot\b", re.IGNORECASE), "moot"),
    (re.compile(r"\bcontinued?\b", re.IGNORECASE), "continued"),
    (re.compile(r"\boff[\s-]?calendar\b", re.IGNORECASE), "off_calendar"),
    (re.compile(r"\bsubmitted\b", re.IGNORECASE), "submitted"),
    (re.compile(r"\bno\s+tentative\b", re.IGNORECASE), "other"),
    (re.compile(r"\bhearing\s+required\b", re.IGNORECASE), "other"),
    (re.compile(r"\bit\s+is\s+(?:so\s+)?ordered\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bgrant\b", re.IGNORECASE), "granted"),
    (re.compile(r"\bdeny\b", re.IGNORECASE), "denied"),
]


def extract_outcome(text: str) -> str | None:
    """Extract outcome from ruling text."""
    for pattern, value in _OUTCOME_PATTERNS:
        if pattern.search(text):
            return value
    return None


# ---------------------------------------------------------------------------
# Motion type extraction (inline)
# ---------------------------------------------------------------------------

_MOTION_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(?:motion\s+for\s+)?summary\s+adjudication\b", re.IGNORECASE),
        "msj_partial",
    ),
    (re.compile(r"\bpartial\s+summary\s+judgment\b", re.IGNORECASE), "msj_partial"),
    (re.compile(r"\b(?:motion\s+for\s+)?summary\s+judgment\b", re.IGNORECASE), "msj"),
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
            r"\bclass\s+action\s+settlement\b|\bpreliminary\s+approval\b", re.IGNORECASE
        ),
        "class_action_settlement",
    ),
    (
        re.compile(r"\bPAGA\s+settlement\b|\bapproval\s+of\s+PAGA\b", re.IGNORECASE),
        "paga_settlement",
    ),
    (
        re.compile(
            r"\bsettlement\s+(?:agreement|approval|hearing)\b|\bapproval\s+of\s+(?:\w+\s+)*?settlement\b",
            re.IGNORECASE,
        ),
        "settlement_approval",
    ),
    (
        re.compile(
            r"\bpetition\s+(?:for\s+)?(?:probate|to\s+administer\s+estate|for\s+letters)\b",
            re.IGNORECASE,
        ),
        "petition_for_probate",
    ),
    (
        re.compile(
            r"\b(?:guardianship\s+petition|petition\s+for\s+(?:guardianship|conservatorship))\b",
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
    # --- Riverside-specific patterns (#2022) ---
    (
        re.compile(
            r"\bmotion\s+(?:for\s+|to\s+)?(?:enter\s+|entry\s+of\s+)?stipulated\s+judgment\b",
            re.IGNORECASE,
        ),
        "motion_for_stipulated_judgment",
    ),
    (
        re.compile(r"\bmotion\s+(?:for\s+)?entry\s+of\s+judgment\b", re.IGNORECASE),
        "motion_for_entry_of_judgment",
    ),
    (re.compile(r"\bmotion\s+to\s+bifurcate\b", re.IGNORECASE), "motion_to_bifurcate"),
    (
        re.compile(r"\bmotion\s+for\s+class\s+certification\b", re.IGNORECASE),
        "motion_for_class_certification",
    ),
    (
        re.compile(r"\bmotion\s+to\s+reclassify\b", re.IGNORECASE),
        "motion_to_reclassify",
    ),
    (
        re.compile(r"\bmotion\s+for\s+(?:judicial\s+)?approval\b", re.IGNORECASE),
        "motion_for_judicial_approval",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+(?:appointment\s+of\s+)?receiver\b", re.IGNORECASE
        ),
        "motion_for_appointment_of_receiver",
    ),
    (
        re.compile(r"\bright\s+to\s+attach\s+order\b", re.IGNORECASE),
        "right_to_attach_order",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+order\b.*\badmissions?\s+(?:to\s+be\s+)?(?:deemed\s+)?admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(
            r"\bmotion\s+(?:for\s+)?(?:order\s+)?(?:for\s+|to\s+)?deem(?:ing)?\s+(?:matters?\s+)?admitted\b",
            re.IGNORECASE,
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(
            r"\badmissions?\s+(?:to\s+be\s+)?deemed\s+admitted\b", re.IGNORECASE
        ),
        "deem_admissions_admitted",
    ),
    (
        re.compile(r"\bmotion\s+to\s+consolidate\b", re.IGNORECASE),
        "motion_to_consolidate",
    ),
    (
        re.compile(
            r"\bmotion\s+for\s+(?:order\s+)?(?:authorizing\s+)?recordation\b",
            re.IGNORECASE,
        ),
        "lis_pendens",
    ),
    (
        re.compile(
            r"\b(?:notice\s+of\s+)?(?:pendency\s+of\s+action|lis\s+pendens)\b",
            re.IGNORECASE,
        ),
        "lis_pendens",
    ),
    (
        re.compile(
            r"\bmotion\s+(?:for|to)\s+(?:transfer|change)\s+venue\b", re.IGNORECASE
        ),
        "motion_to_transfer_venue",
    ),
    (re.compile(r"\bmotion\s+to\s+seal\b", re.IGNORECASE), "motion_to_seal"),
    (
        re.compile(r"\brequest\s+for\s+dismissal\b", re.IGNORECASE),
        "request_for_dismissal",
    ),
    (re.compile(r"\bwrit\s+of\s+attachment\b", re.IGNORECASE), "right_to_attach_order"),
    (re.compile(r"\bex\s+parte\b", re.IGNORECASE), "ex_parte_application"),
]


def extract_motion_type(text: str) -> str | None:
    """Extract motion type from ruling text."""
    for pattern, value in _MOTION_TYPE_PATTERNS:
        if pattern.search(text):
            return value
    return None


# ---------------------------------------------------------------------------
# Case title contamination patterns
# ---------------------------------------------------------------------------

_IMPLAUSIBLE_TITLE_RE = re.compile(
    r"\b(?:GRANTED|DENIED|CONTINUED|TENTATIVE RULING|TENTATIVE RULINGS"
    r"|OVERRULED|SUSTAINED|MOOT|OFF CALENDAR|HEARING|MOTION|DEMURRER"
    r"|ORDER|Department\s+[A-Z0-9]+|Timekeeper|paralegal)\b",
    re.IGNORECASE,
)

_MULTIPLE_CASE_NUMBERS_RE = re.compile(
    r"(?:CV[A-Z]{2,4}\d{5,}|CIV[A-Z]{2}\d{5,}|\d{2,4}-\d{7,}|\d{2}[A-Z]{2}CV\d{5,})"
    r".*"
    r"(?:CV[A-Z]{2,4}\d{5,}|CIV[A-Z]{2}\d{5,}|\d{2,4}-\d{7,}|\d{2}[A-Z]{2}CV\d{5,})",
    re.IGNORECASE,
)


def is_contaminated_title(title: str) -> bool:
    """Return True if the title contains contamination patterns."""
    if not title:
        return False
    if _IMPLAUSIBLE_TITLE_RE.search(title):
        return True
    if _MULTIPLE_CASE_NUMBERS_RE.search(title):
        return True
    if len(title) > 120:
        return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fix_motion_types(conn: psycopg.Connection, *, dry_run: bool = False) -> int:
    """Re-extract motion_type for null Riverside rulings."""
    query = """
        SELECT r.id, LEFT(r.ruling_text, 2000) as ruling_text
        FROM rulings r
        JOIN cases c ON r.case_id = c.id
        JOIN courts co ON c.court_id = co.id
        WHERE co.county = 'Riverside'
          AND r.motion_type IS NULL
          AND r.ruling_text IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    updated = 0
    for ruling_id, ruling_text in rows:
        motion_type = extract_motion_type(ruling_text)
        if motion_type is not None:
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE rulings SET motion_type = %s WHERE id = %s",
                        (motion_type, ruling_id),
                    )
            updated += 1
            logger.info(
                "Fixed motion_type",
                extra={
                    "ruling_id": str(ruling_id),
                    "motion_type": motion_type,
                    "dry_run": dry_run,
                },
            )
    return updated


def fix_outcomes(conn: psycopg.Connection, *, dry_run: bool = False) -> int:
    """Re-extract outcome for null Riverside rulings."""
    query = """
        SELECT r.id, LEFT(r.ruling_text, 2000) as ruling_text
        FROM rulings r
        JOIN cases c ON r.case_id = c.id
        JOIN courts co ON c.court_id = co.id
        WHERE co.county = 'Riverside'
          AND r.outcome IS NULL
          AND r.ruling_text IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    updated = 0
    for ruling_id, ruling_text in rows:
        outcome = extract_outcome(ruling_text)
        if outcome is not None:
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE rulings SET outcome = %s WHERE id = %s",
                        (outcome, ruling_id),
                    )
            updated += 1
            logger.info(
                "Fixed outcome",
                extra={
                    "ruling_id": str(ruling_id),
                    "outcome": outcome,
                    "dry_run": dry_run,
                },
            )
    return updated


def fix_case_titles(conn: psycopg.Connection, *, dry_run: bool = False) -> int:
    """Clean contaminated Riverside case titles by setting them to NULL.

    Contaminated titles will be re-extracted on next reingest.
    """
    query = """
        SELECT c.id, c.case_title
        FROM cases c
        JOIN courts co ON c.court_id = co.id
        WHERE co.county = 'Riverside'
          AND c.case_title IS NOT NULL
          AND (
              c.case_title ILIKE '%%motion%%'
              OR c.case_title ILIKE '%%tentative%%'
              OR c.case_title ILIKE '%%hearing%%'
              OR c.case_title ILIKE '%%Department%%'
              OR c.case_title ILIKE '%%Timekeeper%%'
              OR c.case_title ILIKE '%%paralegal%%'
          )
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    updated = 0
    for case_id, case_title in rows:
        if is_contaminated_title(case_title):
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_title = NULL WHERE id = %s",
                        (case_id,),
                    )
            updated += 1
            logger.info(
                "Cleared contaminated case_title",
                extra={
                    "case_id": str(case_id),
                    "old_title": case_title[:80],
                    "dry_run": dry_run,
                },
            )
    return updated


def strip_calendar_headers(conn: psycopg.Connection, *, dry_run: bool = False) -> int:
    """Strip calendar header boilerplate from Riverside ruling text.

    Removes the standard "Tentative Rulings for [date]\\nDepartment [N]\\n..."
    boilerplate from the beginning of ruling text, plus the oral argument
    instructions block that follows.
    """
    query = """
        SELECT r.id, r.ruling_text
        FROM rulings r
        JOIN cases c ON r.case_id = c.id
        JOIN courts co ON c.court_id = co.id
        WHERE co.county = 'Riverside'
          AND r.ruling_text ILIKE '%%calendar%%hearing date%%'
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    # Pattern to match Riverside calendar header boilerplate.
    # Matches from "Tentative Rulings for..." through the end of the
    # oral argument instructions block (ending around "p.m." or "calendar").
    header_re = re.compile(
        r"^Tentative\s+Rulings?\s+for\s+.*?"
        r"(?:calendar\s*[\.\n]|4:30\s+p\.m\.)",
        re.IGNORECASE | re.DOTALL,
    )

    updated = 0
    for ruling_id, ruling_text in rows:
        m = header_re.match(ruling_text)
        if m:
            cleaned = ruling_text[m.end() :].strip()
            if cleaned and cleaned != ruling_text.strip():
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE rulings SET ruling_text = %s WHERE id = %s",
                            (cleaned, ruling_id),
                        )
                updated += 1
                logger.info(
                    "Stripped calendar header",
                    extra={
                        "ruling_id": str(ruling_id),
                        "original_len": len(ruling_text),
                        "cleaned_len": len(cleaned),
                        "dry_run": dry_run,
                    },
                )
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix Riverside enrichment gaps (#2022)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable not set")
        sys.exit(1)

    with psycopg.connect(database_url, autocommit=not args.dry_run) as conn:
        logger.info("=== Fix Riverside Enrichment Gaps (#2022) ===")
        logger.info("Dry run: %s", args.dry_run)

        # 1. Fix motion types
        mt_fixed = fix_motion_types(conn, dry_run=args.dry_run)
        logger.info("Motion types fixed: %d", mt_fixed)

        # 2. Fix outcomes
        oc_fixed = fix_outcomes(conn, dry_run=args.dry_run)
        logger.info("Outcomes fixed: %d", oc_fixed)

        # 3. Clean contaminated case titles
        ct_fixed = fix_case_titles(conn, dry_run=args.dry_run)
        logger.info("Contaminated case titles cleared: %d", ct_fixed)

        # 4. Strip calendar headers
        ch_fixed = strip_calendar_headers(conn, dry_run=args.dry_run)
        logger.info("Calendar headers stripped: %d", ch_fixed)

        logger.info("=== Summary ===")
        logger.info(
            json.dumps(
                {
                    "motion_types_fixed": mt_fixed,
                    "outcomes_fixed": oc_fixed,
                    "case_titles_cleared": ct_fixed,
                    "calendar_headers_stripped": ch_fixed,
                    "dry_run": args.dry_run,
                }
            )
        )


if __name__ == "__main__":
    main()
