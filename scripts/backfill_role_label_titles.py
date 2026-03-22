#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill LA cases with role-label titles like 'Defendant v. Plaintiff' (#1425).

Finds LA cases where role labels were extracted as party names, re-extracts
the title and parties from ruling_text using the fixed parser, and updates
the database.

Affected patterns:
  - "Defendant v. Plaintiff" (pure role labels, no names)
  - "Defendant, Name v. Plaintiff, Name" (role label prefix on names)
  - "Defendants, Name v. Plaintiffs, Name" (plural variants)

Also cleans up party records where canonical_name is a bare role label.

Usage:
    scripts/ecs-run-task.sh scripts/backfill_role_label_titles.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_role_label_titles.py
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

# Role labels that should never be party names.
_BARE_ROLE_LABELS = frozenset(
    w.lower()
    for w in (
        "Defendant",
        "Defendants",
        "Plaintiff",
        "Plaintiffs",
        "Petitioner",
        "Petitioners",
        "Respondent",
        "Respondents",
        "Cross-Complainant",
        "Cross-Complainants",
        "Cross-Defendant",
        "Cross-Defendants",
    )
)

# Role prefix regex — matches role labels followed by comma and/or whitespace.
_ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)[,\s]+",
    re.IGNORECASE,
)

# Pattern to match MOVING PARTY / RESPONDING PARTY fields.
_MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)

# Entity descriptor regex for title cleaning (same as la_tentatives.py).
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
    # Hyphenated and non-hyphenated variants of successor/administrator phrases
    r"|Successor[- ]In[- ]Interest To(?:\s+And [^,;]+)?"
    r"|Administrator Of [^,;]+"
    r"|As Administrator [^,;]+"
    r"|Derivatively On Behalf Of [^,;]+"
    r"|As An Individual"
    r"|Form Unknown"
    r"|Doe(?:s)? \d+ (?:To|Through) \d+(?:,? Inclusive)?"
    r")",
    re.IGNORECASE,
)

# Plaintiff/defendant role markers in caption blocks.
_P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

# Caption block regex for party extraction.
_CASE_PARTIES_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*"
    r"(?P<p_role>Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*"
    r"(?P<d_role>Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
    re.DOTALL | re.MULTILINE,
)

_ROLE_MAP: dict[str, str] = {
    "plaintiff": "plaintiff",
    "petitioner": "petitioner",
    "cross-complainant": "cross_complainant",
    "defendant": "defendant",
    "respondent": "respondent",
    "cross-defendant": "cross_defendant",
}

# Corporate suffix patterns — protect from comma splitting.
_CORP_SUFFIX_RE = re.compile(
    r",\s*(?:Inc|LLC|LLP|L\.?P\.?|Corp|Corporation|Ltd|Co|Company"
    r"|N\.?A\.?|P\.?C\.?|PLLC|PLC)\.?(?=\s*(?:,|$))",
    re.IGNORECASE,
)

_MAX_TITLE_LENGTH = 120
MAX_PARTY_NAME_LENGTH = 500


def _is_name_fragment(name: str) -> bool:
    """Return True if name is a fragment that should not be standalone."""
    stripped = name.strip().rstrip(".,;: ")
    if not stripped:
        return True
    upper = stripped.upper().rstrip(".")
    corp_suffixes = {
        "INC",
        "LLC",
        "LLP",
        "LP",
        "CORP",
        "CORPORATION",
        "LTD",
        "CO",
        "COMPANY",
        "NA",
        "PC",
        "PLLC",
        "PLC",
    }
    if upper in corp_suffixes:
        return True
    if " " not in stripped and len(stripped) < 4:
        return True
    return False


def _split_party_names(text: str) -> list[str]:
    """Split a multi-party string into individual names."""
    placeholder = "\x00"
    protected = _CORP_SUFFIX_RE.sub(
        lambda m: m.group(0).replace(",", placeholder, 1), text
    )
    parts = re.split(r",\s+and\s+|,\s+", protected)
    if len(parts) == 1:
        parts = re.split(r"\s+and\s+", protected)
    result: list[str] = []
    for p in parts:
        restored = p.replace(placeholder, ",").strip()
        if restored and not _is_name_fragment(restored):
            result.append(restored)
    return result


