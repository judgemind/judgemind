#!/usr/bin/env python3
"""Backfill party records from existing ruling text.

Connects to the database using the DATABASE_URL environment variable
(set via scripts/with-secret.sh) and extracts party names from ruling
text to populate the parties and case_parties tables.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -- python3 scripts/backfill_parties.py

Each batch is committed independently so that progress is saved
incrementally.  If the connection drops mid-run, already-committed
batches are preserved and the script can be safely re-run (it is
idempotent — it only processes cases that have no entries in
case_parties).

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of cases to process per batch (default: 100).
    --limit N       Maximum total cases to process (default: unlimited).
"""

from __future__ import annotations

# Ensure we are running inside the scraper-framework venv (re-execs if not).
from _venv_helper import ensure_venv

ensure_venv("scraper-framework")

import argparse
import logging
import os
import re
import sys
from datetime import datetime

import psycopg

# Add the scraper-framework src directory to sys.path so we can import
# the shared party utilities from the framework package.
_FRAMEWORK_SRC = os.path.join(
    os.path.dirname(__file__),
    "..",
    "packages",
    "scraper-framework",
    "src",
)
sys.path.insert(0, os.path.normpath(_FRAMEWORK_SRC))

from framework.party_utils import is_name_fragment as _is_name_fragment  # noqa: E402, F401 — re-exported for tests
from framework.party_utils import split_party_names as _split_party_names  # noqa: E402
from ingestion.db import batch_upsert_parties  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Party extraction regex — shared utilities imported from framework.party_utils.
# Regex patterns that are specific to this script remain here.
# ---------------------------------------------------------------------------

_CASE_PARTIES_RE = re.compile(
    r"^(?P<plaintiff>.+?),?\s*\n\s*(?P<p_role>Plaintiff|Petitioner|Cross-Complainant)\(?s?\)?,?"
    r"\s+vs\.\s+"
    r"(?P<defendant>.+?),?\s*\n\s*(?P<d_role>Defendant|Respondent|Cross-Defendant)\(?s?\)?\.?",
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


MAX_PARTY_NAME_LENGTH = 500  # btree index limit is ~900 chars; stay well under


def _clean_party_name(raw: str) -> str:
    """Normalise a captured party name."""
    name = " ".join(raw.split()).strip()
    name = _ROLE_PREFIX_RE.sub("", name)
    name = re.sub(r",?\s*et\s+al\.?\s*$", "", name, flags=re.IGNORECASE).strip()
    name = name.strip(")(,.; ")
    # Truncate excessively long names to stay within btree index limits
    if len(name) > MAX_PARTY_NAME_LENGTH:
        name = name[:MAX_PARTY_NAME_LENGTH].rsplit(" ", 1)[0]
    return name


def extract_parties(ruling_text: str) -> list[dict[str, str]]:
    """Extract party names and roles from ruling text.

    Returns a list of dicts, each with ``name`` and ``role`` keys.
    """
    # Strategy 1: Formal caption block
    parties = _extract_from_caption(ruling_text)
    if parties:
        return parties

    # Strategy 2: MOVING PARTY / RESPONDING PARTY
    return _extract_from_moving_responding(ruling_text)


def _extract_from_caption(ruling_text: str) -> list[dict[str, str]]:
    """Extract parties from caption block in ruling text."""
    parties: list[dict[str, str]] = []
    seen: set[str] = set()

    for m in _CASE_PARTIES_RE.finditer(ruling_text):
        p_role = _ROLE_MAP.get(m.group("p_role").lower(), "plaintiff")
        d_role = _ROLE_MAP.get(m.group("d_role").lower(), "defendant")

        plaintiff_raw = " ".join(m.group("plaintiff").split()).strip().rstrip(",")
        defendant_raw = " ".join(m.group("defendant").split()).strip().rstrip(",")

        plaintiff_name = _clean_party_name(plaintiff_raw)
        defendant_name = _clean_party_name(defendant_raw)

        if plaintiff_name:
            key = plaintiff_name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": plaintiff_name.title(), "role": p_role})

        if defendant_name:
            key = defendant_name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": defendant_name.title(), "role": d_role})

    return parties


def _extract_from_moving_responding(text: str) -> list[dict[str, str]]:
    """Extract parties from MOVING PARTY / RESPONDING PARTY fields."""
    m_match = _MOVING_PARTY_RE.search(text)
    if m_match is None:
        return []
    r_match = _RESPONDING_PARTY_RE.search(text)
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
        cleaned = _clean_party_name(raw_name)
        if not cleaned:
            continue

        sub_names = _split_party_names(cleaned)
        for name in sub_names:
            name = name.strip().strip(")(,.; ")
            if not name:
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                parties.append({"name": name.title(), "role": role})

    return parties


def _normalize_party_name(raw_name: str) -> str:
    """Normalize a raw party name for the canonical_name column."""
    name = raw_name.strip()
    name = re.sub(r"\s+", " ", name)
    return name.title()


# ---------------------------------------------------------------------------
# Core backfill logic (importable for testing)
# ---------------------------------------------------------------------------

FETCH_QUERY = """
    SELECT c.id, r.ruling_text, r.court_id, c.created_at
    FROM cases c
    JOIN LATERAL (
        SELECT r2.ruling_text, r2.court_id
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE NOT EXISTS (
        SELECT 1 FROM case_parties cp WHERE cp.case_id = c.id
    )
    AND (c.created_at, c.id) > (%s, %s)
    ORDER BY c.created_at, c.id
    LIMIT %s
"""

# Minimum cursor values for the first batch
_CURSOR_MIN_TIMESTAMP = datetime(1970, 1, 1)
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def _upsert_party(
    conn: psycopg.Connection,
    raw_name: str,
    party_type: str | None = None,
) -> str:
    """Resolve a raw party name to a canonical party record."""
    canonical = _normalize_party_name(raw_name)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pa.party_id FROM party_aliases pa WHERE pa.raw_name = %s LIMIT 1",
            (raw_name,),
        )
        row = cur.fetchone()
        if row is not None:
            return str(row[0])

        cur.execute(
            "INSERT INTO parties (canonical_name, party_type) VALUES (%s, %s) RETURNING id",
            (canonical, party_type),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"INSERT INTO parties returned no row for name={canonical!r}"
            )
        party_id = str(row[0])

        cur.execute(
            """INSERT INTO party_aliases (party_id, raw_name, source, confidence, is_verified)
               VALUES (%s::uuid, %s, 'backfill', 1.0, FALSE)""",
            (party_id, raw_name),
        )
        return party_id


