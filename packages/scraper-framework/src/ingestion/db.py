"""Postgres write operations for the ingestion worker.

All functions accept a psycopg Connection and operate within a caller-managed
transaction. The caller is responsible for commit/rollback.

Write order per event:
  1. upsert_court  — idempotent on court_code
  2. upsert_case   — idempotent on (court_id, case_number)
  3. insert_document — idempotent on documents.id (= scraper document_id UUID)
  4. resolve_judge  — resolve raw judge_name to canonical judge record
  5. insert_ruling   — skipped if document already exists (idempotent via step 3)
  6. upsert_case_judge — link case to judge in join table
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)

# Maximum length for party names.  PostgreSQL B-tree indexes have a row-size
# limit of 8191 bytes; real party names are well under 200 characters.  We
# truncate at 500 as a safety net — long enough for any legitimate name but
# short enough to never hit the index limit.
_MAX_PARTY_NAME_LENGTH = 500


def _truncate_party_name(name: str) -> str:
    """Truncate a party name to ``_MAX_PARTY_NAME_LENGTH`` characters.

    Logs a warning when truncation occurs so the issue is visible in
    monitoring without crashing the pipeline.
    """
    if len(name) <= _MAX_PARTY_NAME_LENGTH:
        return name
    logger.warning(
        "Truncating party name from %d to %d chars (first 80 chars: %r)",
        len(name),
        _MAX_PARTY_NAME_LENGTH,
        name[:80],
    )
    return name[:_MAX_PARTY_NAME_LENGTH]


def _strip_nul(value: str | None) -> str | None:
    """Remove NUL (0x00) bytes from a string.

    PostgreSQL text fields cannot contain NUL bytes. This helper is applied
    to all text parameters before they are passed to INSERT/UPDATE statements,
    protecting all callers from the ``ValueError: PostgreSQL text fields
    cannot contain NUL (0x00) bytes`` error.

    Returns ``None`` unchanged so it is safe to call on optional fields.
    """
    if value is None:
        return None
    return value.replace("\x00", "")


def _derive_court_code(state: str, county: str) -> str:
    """Derive a URL-safe court code from state + county.

    Examples:
        "CA", "Los Angeles"  -> "ca-los-angeles"
        "CA", "Orange"       -> "ca-orange"
        "CA", "San Bernardino" -> "ca-san-bernardino"
    """
    return f"{state.lower()}-{county.lower().replace(' ', '-')}"


def upsert_court(
    conn: psycopg.Connection,
    state: str,
    county: str,
    court_name: str,
    timezone: str = "America/Los_Angeles",
) -> str:
    """Upsert a court row and return its UUID.

    Uses court_code (derived from state + county) as the natural key.
    On conflict, updates court_name and timezone to keep the record fresh.
    """
    court_code = _derive_court_code(state, county)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO courts (state, county, court_name, court_code, timezone)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (court_code) DO UPDATE
                SET court_name = EXCLUDED.court_name,
                    timezone   = EXCLUDED.timezone
            RETURNING id
            """,
            (state, county, court_name, court_code, timezone),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"upsert_court returned no row for court_code={court_code!r}")
    court_id: str = str(row[0])
    logger.debug("upsert_court: court_code=%s id=%s", court_code, court_id)
    return court_id


