#!/usr/bin/env python3
"""Backfill case_title on existing cases from ruling_text.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and extracts party names from ruling
text to populate the cases.case_title column.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_case_titles.py

Each batch is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
batches are preserved and the script can be safely re-run (it is
idempotent — it only updates rows where case_title IS NULL).

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
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
# Title extraction regex
# ---------------------------------------------------------------------------

# Matches the formal party caption block in LA ruling text.
#
# The caption block follows a specific pattern:
#   PARTY_NAME,\n  Plaintiff(s),\n  vs.\n  PARTY_NAME,\n  Defendant(s).
#
# Key distinction from body text: in the caption, "Plaintiff" appears as a
# standalone role designation (followed by comma/period/paren/newline), NOT
# as part of a sentence like "Plaintiff's motion..." or "Plaintiff filed...".
#
# The regex requires:
# 1. A party role keyword (Plaintiff/Petitioner/Cross-Complainant) preceded
#    by a comma or newline (not by a word character — avoids mid-sentence hits)
# 2. Followed by "vs." or "v." on the same or next line
# 3. Followed by another party role keyword (Defendant/Respondent/Cross-Defendant)
#
# We use a single regex that matches the entire caption block from the
# plaintiff role through the defendant role, then extract names from the
# text immediately before and between those markers.

# Find the caption Plaintiff/Defendant role designations that appear at the
# start of a line (with optional whitespace).  This distinguishes caption
# markers from body text like "Plaintiff's motion..." which appears mid-line.
# Match Plaintiff/Defendant role keywords that appear as standalone
# designations — either at the start of a line or after a comma.
# The negative lookahead (?![\w']) excludes possessives ("Plaintiff's")
# and mid-word hits.
# Caption role designations appear on their own line, followed by comma,
# period, paren, or newline — never followed by a space and a name
# (which would indicate body text like "Plaintiff Smith filed...").
_P_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
_D_ROLE_RE = re.compile(
    r"(?:^|\n)\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?\s*[,.\n)]",
    re.MULTILINE,
)
# Also match inline format: ", Plaintiff(s), vs."
_P_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?\s*,",
)
_D_ROLE_INLINE_RE = re.compile(
    r",\s*(?:Defendant|Respondent|Cross-Defendant)\(?s?\)?[,.]",
)
_VS_RE = re.compile(r"\bv(?:s)?\.", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Pattern 2: "MOVING PARTY: [name]" / "RESPONDING PARTY: [name]"
# ---------------------------------------------------------------------------
# Many LA rulings use this format instead of a formal caption block.
# The moving/responding party fields may include a role prefix like
# "Defendant " or "Plaintiffs " which should be stripped.
_MOVING_PARTY_RE = re.compile(
    r"MOVING PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
_RESPONDING_PARTY_RE = re.compile(
    r"(?:RESPONDING|OPPOSING) PART(?:Y|IES)\s*:\s*(?P<name>.+?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Role prefixes to strip from moving/responding party names.
_ROLE_PREFIX_RE = re.compile(
    r"^(?:Defendants?|Plaintiffs?|Petitioners?|Respondents?"
    r"|Cross-Complainants?|Cross-Defendants?)\s+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Pattern 3: "Case Name: [text]" or "Case Title: [text]" inline field
# ---------------------------------------------------------------------------
_CASE_NAME_FIELD_RE = re.compile(
    r"CASE\s+(?:NAME|TITLE)\s*:\s*(?P<title>.+?)(?:\s+CASE\s+NUMBER|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Descriptors that follow a party name and should be stripped.
_DESCRIPTOR_RE = re.compile(
    r",?\s*(?:an individual|a (?:public|private|California|Delaware)"
    r"[\w\s,]*?(?:entity|company|corporation|trust|llc|inc\.?))"
    r"[\s,]*$",
    re.IGNORECASE,
)


def _clean_party_name(raw: str) -> str:
    """Normalise a captured party name: collapse whitespace, strip trailing
    commas/et al, remove descriptors like 'an individual', and title-case."""
    name = " ".join(raw.split()).strip()
    # Strip descriptors like ", an individual" or ", a public entity"
    name = _DESCRIPTOR_RE.sub("", name).strip().rstrip(",").strip()
    # Strip "et al." suffix
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    # Remove stray leading/trailing punctuation
    name = name.strip(")(,.; ")
    return name


def _extract_from_moving_responding(ruling_text: str) -> str | None:
    """Extract a case title from MOVING PARTY / RESPONDING PARTY fields.

    Many LA rulings list parties as:
        MOVING PARTY: Defendant Acme Corp.
        RESPONDING PARTY: Plaintiffs John Doe and Jane Doe

    We strip the role prefix (Defendant/Plaintiffs/etc.) and construct
    "[Moving Party] v. [Responding Party]".
    """
    m_match = _MOVING_PARTY_RE.search(ruling_text)
    if m_match is None:
        return None
    r_match = _RESPONDING_PARTY_RE.search(ruling_text)
    if r_match is None:
        return None

    moving_raw = m_match.group("name").strip()
    responding_raw = r_match.group("name").strip()

    # Reject non-party content like "No opposition filed"
    skip_phrases = ("no opposition", "none", "no response", "unopposed")
    for phrase in skip_phrases:
        if phrase in responding_raw.lower():
            # Only a moving party — no opposing party for a title
            return None

    # Strip role prefixes like "Defendant " or "Plaintiffs "
    moving_name = _ROLE_PREFIX_RE.sub("", moving_raw)
    responding_name = _ROLE_PREFIX_RE.sub("", responding_raw)

    moving_name = _clean_party_name(moving_name)
    responding_name = _clean_party_name(responding_name)

    if not moving_name or not responding_name:
        return None

    title = f"{moving_name.title()} v. {responding_name.title()}"

    if len(title) > 150:
        return None

    return title


def _extract_from_case_name_field(ruling_text: str) -> str | None:
    """Extract a case title from an inline 'Case Name:' or 'Case Title:' field.

    Some LA rulings include a metadata field like:
        CASE NAME: Porsche Leasing Ltd. et al. v. Tsisana Mikia, et al.
    """
    m = _CASE_NAME_FIELD_RE.search(ruling_text)
    if m is None:
        return None

    raw_title = m.group("title").strip()

    # Must contain "v." to be a real case name (not just a description)
    if not re.search(r"\bv\.?\s", raw_title):
        return None

    # Clean up whitespace
    title = " ".join(raw_title.split())

    # Strip trailing punctuation
    title = title.rstrip(".,;: ")

    if len(title) > 150 or len(title) < 5:
        return None

    return title


def extract_case_title(ruling_text: str) -> str | None:
    """Extract a case title from ruling text.

    Tries multiple extraction strategies in order of reliability:

    1. Formal caption block (Plaintiff vs. Defendant) — most reliable
    2. Inline "Case Name:" or "Case Title:" field — direct extraction
    3. "MOVING PARTY:" / "RESPONDING PARTY:" fields — construct from party names

    Returns a title like "Buenaventura v. City Of Pasadena", or None.
    """
    # Strategy 1: Formal caption block (existing logic)
    title = _extract_from_caption_block(ruling_text)
    if title is not None:
        return title

    # Strategy 2: Inline "Case Name:" / "Case Title:" field
    title = _extract_from_case_name_field(ruling_text)
    if title is not None:
        return title

    # Strategy 3: MOVING PARTY / RESPONDING PARTY fields
    return _extract_from_moving_responding(ruling_text)


def _extract_from_caption_block(ruling_text: str) -> str | None:
    """Extract a case title from the formal Plaintiff/Defendant caption block.

    The function looks for line-anchored Plaintiff/Defendant keywords (which
    distinguish the caption block from body text), then extracts names from
    the surrounding text.
    """
    # Step 1: find "Plaintiff" as a standalone role designation.
    # Try line-anchored first (most reliable), then inline format.
    p_match = _P_ROLE_RE.search(ruling_text)
    if p_match is None:
        p_match = _P_ROLE_INLINE_RE.search(ruling_text)
    if p_match is None:
        return None

    # Step 2: find "vs." or "v." after the plaintiff role.
    # In the caption, "vs." appears within ~30 chars of "Plaintiff".
    # A large gap means the vs. is in body text, not the caption.
    vs_match = _VS_RE.search(ruling_text, p_match.end())
    if vs_match is None:
        return None
    if vs_match.start() - p_match.end() > 30:
        return None

    # Step 3: find "Defendant" after vs.
    # Similarly, the defendant name + role should be within ~300 chars of vs.
    d_match = _D_ROLE_RE.search(ruling_text, vs_match.end())
    if d_match is None:
        d_match = _D_ROLE_INLINE_RE.search(ruling_text, vs_match.end())
    if d_match is None:
        return None
    if d_match.start() - vs_match.end() > 300:
        return None

    # Step 4: extract plaintiff name — text before the Plaintiff line.
    # Look back up to 300 chars and take lines that look like names.
    search_start = max(0, p_match.start() - 300)
    plaintiff_raw = ruling_text[search_start : p_match.start()]
    lines = plaintiff_raw.split("\n")

    name_lines: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or stripped == ",":
            if name_lines:
                break
            continue
        upper = stripped.upper()
        # Stop at structural header lines
        if (
            upper in ("DISTRICT", "CALIFORNIA", "DEPARTMENT")
            or upper.startswith("SUPERIOR COURT")
            or upper.startswith("FOR THE")
            or upper.startswith("COUNTY OF")
        ):
            break
        # Stop at single-char/number lines (department designators like "V", "3")
        if len(stripped) <= 2 and not stripped.endswith(","):
            break
        # Limit to 4 lines max — party names don't span more than that
        if len(name_lines) >= 4:
            break
        name_lines.append(stripped)

    if not name_lines:
        return None

    name_lines.reverse()
    plaintiff = " ".join(name_lines)

    # Step 5: extract defendant name — text between vs. and Defendant line
    defendant_raw = ruling_text[vs_match.end() : d_match.start()]

    plaintiff = _clean_party_name(plaintiff)
    defendant = _clean_party_name(defendant_raw)

    if not plaintiff or not defendant:
        return None

    title = f"{plaintiff.title()} v. {defendant.title()}"

    # Sanity check: reject obviously wrong extractions.
    # Real case titles are under ~120 chars; anything longer is body text.
    if len(title) > 150:
        return None

    return title


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT c.id, r.ruling_text
    FROM cases c
    JOIN LATERAL (
        SELECT r2.ruling_text
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE c.case_title IS NULL
    ORDER BY c.created_at
    LIMIT %s OFFSET %s
"""