def _upsert_case_party(
    conn: psycopg.Connection,
    case_id: str,
    party_id: str,
    role: str,
) -> None:
    """Link a party to a case (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO case_parties (case_id, party_id, role)
               SELECT %s::uuid, %s::uuid, %s
               WHERE NOT EXISTS (
                   SELECT 1 FROM case_parties
                   WHERE case_id = %s::uuid AND party_id = %s::uuid AND role = %s
               )""",
            (case_id, party_id, role, case_id, party_id, role),
        )


def backfill_batch(
    conn: psycopg.Connection,
    batch_size: int = 100,
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID),
) -> tuple[int, int, tuple[datetime, str]]:
    """Process one batch of cases.  Returns (processed, updated, next_cursor)."""
    processed = 0
    updated = 0
    next_cursor = cursor

    with conn.cursor() as cur:
        cur.execute(FETCH_QUERY, (cursor[0], cursor[1], batch_size))
        rows = cur.fetchall()

    if not rows:
        return 0, 0, cursor

    for case_id, ruling_text, _court_id, created_at in rows:
        processed += 1
        next_cursor = (created_at, str(case_id))

        parties = extract_parties(ruling_text)
        if not parties:
            logger.debug("No parties extracted for case %s", case_id)
            continue

        try:
            batch_upsert_parties(
                conn, str(case_id), parties, alias_source="backfill"
            )
        except psycopg.errors.ProgramLimitExceeded:
            logger.warning(
                "Skipping parties with name too long for index (case %s)",
                case_id,
            )
            conn.rollback()
            continue

        logger.info(
            "Case %s -> %d parties",
            case_id,
            len(parties),
        )
        updated += 1

    return processed, updated, next_cursor


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
    cursor: tuple[datetime, str] = (_CURSOR_MIN_TIMESTAMP, _CURSOR_MIN_UUID)

    with psycopg.connect(dsn) as conn:
        while True:
            effective_batch = batch_size
            if limit is not None:
                remaining = limit - total_processed
                if remaining <= 0:
                    break
                effective_batch = min(batch_size, remaining)

            processed, updated, cursor = backfill_batch(conn, effective_batch, cursor)
            total_processed += processed
            total_updated += updated

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
                break

    return {
        "total_processed": total_processed,
        "total_updated": total_updated,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill party records from existing ruling text.",
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
        "Backfill complete: %d cases processed, %d updated with parties",
        stats["total_processed"],
        stats["total_updated"],
    )


if __name__ == "__main__":
    main()