def upsert_case(
    conn: psycopg.Connection,
    case_number: str,
    court_id: str,
    case_title: str | None = None,
    case_type: str | None = None,
) -> str:
    """Upsert a case row and return its UUID.

    Uses (court_id, case_number) as the natural key per the schema UNIQUE constraint.
    case_number_normalized strips whitespace and lowercases for search.

    ``case_title`` and ``case_type`` are set on INSERT and updated on conflict
    only when a non-NULL value is provided (COALESCE preserves existing values).
    """
    normalized = case_number.strip().lower().replace(" ", "").replace("-", "")
    case_title = _strip_nul(case_title)
    case_type = _strip_nul(case_type)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (case_number, case_number_normalized, court_id, case_title, case_type)
            VALUES (%s, %s, %s::uuid, %s, %s)
            ON CONFLICT (court_id, case_number) DO UPDATE
                SET case_title = COALESCE(EXCLUDED.case_title, cases.case_title),
                    case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)
            RETURNING id
            """,
            (case_number, normalized, court_id, case_title, case_type),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"upsert_case: could not retrieve case id for case_number={case_number!r}"
        )
    case_id: str = str(row[0])
    logger.debug(
        "upsert_case: case_number=%s case_title=%s case_type=%s id=%s",
        case_number,
        case_title,
        case_type,
        case_id,
    )
    return case_id


def insert_document(
    conn: psycopg.Connection,
    document_id: str,
    case_id: str,
    court_id: str,
    content_format: str,
    content_hash: str,
    s3_key: str | None,
    s3_bucket: str | None,
    source_url: str,
    scraper_id: str,
    captured_at: datetime,
    hearing_date: date | None,
) -> bool:
    """Upsert a document row using the scraper-assigned document_id as the PK.

    Returns True if a new row was inserted, False if it already existed.
    On conflict, updates mutable fields (hearing_date, case_id) while
    preserving immutable fields (s3_key, content_hash, captured_at).

    The scraper's document_id UUID is used as documents.id so that OpenSearch
    document IDs and rulings.document_id references all converge on the same key.
    """
    # Map ContentFormat string to PostgreSQL document_format enum value
    format_map = {"html": "html", "pdf": "pdf", "docx": "docx", "text": "txt"}
    pg_format = format_map.get(content_format.lower(), "html")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (
                id, case_id, court_id,
                document_type, format,
                s3_key, s3_bucket,
                content_hash, source_url, scraper_id,
                captured_at, hearing_date, status
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid,
                'ruling', %s::document_format,
                %s, %s,
                %s, %s, %s,
                %s, %s, 'active'
            )
            ON CONFLICT (id) DO UPDATE SET
                hearing_date = COALESCE(EXCLUDED.hearing_date, documents.hearing_date),
                case_id = EXCLUDED.case_id
            RETURNING (xmax = 0) AS is_new
            """,
            (
                document_id,
                case_id,
                court_id,
                pg_format,
                s3_key,
                s3_bucket,
                content_hash,
                source_url,
                scraper_id,
                captured_at,
                hearing_date,
            ),
        )
        row = cur.fetchone()
        inserted = bool(row[0]) if row else False
    logger.debug("insert_document: id=%s inserted=%s", document_id, inserted)
    return inserted


# Honorific prefixes to strip from judge names during normalization.
# Ordered so longer/more-specific prefixes match first.
# Anchored to the start of the string (^) so mid-name occurrences are not affected.
_HONORIFIC_PREFIX_RE = re.compile(
    r"^(?:The\s+)?Honorable\.?\s+|^Hon\.?\s+|^Judge:?\s+|^Arbitrator\s+",
    re.IGNORECASE,
)

# Generational suffixes that should appear at the end of a name.
# Used to detect and fix misplaced suffixes (e.g. "Jr. Edward B. Moreton").
_GENERATIONAL_SUFFIXES = {"Jr", "Jr.", "Sr", "Sr.", "II", "III", "IV"}

# Canonical forms for generational suffixes (preserves correct casing after title()).
_SUFFIX_CANONICAL: dict[str, str] = {
    "jr": "Jr.",
    "jr.": "Jr.",
    "sr": "Sr.",
    "sr.": "Sr.",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
}

# Maximum length for a valid judge name.  Real judge names are well under 80
# characters; anything longer is likely paragraph text captured by mistake.
_MAX_JUDGE_NAME_LENGTH = 80

# Pattern for detecting garbage/paragraph text in judge names.
# Matches ruling text fragments, party labels, year prefixes, and underscores.
# NOTE: We do NOT use a simple "period + space + capital" sentence-boundary
# pattern because it false-positives on middle initials like "A. Smith".
_GARBAGE_NAME_RE = re.compile(
    r"Moving\s+Party"  # ruling text fragment
    r"|Is\s+Ordered"  # ruling text fragment
    r"|Ordered\s+to"  # ruling text fragment
    r"|Plaintiff"  # party label
    r"|Defendant"  # party label
    r"|^\d{4}\s+"  # starts with a year
    r"|_{2,}",  # underscores (template placeholders)
)