UPDATE_QUERY = """
    UPDATE cases
    SET case_title = %s,
        updated_at = NOW()
    WHERE id = %s::uuid
      AND case_title IS NULL
"""


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    offset: int = 0,
) -> tuple[int, int]:
    """Process one batch of cases.  Returns (processed, updated) counts."""
    processed = 0
    updated = 0

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (batch_size, offset))
        rows = cur.fetchall()

    if not rows:
        return 0, 0

    for case_id, ruling_text in rows:
        processed += 1

        title = extract_case_title(ruling_text)
        if title is None:
            logger.debug("No title extracted for case %s", case_id)
            continue

        logger.info("Case %s -> %s", case_id, title)

        with conn.cursor() as cur:
            cur.execute(UPDATE_QUERY, (title, str(case_id)))
        updated += 1

    return processed, updated


def run_backfill(
    dsn: str,
    *,
    batch_size: int = 100,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run the full backfill.  Returns summary stats."""
    total_processed = 0
    total_updated = 0
    offset = 0

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated = backfill_batch(conn, effective_batch, offset)
            total_processed += processed
            total_updated += updated

            # Commit (or rollback) after each batch so that progress is
            # saved incrementally and not lost if the connection drops.
            if dry_run:
                conn.rollback()
            else:
                conn.commit()

            logger.info(
                "Batch: processed=%d updated=%d (total: %d/%d)%s",
                processed,
                updated,
                total_processed,
                total_updated,
                " [dry-run, rolled back]" if dry_run else " [committed]",
            )

            if processed < effective_batch:
                # Last batch — no more rows
                break

            offset += effective_batch

    stats = {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }
    return stats


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill case_title on existing cases from ruling_text.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without writing to the database.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of cases per batch (default: 100).",
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
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    logger.info(
        "Backfill complete: %d cases processed, %d updated",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