def _clean_name(raw: str) -> str:
    """Clean a party name: strip role prefix, descriptors, punctuation."""
    name = " ".join(raw.split()).strip()
    name = _ROLE_PREFIX_RE.sub("", name)
    name = _ENTITY_DESCRIPTOR_RE.sub("", name).strip()
    name = re.sub(r"[;,]\s*$", "", name).strip()
    name = re.sub(r"\s+And\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(
        r",?\s*Et\.?\s*Al\.?\s*$", ", et al.", name, flags=re.IGNORECASE
    ).strip()
    name = name.strip(")(,.; ")
    # Reject bare role labels.
    if name.lower() in _BARE_ROLE_LABELS:
        return ""
    return name


def extract_title_from_caption(ruling_text: str) -> str | None:
    """Extract title from formal plaintiff/defendant caption block."""
    p_match = _P_ROLE_RE.search(ruling_text)
    d_match = _D_ROLE_RE.search(ruling_text)
    vs_match = _VS_RE.search(ruling_text)

    if not (p_match and d_match and vs_match):
        return None

    p_start = max(0, p_match.start() - 500)
    p_text = ruling_text[p_start : p_match.start()]
    lines = [ln.strip() for ln in p_text.split("\n") if ln.strip()]
    if not lines:
        return None
    plaintiff_raw = lines[-1].rstrip(",")

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

    moving = _clean_name(moving_raw)
    responding = _clean_name(responding_raw)

    if not moving or not responding:
        return None

    title = f"{moving.title()} v. {responding.title()}"
    if len(title) > _MAX_TITLE_LENGTH or len(title) < 5:
        return None
    return title


def extract_clean_title(ruling_text: str) -> str | None:
    """Try caption block then MOVING/RESPONDING PARTY patterns."""
    title = extract_title_from_caption(ruling_text)
    if title:
        return title
    return extract_title_from_moving_responding(ruling_text)


# ---------------------------------------------------------------------------
# Party extraction from ruling text
# ---------------------------------------------------------------------------


def _extract_parties_from_caption(ruling_text: str) -> list[dict[str, str]]:
    """Extract parties from caption block in ruling text."""
    parties: list[dict[str, str]] = []
    seen: set[str] = set()

    for m in _CASE_PARTIES_RE.finditer(ruling_text):
        p_role = _ROLE_MAP.get(m.group("p_role").lower(), "plaintiff")
        d_role = _ROLE_MAP.get(m.group("d_role").lower(), "defendant")

        plaintiff_raw = " ".join(m.group("plaintiff").split()).strip()
        plaintiff_raw = plaintiff_raw.rstrip(",")
        defendant_raw = " ".join(m.group("defendant").split()).strip()
        defendant_raw = defendant_raw.rstrip(",")

        plaintiff_name = _clean_name(plaintiff_raw)
        defendant_name = _clean_name(defendant_raw)

        if plaintiff_name:
            if len(plaintiff_name) > MAX_PARTY_NAME_LENGTH:
                plaintiff_name = plaintiff_name[:MAX_PARTY_NAME_LENGTH].rsplit(" ", 1)[
                    0
                ]
            key = plaintiff_name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": plaintiff_name.title(), "role": p_role})

        if defendant_name:
            if len(defendant_name) > MAX_PARTY_NAME_LENGTH:
                defendant_name = defendant_name[:MAX_PARTY_NAME_LENGTH].rsplit(" ", 1)[
                    0
                ]
            key = defendant_name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": defendant_name.title(), "role": d_role})

    return parties


def _extract_parties_from_moving_responding(
    ruling_text: str,
) -> list[dict[str, str]]:
    """Extract parties from MOVING PARTY / RESPONDING PARTY fields."""
    m_match = _MOVING_PARTY_RE.search(ruling_text)
    if m_match is None:
        return []
    r_match = _RESPONDING_PARTY_RE.search(ruling_text)
    if r_match is None:
        return []

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    skip_phrases = ("no opposition", "none", "no response", "unopposed")
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            return []

    parties: list[dict[str, str]] = []
    seen: set[str] = set()

    for raw_name, role in [
        (moving_raw, "moving_party"),
        (responding_raw, "responding_party"),
    ]:
        cleaned = _clean_name(raw_name)
        if not cleaned:
            continue
        sub_names = _split_party_names(cleaned)
        for name in sub_names:
            name = name.strip().strip(")(,.; ")
            if not name:
                continue
            if len(name) > MAX_PARTY_NAME_LENGTH:
                name = name[:MAX_PARTY_NAME_LENGTH].rsplit(" ", 1)[0]
            key = name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": name.title(), "role": role})

    return parties


def extract_parties(ruling_text: str) -> list[dict[str, str]]:
    """Extract parties from ruling text using caption or MOVING/RESPONDING."""
    parties = _extract_parties_from_caption(ruling_text)
    if parties:
        return parties
    return _extract_parties_from_moving_responding(ruling_text)