def normalize_judge_name(raw_name: str) -> str | None:
    """Normalize a raw judge name string to a canonical form.

    Handles common court formats:
      - "LAST, FIRST M."    -> "First M. Last"
      - "FIRST M. LAST"     -> "First M. Last"
      - "  Smith,  John A. " -> "John A. Smith"
      - "Hon. Joseph B. Widman" -> "Joseph B. Widman"
      - "Judge Bobby P. Luna" -> "Bobby P. Luna"
      - "The Honorable Jane Doe" -> "Jane Doe"
      - "Arbitrator Howard B. Miller" -> "Howard B. Miller"
      - "Jr. Edward B. Moreton" -> "Edward B. Moreton Jr."

    Returns ``None`` for names that are clearly invalid (garbage text,
    too long, unicode junk, or single-word-only names without a first name).

    Steps:
      1. Strip unicode replacement characters (U+FFFD).
      2. Strip leading/trailing whitespace and collapse internal whitespace.
      3. Reject strings > 80 chars or containing garbage patterns.
      4. Strip honorific prefixes (Hon., Judge, The Honorable, Arbitrator).
      5. If the name contains a comma, treat the part before the comma as the
         last name and the part after as the first/middle.
      6. Move misplaced generational suffixes (Jr., Sr., III, etc.) to end.
      7. Title-case the result, preserving correct suffix casing.
    """
    # Strip unicode replacement characters
    name = raw_name.replace("\ufffd", "").strip()

    # Collapse multiple spaces to one
    name = re.sub(r"\s+", " ", name)

    # Reject empty or obviously invalid names
    if not name:
        return None

    # Reject names that are too long (likely paragraph text)
    if len(name) > _MAX_JUDGE_NAME_LENGTH:
        logger.warning(
            "Rejecting judge name exceeding %d chars: %r",
            _MAX_JUDGE_NAME_LENGTH,
            name[:80],
        )
        return None

    # Reject garbage patterns (sentences, ruling text fragments, etc.)
    if _GARBAGE_NAME_RE.search(name):
        logger.warning("Rejecting judge name with garbage pattern: %r", name[:80])
        return None

    # Strip honorific prefixes — order matters: "The Honorable" before "Honorable",
    # and "Hon." before "Hon" to avoid leaving a trailing period.
    name = _HONORIFIC_PREFIX_RE.sub("", name).strip()

    if not name:
        return None

    if "," in name:
        parts = name.split(",", 1)
        last = parts[0].strip()
        first = parts[1].strip()
        name = f"{first} {last}"

    # Move misplaced generational suffix from beginning to end.
    # E.g. "Jr. Edward B. Moreton" -> "Edward B. Moreton Jr."
    words = name.split()
    if len(words) >= 2 and words[0].rstrip(".") in {s.rstrip(".") for s in _GENERATIONAL_SUFFIXES}:
        suffix = words[0]
        name = " ".join(words[1:]) + " " + suffix

    # Title-case, but preserve periods (e.g. "A." stays "A.")
    name = name.title()

    # Fix generational suffixes that title() mangles (e.g. "Iii" -> "III")
    words = name.split()
    for i, word in enumerate(words):
        canonical = _SUFFIX_CANONICAL.get(word.lower().rstrip(","))
        if canonical is not None:
            # Preserve any trailing comma
            trail = "," if word.endswith(",") else ""
            words[i] = canonical + trail
    name = " ".join(words)

    return name


def _looks_like_valid_judge_name(name: str) -> bool:
    """Return True if *name* is plausibly a valid judge name.

    Rejects:
    - Single-word names (last name only, no first name)
    - Empty or whitespace-only strings

    This guard prevents garbage entries from being created in the judges table.
    """
    if not name or not name.strip():
        return False

    # Must have at least two words (first + last name)
    words = name.strip().split()
    if len(words) < 2:
        return False

    return True


