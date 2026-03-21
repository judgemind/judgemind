#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA case titles that contain department header boilerplate (#1244).

Finds cases whose case_title contains "Law And Motion Rulings" (the LA
department calendar header text) and re-extracts the correct title from
the ruling_text stored in the rulings table.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_la_header_titles.py
    scripts/ecs-run.sh --script scripts/backfill_la_header_titles.py -- --dry-run

Options:
    --dry-run    Print what would be updated without writing to the database.
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

# ---------------------------------------------------------------------------
# Department header boilerplate detection
# ---------------------------------------------------------------------------

_DEPT_HEADER_RE = re.compile(
    r"DEPARTMENT\s+\S+\s+LAW AND MOTION RULINGS",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Title extraction from ruling text (inline to satisfy ECS oneshot constraint)
# ---------------------------------------------------------------------------

# Entity descriptor phrases to strip from party names.
_ENTITY_DESCRIPTOR_RE = re.compile(
    r",?\s*(?:"
    r"An Individual(?:\s+And Derivatively On Behalf Of [^,;]+)?"
    r"|An? (?:Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut"
    r"|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois"
    r"|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts"
    r"|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada"
    r"|New Hampshire|New Jersey|New Mexico|New York|North Carolina"
    r"|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island"
    r"|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia"
    r"|Washington|West Virginia|Wisconsin|Wyoming)"
    r" (?:Corporation|Limited Liability Company|Limited Partnership"
    r"|General Partnership|Business Entity|Nonprofit Corporation|Public Entity)"
    r"|A (?:Corporation|Limited Liability Company|Limited Partnership"
    r"|General Partnership|Business Entity|Nonprofit Corporation|Public Entity)"
    r"|Individually And As [^,;]+"
    r"|By And Through [^,;]+"
    r"|As Trustee Of [^,;]+"
    r"|Successor In Interest To [^,;]+"
    r"|Derivatively On Behalf Of [^,;]+"
    r"|Form Unknown"
    r"|Doe(?:s)? \d+ (?:To|Through) \d+(?:,? Inclusive)?"
    r")",
    re.IGNORECASE,
)

# Caption block: "NAME, Plaintiff(s), vs. NAME, Defendant(s)."
_P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

# Moving/responding party fields
_MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)\s+",
    re.IGNORECASE,
)


def _clean_name(raw: str) -> str:
    """Clean a party name: strip descriptors, whitespace, punctuation."""
    name = " ".join(raw.split()).strip()
    name = _ENTITY_DESCRIPTOR_RE.sub("", name).strip()
    name = re.sub(r"[;,]\s*$", "", name).strip()
    name = re.sub(r"\s+And\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(
        r",?\s*Et\.?\s*Al\.?\s*$", ", et al.", name, flags=re.IGNORECASE
    ).strip()
    name = name.strip(")(,.; ")
    return name


def _extract_title_from_caption(ruling_text: str) -> str | None:
    """Extract title from formal plaintiff/defendant caption block."""
    p_match = _P_ROLE_RE.search(ruling_text)
    d_match = _D_ROLE_RE.search(ruling_text)
    vs_match = _VS_RE.search(ruling_text)

    if not (p_match and d_match and vs_match):
        return None

    # Plaintiff name: text before the plaintiff role marker
    p_start = max(0, p_match.start() - 500)
    p_text = ruling_text[p_start : p_match.start()]
    # Take the last meaningful line as the plaintiff name
    lines = [ln.strip() for ln in p_text.split("\n") if ln.strip()]
    if not lines:
        return None
    plaintiff_raw = lines[-1].rstrip(",")

    # Defendant name: text between vs. and defendant role marker
    vs_end = vs_match.end()
    d_start = d_match.start()
    if vs_end >= d_start:
        return None
    defendant_raw = ruling_text[vs_end:d_start].strip()
    # Take the first meaningful block
    d_lines = [ln.strip() for ln in defendant_raw.split("\n") if ln.strip()]
    if not d_lines:
        return None
    defendant_raw = " ".join(d_lines).rstrip(",")

    plaintiff = _clean_name(plaintiff_raw)
    defendant = _clean_name(defendant_raw)

    if not plaintiff or not defendant:
        return None

    title = f"{plaintiff.title()} v. {defendant.title()}"
    if len(title) > 120 or len(title) < 5:
        return None
    return title


def _extract_title_from_moving_responding(ruling_text: str) -> str | None:
    """Extract title from MOVING PARTY / RESPONDING PARTY fields."""
    m_match = _MOVING_PARTY_RE.search(ruling_text)
    if m_match is None:
        return None
    r_match = _RESPONDING_PARTY_RE.search(ruling_text)
    if r_match is None:
        return None

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    skip_phrases = ("no opposition", "none", "no response", "unopposed")
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return None

    moving = _clean_name(_ROLE_PREFIX_RE.sub("", moving_raw))
    responding = _clean_name(_ROLE_PREFIX_RE.sub("", responding_raw))

    if not moving or not responding:
        return None

    title = f"{moving.title()} v. {responding.title()}"
    if len(title) > 120 or len(title) < 5:
        return None
    return title


def _extract_clean_title(ruling_text: str) -> str | None:
    """Try multiple strategies to extract a clean case title."""
    title = _extract_title_from_caption(ruling_text)
    if title and not _DEPT_HEADER_RE.search(title):
        return title

    title = _extract_title_from_moving_responding(ruling_text)
    if title and not _DEPT_HEADER_RE.search(title):
        return title

    return None


def main() -> None:
    """Find and fix LA case titles containing department header boilerplate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            # Find cases with header boilerplate in the title
            cur.execute(
                """
                SELECT c.id, c.case_number, c.case_title
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'Los Angeles'
                  AND (c.case_title LIKE '%%Law And Motion Rulings%%'
                       OR c.case_title LIKE '%%LAW AND MOTION RULINGS%%'
                       OR c.case_title LIKE '%%Department%%Law%%Motion%%')
                """
            )
            bad_cases = cur.fetchall()

        logger.info("Found %d cases with header boilerplate in title", len(bad_cases))

        fixed = 0
        skipped = 0
        for case_id, case_number, old_title in bad_cases:
            # Fetch the ruling text for this case
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.ruling_text
                    FROM rulings r
                    JOIN documents d ON r.document_id = d.id
                    WHERE d.case_id = %s
                    ORDER BY r.hearing_date DESC NULLS LAST
                    LIMIT 1
                    """,
                    (case_id,),
                )
                row = cur.fetchone()

            if not row or not row[0]:
                logger.warning(
                    "No ruling text for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            ruling_text = row[0]
            new_title = _extract_clean_title(ruling_text)

            if new_title is None:
                logger.warning(
                    "Could not extract clean title for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s (%s):\n  OLD: %s\n  NEW: %s",
                case_id,
                case_number,
                old_title[:100],
                new_title,
            )

            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_title = %s WHERE id = %s",
                        (new_title, case_id),
                    )
                conn.commit()
                fixed += 1
            else:
                fixed += 1

        logger.info(
            "Done: %d fixed, %d skipped%s",
            fixed,
            skipped,
            " (dry-run)" if args.dry_run else "",
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
