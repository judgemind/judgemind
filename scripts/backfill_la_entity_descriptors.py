#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA case titles that contain entity descriptors (#1267).

Finds LA cases with case_title longer than 150 characters (caused by entity
descriptors like "An Individual", "A California Corporation", etc.) and cleans
them using the same sanitization logic as the live scraper.

Strategy:
  1. Apply ``_sanitize_title()`` directly to the existing title.
  2. If that fails (returns None), re-extract from ruling_text using the
     caption block and moving/responding party patterns.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py -- --dry-run
    scripts/ecs-run.sh --script scripts/backfill_la_entity_descriptors.py -- --threshold 120

Options:
    --dry-run      Print what would be updated without writing to the database.
    --threshold N  Title length threshold (default: 150).
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
# Entity descriptor stripping (inlined from la_tentatives.py for ECS oneshot)
# ---------------------------------------------------------------------------

_ENTITY_DESCRIPTOR_RE = re.compile(
    r",?\s*(?:"
    r"An Individual(?:\s+And Derivatively On Behalf Of [^,;]+)?"
    r"|A (?:California|Delaware|Nevada|Texas|Colorado|New York|Florida|Minnesota"
    r"|Georgia|Illinois|New Jersey|Oregon|Washington|Maryland|Ohio|Arizona)"
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

_DEPT_HEADER_BOILERPLATE_RE = re.compile(
    r"DEPARTMENT\s+\S+\s+LAW AND MOTION RULINGS",
    re.IGNORECASE,
)

_MAX_TITLE_LENGTH = 120

# For the backfill, we use a more lenient length limit.  The live scraper
# enforces 120 chars, but for existing data we accept anything that is at
# least shorter than the original title after stripping entity descriptors.
_BACKFILL_MAX_TITLE_LENGTH = 250


def sanitize_title(raw_title: str | None) -> str | None:
    """Clean a raw case title: strip entity descriptors, excess length.

    Returns ``None`` if the title is invalid (contains department header
    boilerplate, is too short after cleaning, or is empty).

    This is a standalone copy of ``la_tentatives._sanitize_title()`` to
    satisfy the ECS oneshot constraint (no sibling imports).
    """
    return _strip_descriptors(raw_title, max_length=_MAX_TITLE_LENGTH)


def sanitize_title_lenient(raw_title: str | None) -> str | None:
    """Like ``sanitize_title`` but with a lenient max-length for backfill use.

    Accepts titles up to ``_BACKFILL_MAX_TITLE_LENGTH`` after cleaning,
    because many multi-party LA cases are legitimately > 120 chars even
    after stripping entity descriptors.
    """
    return _strip_descriptors(raw_title, max_length=_BACKFILL_MAX_TITLE_LENGTH)


def _strip_descriptors(raw_title: str | None, *, max_length: int) -> str | None:
    """Strip entity descriptors from a case title.

    Returns ``None`` if the title is invalid (boilerplate, too short,
    or exceeds *max_length* after cleaning).
    """
    if not raw_title:
        return None

    if _DEPT_HEADER_BOILERPLATE_RE.search(raw_title):
        return None

    title = raw_title

    # Normalize "vs.", "vs", "V." etc. to " v. " so splitting works consistently.
    title = re.sub(r"\s+[Vv][Ss]?\.?\s+", " v. ", title)

    # Strip entity descriptors from each side of "v."
    if " v. " in title:
        parts = title.split(" v. ", 1)
        cleaned_parts = []
        for part in parts:
            cleaned = _ENTITY_DESCRIPTOR_RE.sub("", part).strip()
            # Strip trailing semicolons, commas, "and" connectors left by removal
            cleaned = re.sub(r"[;,]\s*$", "", cleaned).strip()
            cleaned = re.sub(r"\s+And\s*$", "", cleaned, flags=re.IGNORECASE).strip()
            # Collapse "Et Al." variants
            cleaned = re.sub(
                r",?\s*Et\.?\s*Al\.?\s*$",
                ", et al.",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()
            # Remove dangling punctuation
            cleaned = cleaned.strip(",;. ")
            cleaned_parts.append(cleaned)
        title = " v. ".join(cleaned_parts)

    # Final length check
    if len(title) > max_length or len(title) < 5:
        return None

    return title


# ---------------------------------------------------------------------------
# Title extraction from ruling text (fallback strategies)
# ---------------------------------------------------------------------------

_P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

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


def extract_title_from_caption(ruling_text: str) -> str | None:
    """Extract title from formal plaintiff/defendant caption block."""
    p_match = _P_ROLE_RE.search(ruling_text)
    d_match = _D_ROLE_RE.search(ruling_text)
    vs_match = _VS_RE.search(ruling_text)

    if not (p_match and d_match and vs_match):
        return None

    # Plaintiff name: text before the plaintiff role marker
    p_start = max(0, p_match.start() - 500)
    p_text = ruling_text[p_start : p_match.start()]
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
    d_lines = [ln.strip() for ln in defendant_raw.split("\n") if ln.strip()]
    if not d_lines:
        return None
    defendant_raw = " ".join(d_lines).rstrip(",")

    plaintiff = _clean_name(plaintiff_raw)
    defendant = _clean_name(defendant_raw)

    if not plaintiff or not defendant:
        return None

    title = f"{plaintiff.title()} v. {defendant.title()}"
    if len(title) > _MAX_TITLE_LENGTH or len(title) < 5:
        return None
    return title


def extract_title_from_moving_responding(ruling_text: str) -> str | None:
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
    if len(title) > _MAX_TITLE_LENGTH or len(title) < 5:
        return None
    return title


def extract_clean_title(ruling_text: str) -> str | None:
    """Try multiple strategies to extract a clean case title from ruling text."""
    title = extract_title_from_caption(ruling_text)
    if title and not _DEPT_HEADER_BOILERPLATE_RE.search(title):
        return title

    title = extract_title_from_moving_responding(ruling_text)
    if title and not _DEPT_HEADER_BOILERPLATE_RE.search(title):
        return title

    return None


def clean_case_title(
    old_title: str,
    ruling_text: str | None,
) -> str | None:
    """Determine the best cleaned title for a case.

    Strategy 1: Apply ``sanitize_title()`` directly to the existing title.
    Strategy 2: Re-extract from ruling text using caption/party patterns.

    Returns ``None`` if no clean title can be determined.
    """
    # Strategy 1: sanitize the existing title directly (lenient length for backfill)
    cleaned = sanitize_title_lenient(old_title)
    if cleaned is not None:
        return cleaned

    # Strategy 2: re-extract from ruling text
    if ruling_text:
        return extract_clean_title(ruling_text)

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Find and fix LA case titles with entity descriptors (> threshold chars)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--threshold",
        type=int,
        default=150,
        help="Title length threshold (default: 150)",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.case_number, c.case_title
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'Los Angeles'
                  AND length(c.case_title) > %s
                ORDER BY c.case_number
                """,
                (args.threshold,),
            )
            long_cases = cur.fetchall()

        logger.info(
            "Found %d LA cases with title > %d chars",
            len(long_cases),
            args.threshold,
        )

        fixed = 0
        skipped = 0
        for case_id, case_number, old_title in long_cases:
            # Fetch the ruling text for fallback extraction
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

            ruling_text = row[0] if row and row[0] else None
            new_title = clean_case_title(old_title, ruling_text)

            if new_title is None:
                logger.warning(
                    "Could not clean title for case %s (%s): %s",
                    case_id,
                    case_number,
                    old_title[:100],
                )
                skipped += 1
                continue

            if new_title == old_title:
                logger.info(
                    "Title unchanged for case %s (%s), skipping",
                    case_id,
                    case_number,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s (%s):\n  OLD: %s\n  NEW: %s",
                case_id,
                case_number,
                old_title[:120],
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