def resolve_judge(
    conn: psycopg.Connection,
    raw_name: str,
    court_id: str,
) -> str | None:
    """Resolve a raw judge name to a canonical judge record, returning the judge UUID.

    Returns ``None`` if the raw name is invalid (garbage, too short, etc.)
    instead of creating a bad judge record.

    Lookup strategy (simple — exact normalized name match within the same court):
      1. Normalize the raw name and validate it.
      2. Search judge_aliases for a matching raw_name + court (via judge.court_id).
      3. If found, return the judge_id from the alias.
      4. If not found, create a new judge with canonical_name = normalized name,
         create a judge_alias linking raw_name to the new judge, and return the id.
    """
    raw_name = _strip_nul(raw_name) or raw_name
    canonical = normalize_judge_name(raw_name)

    # Reject invalid names before touching the database
    if canonical is None:
        logger.warning("resolve_judge: rejected invalid raw name %r", raw_name[:80])
        return None

    if not _looks_like_valid_judge_name(canonical):
        logger.warning(
            "resolve_judge: rejected single-word or invalid name %r (normalized: %r)",
            raw_name[:80],
            canonical,
        )
        return None

    with conn.cursor() as cur:
        # Look up existing alias for this raw name at this court
        cur.execute(
            """
            SELECT ja.judge_id
            FROM judge_aliases ja
            JOIN judges j ON j.id = ja.judge_id
            WHERE ja.raw_name = %s AND j.court_id = %s::uuid
            LIMIT 1
            """,
            (raw_name, court_id),
        )
        row = cur.fetchone()
        if row is not None:
            judge_id: str = str(row[0])
            logger.debug("resolve_judge: found existing alias for %r -> %s", raw_name, judge_id)
            return judge_id

        # No alias found — create new judge and alias
        cur.execute(
            """
            INSERT INTO judges (canonical_name, court_id)
            VALUES (%s, %s::uuid)
            RETURNING id
            """,
            (canonical, court_id),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"resolve_judge: INSERT INTO judges returned no row for name={canonical!r}"
            )
        judge_id = str(row[0])

        cur.execute(
            """
            INSERT INTO judge_aliases (judge_id, raw_name, source, confidence, is_verified)
            VALUES (%s::uuid, %s, 'scraper', 1.0, FALSE)
            """,
            (judge_id, raw_name),
        )

        logger.debug("resolve_judge: created new judge %s for %r", judge_id, raw_name)
        return judge_id


def upsert_case_judge(
    conn: psycopg.Connection,
    case_id: str,
    judge_id: str,
    hearing_date: date | None,
) -> None:
    """Link a case to a judge in the case_judges join table.

    Idempotent — ON CONFLICT on the (case_id, judge_id) PK does nothing.
    Sets is_current = TRUE and assigned_at to the hearing_date if provided.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO case_judges (case_id, judge_id, assigned_at, is_current)
            VALUES (%s::uuid, %s::uuid, %s, TRUE)
            ON CONFLICT (case_id, judge_id) DO NOTHING
            """,
            (case_id, judge_id, hearing_date),
        )
    logger.debug("upsert_case_judge: case_id=%s judge_id=%s", case_id, judge_id)