def main() -> None:
    """Find and fix LA cases with role-label titles."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)
    try:
        # Step 1: Find cases with role-label titles.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.case_number, c.case_title
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE ct.county = 'Los Angeles'
                  AND (
                    c.case_title ILIKE 'Defendant%%v.%%Plaintiff%%'
                    OR c.case_title ILIKE 'Defendants%%v.%%Plaintiff%%'
                    OR c.case_title ILIKE 'Defendant%%v.%%Plaintiffs%%'
                    OR c.case_title ILIKE 'Defendants%%v.%%Plaintiffs%%'
                  )
                ORDER BY c.case_number
                """
            )
            bad_cases = cur.fetchall()

        logger.info("Found %d cases with role-label titles", len(bad_cases))

        fixed_titles = 0
        skipped = 0
        for case_id, case_number, old_title in bad_cases:
            # Fetch ruling text for re-extraction.
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

            new_title = None
            if ruling_text:
                new_title = extract_clean_title(ruling_text)

            if new_title is None:
                logger.warning(
                    "Could not re-extract title for case %s (%s): %s",
                    case_id,
                    case_number,
                    old_title,
                )
                skipped += 1
                continue

            logger.info(
                "Case %s (%s):\n  OLD: %s\n  NEW: %s",
                case_id,
                case_number,
                old_title,
                new_title,
            )

            # Re-extract parties from ruling text.
            new_parties = extract_parties(ruling_text) if ruling_text else []

            if not args.dry_run:
                with conn.cursor() as cur:
                    # Update the case title.
                    cur.execute(
                        "UPDATE cases SET case_title = %s WHERE id = %s",
                        (new_title, case_id),
                    )

                    # Remove old case_parties for this case and re-insert.
                    cur.execute(
                        "DELETE FROM case_parties WHERE case_id = %s",
                        (case_id,),
                    )

                    for party in new_parties:
                        # Look up existing party by canonical_name, or create.
                        # parties table has no UNIQUE on canonical_name, so we
                        # SELECT first to avoid duplicates (#1566).
                        cur.execute(
                            "SELECT id FROM parties WHERE canonical_name = %s LIMIT 1",
                            (party["name"],),
                        )
                        row = cur.fetchone()
                        if row:
                            party_id = row[0]
                        else:
                            cur.execute(
                                """
                                INSERT INTO parties (canonical_name)
                                VALUES (%s)
                                RETURNING id
                                """,
                                (party["name"],),
                            )
                            party_id = cur.fetchone()[0]

                        # Link party to case — idempotent via unique
                        # constraint on (case_id, party_id, role).
                        cur.execute(
                            """
                            INSERT INTO case_parties (case_id, party_id, role)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (case_id, party_id, role) DO NOTHING
                            """,
                            (case_id, party_id, party["role"]),
                        )

                conn.commit()

            if new_parties:
                party_desc = ", ".join(
                    f"{p['name']} ({p['role']})" for p in new_parties
                )
                logger.info("  PARTIES: %s", party_desc)
            else:
                logger.warning("  No parties re-extracted for case %s", case_id)

            fixed_titles += 1

        logger.info(
            "Titles: %d fixed, %d skipped%s",
            fixed_titles,
            skipped,
            " (dry-run)" if args.dry_run else "",
        )

        # Step 2: Clean up orphaned party records with bare role-label names.
        role_labels = ["Defendant", "Plaintiff", "Defendants", "Plaintiffs"]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, canonical_name
                FROM parties
                WHERE canonical_name = ANY(%s)
                """,
                (role_labels,),
            )
            bad_parties = cur.fetchall()

        logger.info(
            "Found %d party records with role-label canonical_name",
            len(bad_parties),
        )

        deleted_parties = 0
        for party_id, canonical_name in bad_parties:
            logger.info(
                "Removing party %s (canonical_name=%s)",
                party_id,
                canonical_name,
            )
            if not args.dry_run:
                with conn.cursor() as cur:
                    # Remove case_parties references first.
                    cur.execute(
                        "DELETE FROM case_parties WHERE party_id = %s",
                        (party_id,),
                    )
                    cur.execute(
                        "DELETE FROM parties WHERE id = %s",
                        (party_id,),
                    )
                conn.commit()
            deleted_parties += 1

        logger.info(
            "Parties: %d removed%s",
            deleted_parties,
            " (dry-run)" if args.dry_run else "",
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