def insert_ruling(
    conn: psycopg.Connection,
    document_id: str,
    case_id: str,
    court_id: str,
    hearing_date: date,
    ruling_text: str | None,
    department: str | None,
    judge_id: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    ruling_text_html: str | None = None,
) -> None:
    """Upsert a ruling row linked to the document.

    On conflict (same document_id), updates extracted fields using COALESCE
    so that non-NULL existing values are preserved unless the new value is
    also non-NULL (allowing improved extraction to overwrite).

    Requires a UNIQUE constraint on rulings.document_id (migration 3).

    ``outcome`` must be a valid ``ruling_outcome`` enum value (e.g.
    ``"granted"``, ``"denied"``) or ``None``.  ``motion_type`` is free-text.

    ``ruling_text_html`` is LLM-formatted semantic HTML for display.
    """
    ruling_text = _strip_nul(ruling_text)
    ruling_text_html = _strip_nul(ruling_text_html)
    department = _strip_nul(department)
    outcome = _strip_nul(outcome)
    motion_type = _strip_nul(motion_type)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rulings (
                document_id, case_id, court_id, judge_id,
                hearing_date, ruling_text, ruling_text_html,
                department, is_tentative,
                outcome, motion_type
            )
            VALUES (
                %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::date, %s, %s,
                %s, TRUE,
                %s::ruling_outcome, %s
            )
            ON CONFLICT (document_id) DO UPDATE SET
                judge_id = COALESCE(EXCLUDED.judge_id, rulings.judge_id),
                outcome = COALESCE(EXCLUDED.outcome, rulings.outcome),
                motion_type = COALESCE(EXCLUDED.motion_type, rulings.motion_type),
                ruling_text = COALESCE(EXCLUDED.ruling_text, rulings.ruling_text),
                ruling_text_html = COALESCE(EXCLUDED.ruling_text_html, rulings.ruling_text_html),
                department = COALESCE(EXCLUDED.department, rulings.department)
            """,
            (
                document_id,
                case_id,
                court_id,
                judge_id,
                hearing_date,
                ruling_text,
                ruling_text_html,
                department,
                outcome,
                motion_type,
            ),
        )
    logger.debug("insert_ruling: document_id=%s", document_id)


def normalize_party_name(raw_name: str) -> str:
    """Normalize a raw party name string to a canonical form.

    Steps:
      1. Strip leading/trailing whitespace.
      2. Collapse internal whitespace.
      3. Title-case the result.
    """
    name = raw_name.strip()
    name = re.sub(r"\s+", " ", name)
    return name.title()


def upsert_party(
    conn: psycopg.Connection,
    raw_name: str,
    party_type: str | None = None,
) -> str:
    """Resolve a raw party name to a canonical party record, returning the party UUID.

    Lookup strategy (same pattern as ``resolve_judge``):
      1. Search party_aliases for a matching raw_name.
      2. If found, return the party_id from the alias.
      3. If not found, create a new party with canonical_name = normalized name,
         create a party_alias linking raw_name to the new party, and return the id.
    """
    raw_name = _strip_nul(raw_name) or raw_name
    raw_name = _truncate_party_name(raw_name)
    canonical = normalize_party_name(raw_name)

    with conn.cursor() as cur:
        # Look up existing alias for this raw name
        cur.execute(
            """
            SELECT pa.party_id
            FROM party_aliases pa
            WHERE pa.raw_name = %s
            LIMIT 1
            """,
            (raw_name,),
        )
        row = cur.fetchone()
        if row is not None:
            party_id: str = str(row[0])
            logger.debug("upsert_party: found alias for %r -> %s", raw_name, party_id)
            return party_id

        # No alias found — create new party and alias
        cur.execute(
            """
            INSERT INTO parties (canonical_name, party_type)
            VALUES (%s, %s)
            RETURNING id
            """,
            (canonical, party_type),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"upsert_party: INSERT INTO parties returned no row for name={canonical!r}"
            )
        party_id = str(row[0])

        cur.execute(
            """
            INSERT INTO party_aliases (party_id, raw_name, source, confidence, is_verified)
            VALUES (%s::uuid, %s, 'scraper', 1.0, FALSE)
            """,
            (party_id, raw_name),
        )

        logger.debug("upsert_party: created new party %s for %r", party_id, raw_name)
        return party_id


def upsert_case_party(
    conn: psycopg.Connection,
    case_id: str,
    party_id: str,
    role: str,
) -> None:
    """Link a party to a case in the case_parties join table.

    Idempotent — ON CONFLICT DO NOTHING on the (case_id, party_id, role)
    unique constraint prevents duplicate rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO case_parties (case_id, party_id, role)
            VALUES (%s::uuid, %s::uuid, %s)
            ON CONFLICT (case_id, party_id, role) DO NOTHING
            """,
            (case_id, party_id, role),
        )
    logger.debug("upsert_case_party: case_id=%s party_id=%s role=%s", case_id, party_id, role)


def batch_upsert_parties(
    conn: psycopg.Connection,
    case_id: str,
    parties_data: list[dict[str, str]],
    alias_source: str = "scraper",
) -> None:
    """Batch-upsert parties and link them to a case in O(1) queries.

    Replaces the N+1 pattern of calling ``upsert_party`` + ``upsert_case_party``
    in a loop.  For a document with *n* parties, this issues a constant number
    of queries (3-4) instead of 2n-3n individual queries.

    Steps:
      1. Collect all distinct (raw_name, canonical_name, role) tuples.
      2. Single ``SELECT ... WHERE raw_name = ANY(...)`` to find existing aliases.
      3. Single ``INSERT ... RETURNING`` for new parties + ``executemany`` for aliases.
      4. Single ``executemany`` for case_party links with ``ON CONFLICT DO NOTHING``.

    Parameters
    ----------
    conn : psycopg.Connection
        Open connection (caller manages transaction / commit).
    case_id : str
        UUID of the case to link parties to.
    parties_data : list[dict[str, str]]
        Each dict must have ``"name"`` and optionally ``"role"``.
    alias_source : str
        Value for the ``source`` column in ``party_aliases`` (default: ``"scraper"``).
    """
    if not parties_data:
        return

    # Deduplicate and collect valid entries
    entries: list[tuple[str, str, str]] = []  # (raw_name, canonical, role)
    seen: set[str] = set()
    for party_info in parties_data:
        raw_name = (_strip_nul(party_info.get("name", "")) or "").strip()
        if not raw_name:
            continue
        raw_name = _truncate_party_name(raw_name)
        role = party_info.get("role", "")
        canonical = normalize_party_name(raw_name)
        key = raw_name.lower()
        if key not in seen:
            seen.add(key)
            entries.append((raw_name, canonical, role))

    if not entries:
        return

    raw_names = [e[0] for e in entries]
    new_count = 0

    with conn.cursor() as cur:
        # Step 1: Batch-lookup existing aliases — single SELECT for all names
        cur.execute(
            "SELECT raw_name, party_id FROM party_aliases WHERE raw_name = ANY(%s)",
            (raw_names,),
        )
        existing: dict[str, str] = {row[0]: str(row[1]) for row in cur.fetchall()}

        # Step 2: Insert new parties + aliases for names not yet in the DB
        new_entries = [(rn, cn) for rn, cn, _role in entries if rn not in existing]
        new_count = len(new_entries)
        if new_entries:
            # Insert each new party and collect its ID.  We use executemany with
            # returning=True so psycopg3 pipelines the INSERTs in a single
            # network round-trip.
            cur.executemany(
                "INSERT INTO parties (canonical_name, party_type) VALUES (%s, NULL) RETURNING id",
                [(cn,) for _rn, cn in new_entries],
                returning=True,
            )
            # Collect RETURNING ids — one per statement in the executemany batch.
            new_ids: list[str] = []
            while True:
                row = cur.fetchone()
                if row is not None:
                    new_ids.append(str(row[0]))
                if not cur.nextset():
                    break

            # Insert aliases linking raw_name -> party_id
            cur.executemany(
                "INSERT INTO party_aliases "
                "(party_id, raw_name, source, confidence, is_verified) "
                "VALUES (%s::uuid, %s, %s, 1.0, FALSE) "
                "ON CONFLICT DO NOTHING",
                [(pid, rn, alias_source) for (rn, _cn), pid in zip(new_entries, new_ids)],
            )

            # Merge new IDs into the lookup dict
            for (rn, _cn), pid in zip(new_entries, new_ids):
                existing[rn] = pid

        # Step 3: Batch-insert case_party links
        link_params = []
        for raw_name, _canonical, role in entries:
            party_id = existing.get(raw_name)
            if party_id and role:
                link_params.append((case_id, party_id, role))

        if link_params:
            cur.executemany(
                "INSERT INTO case_parties (case_id, party_id, role) "
                "VALUES (%s::uuid, %s::uuid, %s) "
                "ON CONFLICT DO NOTHING",
                link_params,
            )

    logger.debug(
        "batch_upsert_parties: case_id=%s party_count=%d new=%d",
        case_id,
        len(entries),
        new_count,
    )
