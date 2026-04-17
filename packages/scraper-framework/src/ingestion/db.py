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

import hashlib
import logging
import re
import time
from datetime import date, datetime
from typing import TYPE_CHECKING

import psycopg.errors

from framework.party_utils import is_contaminated_party_name

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


def normalize_ruling_text_hash(text: str | None) -> str | None:
    """Compute a SHA-256 hash of normalized ruling text for deduplication.

    Normalization steps:
      1. Lowercase the text.
      2. Collapse all whitespace (including newlines, tabs) to single spaces.
      3. Strip leading and trailing whitespace.

    This ensures that rulings with identical content but different casing
    or whitespace produce the same hash, preventing semantic duplicates.

    Returns ``None`` if the input is ``None`` or empty after normalization.
    """
    if text is None:
        return None
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Legal acronyms that should be preserved when title-casing case titles.
# After str.title() these would become e.g. "Llc", "Llp" — we restore them.
_LEGAL_ACRONYMS: dict[str, str] = {
    "llc": "LLC",
    "llp": "LLP",
    "dba": "DBA",
    "inc": "Inc.",
    "inc.": "Inc.",
    "corp": "Corp.",
    "corp.": "Corp.",
    "ltd": "Ltd.",
    "ltd.": "Ltd.",
    "l.p.": "L.P.",
    "lp": "LP",
    "n.a.": "N.A.",
    "na": "NA",
    "pc": "PC",
    "p.c.": "P.C.",
    "pllc": "PLLC",
    "gp": "GP",
    "pa": "PA",
    "p.a.": "P.A.",
    "co": "Co.",
    "co.": "Co.",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
    "d/b/a": "d/b/a",
}


def _is_all_caps_title(title: str) -> bool:
    """Return True if a case title is ALL CAPS (ignoring the separator and short words).

    Splits on the "v." / "vs." / "vs" separator and checks whether all
    name parts are fully uppercased.  Single-character words and known
    legal acronyms are ignored — we only care about multi-word party names
    being in ALL CAPS.

    A title must have at least one part longer than 1 character to qualify.
    """
    # Split on the "v." / "vs." / "vs" separator
    parts = re.split(r"\s+[Vv][Ss]?\.?\s+", title)
    if not parts:
        return False

    has_alpha_part = False
    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        # Check if all alphabetic characters are uppercase
        alpha_chars = [c for c in stripped if c.isalpha()]
        if not alpha_chars:
            continue
        has_alpha_part = True
        if not all(c.isupper() for c in alpha_chars):
            return False

    return has_alpha_part


def normalize_case_title(title: str | None) -> str | None:
    """Normalize an ALL CAPS case title to title case.

    Detects ALL CAPS titles and converts them to title case while
    preserving legal acronyms (LLC, LLP, DBA, Inc., Corp., etc.)
    and the "v." separator.

    Mixed-case or already-normalized titles are returned unchanged
    (only whitespace normalization is applied).

    Returns ``None`` if the input is ``None`` or empty after stripping.

    Examples:
        "SMITH VS JONES"  -> "Smith v. Jones"
        "ACME LLC VS DOE" -> "Acme LLC v. Jones"
        "Smith v. Jones"  -> "Smith v. Jones"  (unchanged)
        None              -> None
    """
    if title is None:
        return None

    # Normalize whitespace
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None

    if not _is_all_caps_title(title):
        return title

    # Normalize the separator to "v." before title-casing
    title = re.sub(r"\s+[Vv][Ss]?\.?\s+", " v. ", title)

    # Split on "v." to title-case each side independently
    parts = title.split(" v. ")
    normalized_parts: list[str] = []

    for part in parts:
        # Apply title case
        tc = part.title()
        # Restore legal acronyms
        words = tc.split()
        restored: list[str] = []
        for word in words:
            # Strip trailing punctuation for lookup, then reattach
            stripped_word = word.rstrip(",;:.")
            trailing = word[len(stripped_word) :]
            canonical = _LEGAL_ACRONYMS.get(stripped_word.lower())
            if canonical is None:
                # Also try with the trailing punctuation included
                canonical = _LEGAL_ACRONYMS.get(word.lower().rstrip(",;:"))
            if canonical is not None:
                # Avoid double punctuation: if canonical already ends with
                # the same char as trailing, skip the trailing
                if trailing and canonical.endswith(trailing[0]):
                    restored.append(canonical + trailing[1:])
                else:
                    restored.append(canonical + trailing)
            else:
                restored.append(word)
        normalized_parts.append(" ".join(restored))

    return " v. ".join(normalized_parts)


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
    *,
    force_update: bool = False,
) -> str:
    """Upsert a case row and return its UUID.

    Uses (court_id, case_number) as the natural key per the schema UNIQUE constraint.
    case_number_normalized strips whitespace and lowercases for search.

    ``case_title`` and ``case_type`` are set on INSERT.  The behavior on
    conflict is controlled by ``force_update``:

    **Default (``force_update=False``) — live ingestion path (#2468):**
    ``case_title`` and ``case_type`` are preserved on conflict.  Once a
    non-NULL value exists for a case, it is NOT overwritten by a later
    upsert (even if the incoming value is non-NULL).  An incoming non-NULL
    value only fills in a currently-NULL column — it cannot silently replace
    an existing title/type.  This prevents upstream mis-routing bugs (e.g.
    fuzzy-match case-number rewrites, #2449) from propagating wrong titles
    across the DB.  Live ingestion (``ingestion.worker.IngestionWorker``)
    relies on this default.

    **``force_update=True`` — reingest / correction path (#2431):**
    The ON CONFLICT clause swaps the COALESCE argument order so the
    *incoming* value wins when non-NULL:
    ``COALESCE(EXCLUDED.case_title, cases.case_title)`` (and same for
    ``case_type``).  This allows ``scripts/reingest_from_s3.py`` to
    overwrite stuck/bad historical values (e.g. a misclassified
    ``case_type='criminal'`` that should be ``'family'`` after the #2368
    regex fix) while still refusing to erase a good existing value with an
    incoming NULL — COALESCE falls through to the preserved ``cases.*``
    column when the incoming EXCLUDED value is NULL.  Mirrors the
    ``force_update`` semantic already used by ``insert_document`` /
    ``insert_ruling`` (#2405).

    Returns the case UUID as a string.  To also retrieve the effective
    case_title (after COALESCE), use ``upsert_case_returning_title()``.
    """
    normalized = case_number.strip().lower().replace(" ", "").replace("-", "")
    case_title = normalize_case_title(_strip_nul(case_title))
    case_type = _strip_nul(case_type)
    # Build ON CONFLICT set clauses based on force_update mode.  The default
    # (preserve-existing) mode guards live ingestion against silent identity
    # rewrites (#2468).  The force_update mode lets reingest correct
    # previously-stuck values (#2431) while still never erasing a good
    # existing value with a NULL incoming — EXCLUDED=NULL → COALESCE falls
    # through to cases.case_title / cases.case_type.
    if force_update:
        case_title_clause = "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)"
        case_type_clause = "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)"
    else:
        case_title_clause = "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)"
        case_type_clause = "case_type  = COALESCE(cases.case_type, EXCLUDED.case_type)"

    sql = f"""
        INSERT INTO cases (case_number, case_number_normalized, court_id, case_title, case_type)
        VALUES (%s, %s, %s::uuid, %s, %s)
        ON CONFLICT (court_id, case_number) DO UPDATE
            SET {case_title_clause},
                {case_type_clause}
        RETURNING id
        """
    with conn.cursor() as cur:
        cur.execute(
            sql,
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


def upsert_case_returning_title(
    conn: psycopg.Connection,
    case_number: str,
    court_id: str,
    case_title: str | None = None,
    case_type: str | None = None,
    *,
    force_update: bool = False,
) -> tuple[str, str | None]:
    """Upsert a case row and return ``(case_id, effective_case_title)``.

    Identical to ``upsert_case()`` but also returns the effective
    ``case_title`` after the COALESCE.  This allows callers to discover
    an existing title that was preserved from a prior ruling — useful for
    the cross-case title lookup (#2006).

    **Default (``force_update=False``) — live ingestion path (#2468):**
    Preserves the existing ``case_title`` / ``case_type`` on conflict:
    once a non-NULL value exists for a case, a later upsert with a
    different non-NULL value will NOT overwrite it.  The incoming value
    only wins when the existing column is NULL.  This keeps the cross-case
    title lookup (#2006) working — the first ruling to supply a title
    wins and subsequent titleless rulings reuse it — while preventing
    upstream mis-routing (#2449) from silently rewriting case identities.

    **``force_update=True`` — reingest / correction path (#2431):**
    Swaps the COALESCE argument order so the incoming value wins when
    non-NULL (``COALESCE(EXCLUDED.case_title, cases.case_title)``), letting
    ``scripts/reingest_from_s3.py`` correct previously-stuck ``case_type``
    / ``case_title`` values after an extraction logic fix.  A NULL incoming
    EXCLUDED value still falls through to the preserved ``cases.*`` column
    — we never erase a good existing value with an incoming NULL.  Mirrors
    the ``force_update`` semantic already used by ``insert_document`` /
    ``insert_ruling`` (#2405).
    """
    normalized = case_number.strip().lower().replace(" ", "").replace("-", "")
    case_title = normalize_case_title(_strip_nul(case_title))
    case_type = _strip_nul(case_type)
    # See ``upsert_case`` for the full force_update rationale (#2431 / #2468).
    if force_update:
        case_title_clause = "case_title = COALESCE(EXCLUDED.case_title, cases.case_title)"
        case_type_clause = "case_type  = COALESCE(EXCLUDED.case_type, cases.case_type)"
    else:
        case_title_clause = "case_title = COALESCE(cases.case_title, EXCLUDED.case_title)"
        case_type_clause = "case_type  = COALESCE(cases.case_type, EXCLUDED.case_type)"

    sql = f"""
        INSERT INTO cases (case_number, case_number_normalized, court_id, case_title, case_type)
        VALUES (%s, %s, %s::uuid, %s, %s)
        ON CONFLICT (court_id, case_number) DO UPDATE
            SET {case_title_clause},
                {case_type_clause}
        RETURNING id, case_title
        """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (case_number, normalized, court_id, case_title, case_type),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"upsert_case: could not retrieve case id for case_number={case_number!r}"
        )
    case_id: str = str(row[0])
    effective_title: str | None = row[1] if len(row) > 1 else None
    logger.debug(
        "upsert_case_returning_title: case_number=%s case_title=%s effective_title=%s id=%s",
        case_number,
        case_title,
        effective_title,
        case_id,
    )
    return case_id, effective_title


def lookup_existing_case_title(
    conn: psycopg.Connection,
    case_number: str,
    court_id: str,
) -> str | None:
    """Look up an existing case_title for a (court_id, case_number) pair.

    When a new ruling for the same case arrives without party information,
    this allows re-using the title from a prior ruling.  Returns ``None``
    if the case does not exist or has no title.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT case_title FROM cases
            WHERE court_id = %s::uuid AND case_number = %s
            """,
            (court_id, case_number),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    title: str = str(row[0])
    logger.debug(
        "lookup_existing_case_title: case_number=%s title=%s",
        case_number,
        title,
    )
    return title


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
    *,
    force_update: bool = False,
) -> bool:
    """Upsert a document row using the scraper-assigned document_id as the PK.

    Returns True if a new row was inserted, False if it already existed.
    On conflict, updates mutable fields (hearing_date, case_id, last_seen_at)
    while preserving immutable fields (s3_key, content_hash, captured_at).

    ``last_seen_at`` is always set to the current timestamp on both insert and
    upsert, so it reflects the most recent time the scraper encountered this
    document — even when the content hasn't changed (dedup'd documents).

    The scraper's document_id UUID is used as documents.id so that OpenSearch
    document IDs and rulings.document_id references all converge on the same key.

    When ``force_update`` is True, the ON CONFLICT clause overwrites
    ``hearing_date`` with the new value unconditionally (instead of using
    ``COALESCE``).  This is the "force update" path used by
    ``reingest_from_s3.py`` to correct bad historical data — live ingestion
    uses the default ``force_update=False`` to preserve good existing dates
    when a new extraction happens to miss them (#2405).
    """
    # Map ContentFormat string to PostgreSQL document_format enum value
    format_map = {"html": "html", "pdf": "pdf", "docx": "docx", "text": "txt"}
    pg_format = format_map.get(content_format.lower(), "html")

    if force_update:
        hearing_date_clause = "hearing_date = EXCLUDED.hearing_date"
    else:
        hearing_date_clause = (
            "hearing_date = COALESCE(EXCLUDED.hearing_date, documents.hearing_date)"
        )

    sql = f"""
        INSERT INTO documents (
            id, case_id, court_id,
            document_type, format,
            s3_key, s3_bucket,
            content_hash, source_url, scraper_id,
            captured_at, last_seen_at, hearing_date, status
        )
        VALUES (
            %s::uuid, %s::uuid, %s::uuid,
            'ruling', %s::document_format,
            %s, %s,
            %s, %s, %s,
            %s, NOW(), %s, 'active'
        )
        ON CONFLICT (id) DO UPDATE SET
            {hearing_date_clause},
            case_id = EXCLUDED.case_id,
            last_seen_at = NOW()
        RETURNING (xmax = 0) AS is_new
    """

    with conn.cursor() as cur:
        cur.execute(
            sql,
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

# Regex to strip encoding artifacts from judge names.
# Targets specific problematic characters that result from encoding
# mismatches (e.g. Windows-1252 content served as UTF-8):
#   U+00BF  ¿  inverted question mark
#   U+00A0     non-breaking space (replaced with regular space, not stripped)
#   U+00AD     soft hyphen
#   U+0080-U+009F  C1 control characters (common Windows-1252 artifacts)
#   U+200B     zero-width space
# We handle NBSP separately (replace with space) to avoid merging words.
_ENCODING_ARTIFACT_RE = re.compile(r"[\u00bf\u00ad\u0080-\u009f\u200b]")
_NBSP_RE = re.compile(r"\u00a0")

# Known suffixes and initial patterns that are valid as a final word in a
# judge name, even though they are short (1-3 characters).
_VALID_SHORT_FINAL_WORDS = {
    "jr",
    "jr.",
    "sr",
    "sr.",
    "ii",
    "iii",
    "iv",
}

# Minimum length for the last word to be considered a plausible surname
# (unless it matches a known suffix pattern above).
_MIN_SURNAME_LENGTH = 3


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
    too long, unicode junk, encoding artifacts, or single-word-only names
    without a first name).

    Steps:
      1. Strip unicode replacement characters (U+FFFD) and encoding artifacts.
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

    # Strip encoding artifacts: remove specific problematic characters
    # that result from encoding mismatches (e.g. ¿, soft hyphen, C1 controls).
    # Replace NBSP with a regular space to avoid merging words.
    name = _NBSP_RE.sub(" ", name)
    name = _ENCODING_ARTIFACT_RE.sub("", name)

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
    - Clearly truncated names (last word is suspiciously short, e.g. "Mc", "Kin")

    This guard prevents garbage entries from being created in the judges table.
    """
    if not name or not name.strip():
        return False

    # Must have at least two words (first + last name)
    words = name.strip().split()
    if len(words) < 2:
        return False

    # Detect truncated names: if the last word is very short and isn't a
    # known suffix (Jr., Sr., II, III, IV) or an initial (single letter
    # with period like "A."), it's likely a truncated surname from
    # fixed-width HTML fields.
    last_word = words[-1]
    last_word_stripped = last_word.rstrip(".")

    # Single uppercase letter + optional period is an initial, not a surname.
    # This is fine — the name might be "John Smith A." (unusual but not truncated).
    # However, a name ending in just an initial suggests no surname at all,
    # so we still reject it.
    if len(last_word_stripped) == 1 and last_word_stripped.isalpha():
        logger.warning("Rejecting judge name ending in bare initial: %r", name)
        return False

    # Check if last word is a known valid short word (suffix)
    if last_word.lower() in _VALID_SHORT_FINAL_WORDS:
        return True

    # Reject if last word is too short to be a real surname
    if len(last_word_stripped) < _MIN_SURNAME_LENGTH:
        logger.warning(
            "Rejecting likely truncated judge name (last word %r too short): %r",
            last_word,
            name,
        )
        return False

    # Reject surnames with no vowels (likely truncated, e.g. "Bnk", "Sch")
    # unless it's at least 4 chars (some real surnames lack vowels: "Ng", "Lv"
    # — but those are 2-char and caught above; 4+ char consonant-only is suspicious)
    vowels = set("aeiouAEIOU")
    if len(last_word_stripped) < 5 and not any(c in vowels for c in last_word_stripped):
        logger.warning(
            "Rejecting likely truncated judge name (last word %r has no vowels): %r",
            last_word,
            name,
        )
        return False

    return True


def _strip_middle_initials(name: str) -> str:
    """Remove middle initials from a name for near-duplicate comparison.

    Converts "Carmen R. Luege" -> "Carmen Luege" and
    "John A. B. Smith" -> "John Smith".

    A middle initial is defined as a single uppercase letter optionally
    followed by a period, appearing between the first and last words.
    """
    words = name.split()
    if len(words) <= 2:
        return name
    # Keep first and last words; filter out middle initials
    middle = words[1:-1]
    kept = [w for w in middle if not (len(w.rstrip(".")) == 1 and w[0].isupper())]
    return " ".join([words[0], *kept, words[-1]])


# ---------------------------------------------------------------------------
# Roster-based judge name matching
# ---------------------------------------------------------------------------

# Maximum Levenshtein distance for fuzzy name matching against the roster.
_MAX_ROSTER_LEVENSHTEIN = 3

# Minimum confidence for a roster match to be used in resolve_judge.
# Matches below this threshold are ignored to prevent merging distinct
# judges with similar names (e.g. "Jane Smythe" != "Jane Smith").
_ROSTER_MATCH_CONFIDENCE_THRESHOLD = 0.85


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings.

    Uses a simple dynamic-programming implementation (O(n*m)).
    """
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _normalize_for_comparison(name: str) -> str:
    """Normalize a judge name for fuzzy comparison.

    Strips ALL single-letter initials (not just middle ones), lowercases,
    removes periods/punctuation, and sorts words alphabetically so initial
    placement doesn't matter.

    Examples:
      "H. Shaina Colover"  -> "colover shaina"
      "Shaina H. Colover"  -> "colover shaina"
      "Jennifer Mccarthey" -> "jennifer mccarthey"
      "Andre De La Cruz"   -> "andre cruz de la"
      "De La Cruz"         -> "cruz de la"
    """
    # Lowercase and remove periods first
    cleaned = name.lower().replace(".", "")
    # Split into words, remove all single-letter words (initials)
    words = [w for w in cleaned.split() if len(w) > 1]
    # Sort for position-invariant comparison
    words.sort()
    return " ".join(words)


def fuzzy_match_roster_name(
    candidate_name: str,
    roster_names: list[str],
    *,
    max_distance: int = _MAX_ROSTER_LEVENSHTEIN,
) -> tuple[str, float] | None:
    """Match a judge name against a list of roster names using fuzzy matching.

    Uses a multi-strategy approach:
      1. Exact match (after normalization)
      2. Sorted-word match (catches initial placement variants)
      3. Levenshtein distance on normalized forms (catches typos)
      4. Subset matching (catches partial names like "De La Cruz")

    Parameters
    ----------
    candidate_name : str
        The judge name to match (already normalized via normalize_judge_name).
    roster_names : list[str]
        List of canonical judge names from the roster.
    max_distance : int
        Maximum Levenshtein distance for fuzzy matching. Default is 3.

    Returns
    -------
    tuple[str, float] | None
        A tuple of (matched_roster_name, confidence) if a match is found,
        or None if no match. Confidence ranges from 0.0 to 1.0.
    """
    if not candidate_name or not roster_names:
        return None

    candidate_lower = candidate_name.lower().strip()
    candidate_normalized = _normalize_for_comparison(candidate_name)
    candidate_words = set(candidate_normalized.split())

    best_match: tuple[str, float] | None = None

    for roster_name in roster_names:
        roster_lower = roster_name.lower().strip()

        # Strategy 1: Exact match (case-insensitive)
        if candidate_lower == roster_lower:
            return (roster_name, 1.0)

        roster_normalized = _normalize_for_comparison(roster_name)
        roster_words = set(roster_normalized.split())

        # Strategy 2: Sorted-word match (catches "H. Shaina Colover" vs "Shaina H. Colover")
        if candidate_normalized == roster_normalized:
            confidence = 0.95
            if best_match is None or confidence > best_match[1]:
                best_match = (roster_name, confidence)

        # Strategy 3: Levenshtein distance on normalized forms (catches typos)
        dist = _levenshtein_distance(candidate_normalized, roster_normalized)
        if dist <= max_distance:
            # Scale confidence inversely with distance
            # dist=1 -> 0.85, dist=2 -> 0.75, dist=3 -> 0.65
            confidence = max(0.5, 0.95 - dist * 0.1)
            if best_match is None or confidence > best_match[1]:
                best_match = (roster_name, confidence)

        # Strategy 4: Subset matching (catches partial names)
        # If one name's words are a proper subset of the other's,
        # and the subset has at least 2 words (to avoid false positives
        # on single common last names), treat as a match.
        if len(candidate_words) >= 2 and len(roster_words) >= 2:
            if candidate_words < roster_words:
                # candidate is a subset of roster (e.g. "De La Cruz" vs "Andre De La Cruz")
                confidence = 0.8 * (len(candidate_words) / len(roster_words))
                if best_match is None or confidence > best_match[1]:
                    best_match = (roster_name, confidence)
            elif roster_words < candidate_words:
                # roster is a subset of candidate (rare but possible)
                confidence = 0.8 * (len(roster_words) / len(candidate_words))
                if best_match is None or confidence > best_match[1]:
                    best_match = (roster_name, confidence)

    return best_match


# In-memory cache for roster name lookups.  Keyed by court UUID, values are
# ``(monotonic_timestamp, names_list)`` tuples.  Roster data changes at most
# daily (via ``fetch_rosters.py``), so a 5-minute TTL eliminates 20,000+
# redundant queries during a full rebuild while keeping the window for stale
# data acceptably short.
_roster_cache: dict[str, tuple[float, list[str]]] = {}
_ROSTER_CACHE_TTL = 300  # seconds (5 minutes)


def clear_roster_cache(court_id: str | None = None) -> None:
    """Clear the roster name cache.

    Parameters
    ----------
    court_id : str or None
        If provided, clear only the entry for this court UUID.
        If ``None``, clear the entire cache.
    """
    if court_id is None:
        _roster_cache.clear()
    else:
        _roster_cache.pop(court_id, None)


def _get_roster_names(
    conn: psycopg.Connection,
    court_id: str,
) -> list[str]:
    """Get unique judge names from the latest court directory snapshot.

    Results are cached per *court_id* with a short TTL to avoid redundant
    queries when ``resolve_judge()`` is called once per document event during
    bulk processing.

    Parameters
    ----------
    conn : psycopg.Connection
        Database connection.
    court_id : str
        The court UUID (from the judges table).

    Returns
    -------
    list[str]
        List of unique judge names from the roster. Empty list if no
        snapshot exists or the court_code cannot be determined.
    """
    now = time.monotonic()
    cached = _roster_cache.get(court_id)
    if cached is not None and now - cached[0] < _ROSTER_CACHE_TTL:
        return cached[1]

    # Get the court_code for this court UUID, then convert to snapshot format
    with conn.cursor() as cur:
        cur.execute(
            "SELECT court_code FROM courts WHERE id = %s::uuid LIMIT 1",
            (court_id,),
        )
        row = cur.fetchone()
    if row is None:
        _roster_cache[court_id] = (now, [])
        return []

    court_code: str = row[0]
    # Convert court_code format (ca-los-angeles) to snapshot format (ca_los_angeles)
    snapshot_court_id = court_code.replace("-", "_")

    # Get the latest snapshot mapping for this court
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping FROM court_directory_snapshots
            WHERE court_id = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (snapshot_court_id,),
        )
        row = cur.fetchone()

    if row is None or row[0] is None:
        _roster_cache[court_id] = (now, [])
        return []

    import json

    mapping_data = row[0]
    mapping: dict[str, str]
    if isinstance(mapping_data, dict):
        mapping = mapping_data
    else:
        try:
            mapping = json.loads(mapping_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Failed to parse court directory mapping for roster lookup",
                extra={"court_id": court_id},
            )
            _roster_cache[court_id] = (now, [])
            return []

    # Return unique judge names from the mapping values
    result = list(set(mapping.values()))
    _roster_cache[court_id] = (now, result)
    return result


def resolve_judge_from_department(
    conn: psycopg.Connection,
    court_id: str,
    department: str,
    hearing_date: date | None = None,
) -> str | None:
    """Look up a judge name from the court directory snapshot for a given department.

    Uses the ``court_directory_snapshots`` table to find the judge assigned to
    a department.  When ``hearing_date`` is provided, selects the snapshot
    closest to (but not after) that date for historical accuracy.  Otherwise
    uses the most recent snapshot.

    This is the universal dept-to-judge fallback that works for ALL counties
    with roster data, replacing the LA-specific hardcoded lookup.  It fires
    during both normal ingestion and rebuilds, regardless of ``_llm_extracted``
    status.

    Parameters
    ----------
    conn : psycopg.Connection
        Database connection.
    court_id : str
        The court UUID (from the courts table).
    department : str
        The department string to look up.
    hearing_date : date | None
        The hearing date for historical snapshot selection.

    Returns
    -------
    str | None
        The judge name from the roster, or ``None`` if no match found.
    """
    import json

    # Get the court_code for this court UUID
    with conn.cursor() as cur:
        cur.execute(
            "SELECT court_code FROM courts WHERE id = %s::uuid LIMIT 1",
            (court_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None

    court_code: str = row[0]
    # Convert court_code format (ca-los-angeles) to snapshot format (ca_los_angeles)
    snapshot_court_id = court_code.replace("-", "_")

    # Get the appropriate snapshot
    with conn.cursor() as cur:
        if hearing_date is not None:
            cur.execute(
                """
                SELECT mapping FROM court_directory_snapshots
                WHERE court_id = %s AND captured_at <= %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (snapshot_court_id, hearing_date),
            )
        else:
            cur.execute(
                """
                SELECT mapping FROM court_directory_snapshots
                WHERE court_id = %s
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (snapshot_court_id,),
            )
        row = cur.fetchone()

    if row is None or row[0] is None:
        return None

    mapping_data = row[0]
    mapping: dict[str, str]
    if isinstance(mapping_data, dict):
        mapping = mapping_data
    else:
        try:
            mapping = json.loads(mapping_data)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Failed to parse court directory mapping for dept lookup",
                extra={"court_id": court_id, "department": department},
            )
            return None

    # Try exact department match first, then case-insensitive
    if department in mapping:
        return mapping[department]
    dept_lower = department.lower().strip()
    for key, value in mapping.items():
        if key.lower().strip() == dept_lower:
            return value
    return None


def resolve_judge(
    conn: psycopg.Connection,
    raw_name: str,
    court_id: str,
) -> str | None:
    """Resolve a raw judge name to a canonical judge record, returning the judge UUID.

    Returns ``None`` if the raw name is invalid (garbage, too short, etc.)
    instead of creating a bad judge record.

    Lookup strategy:
      1. Normalize the raw name and validate it.
      2. Search judge_aliases for a matching raw_name + court (via judge.court_id).
      3. If found, return the judge_id from the alias.
      4. Check judges table for exact (canonical_name, court_id) match.
      5. If found, create an alias and return the existing judge_id.
      6. Check for near-duplicates (missing middle initial) at the same court.
      7. If near-match found, link to existing judge (prefer more complete name).
      8. Fuzzy-match against court directory roster names (#2140).
         If a roster match is found AND a judge with that canonical name exists,
         link via alias. If no existing judge, use the roster canonical name
         for the new record.
      9. If no match at all, create a new judge with ON CONFLICT handling.
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
        # Step 1: Look up existing alias for this raw name at this court
        # (case-insensitive to prevent duplicate judge records when the
        # same name arrives in different casing — see #1453)
        cur.execute(
            """
            SELECT ja.judge_id
            FROM judge_aliases ja
            JOIN judges j ON j.id = ja.judge_id
            WHERE LOWER(ja.raw_name) = LOWER(%s) AND j.court_id = %s::uuid
            LIMIT 1
            """,
            (raw_name, court_id),
        )
        row = cur.fetchone()
        if row is not None:
            judge_id: str = str(row[0])
            logger.debug("resolve_judge: found existing alias for %r -> %s", raw_name, judge_id)
            return judge_id

        # Step 2: Check judges table for exact canonical_name + court_id match
        cur.execute(
            """
            SELECT id FROM judges
            WHERE canonical_name = %s AND court_id = %s::uuid
            LIMIT 1
            """,
            (canonical, court_id),
        )
        row = cur.fetchone()
        if row is not None:
            judge_id = str(row[0])
            # Create alias linking this raw_name to the existing judge
            cur.execute(
                """
                INSERT INTO judge_aliases (judge_id, raw_name, source, confidence, is_verified)
                VALUES (%s::uuid, %s, 'scraper', 1.0, FALSE)
                ON CONFLICT DO NOTHING
                """,
                (judge_id, raw_name),
            )
            logger.debug(
                "resolve_judge: found existing judge by canonical name %r -> %s",
                canonical,
                judge_id,
            )
            return judge_id

        # Step 3: Near-duplicate detection — check for names that differ
        # only by middle initial(s) at the same court.
        candidate_stripped = _strip_middle_initials(canonical)
        cur.execute(
            """
            SELECT id, canonical_name FROM judges
            WHERE court_id = %s::uuid
            """,
            (court_id,),
        )
        existing_judges = cur.fetchall()
        for existing_id, existing_canonical in existing_judges:
            existing_stripped = _strip_middle_initials(existing_canonical)
            if candidate_stripped.lower() == existing_stripped.lower():
                judge_id = str(existing_id)
                # If the new name is more complete (longer), update canonical
                if len(canonical) > len(existing_canonical):
                    cur.execute(
                        """
                        UPDATE judges SET canonical_name = %s, updated_at = NOW()
                        WHERE id = %s::uuid
                        """,
                        (canonical, judge_id),
                    )
                    logger.info(
                        "resolve_judge: updated canonical name %r -> %r for judge %s",
                        existing_canonical,
                        canonical,
                        judge_id,
                    )
                # Create alias for the new raw name
                cur.execute(
                    """
                    INSERT INTO judge_aliases (judge_id, raw_name, source, confidence, is_verified)
                    VALUES (%s::uuid, %s, 'scraper', 0.9, FALSE)
                    ON CONFLICT DO NOTHING
                    """,
                    (judge_id, raw_name),
                )
                logger.debug(
                    "resolve_judge: near-duplicate match %r ~ %r -> %s",
                    canonical,
                    existing_canonical,
                    judge_id,
                )
                return judge_id

        # Step 3b: Roster-based matching — check the court directory
        # snapshot for a fuzzy match against official judge names (#2140).
        # This catches typos, initial placement variants, and partial names
        # that steps 2-3 miss.
        roster_names = _get_roster_names(conn, court_id)
        if roster_names:
            roster_match = fuzzy_match_roster_name(canonical, roster_names)
            if roster_match is not None:
                roster_canonical, confidence = roster_match
                # Only use high-confidence matches to avoid merging
                # distinct judges with similar names (#2140)
                if confidence < _ROSTER_MATCH_CONFIDENCE_THRESHOLD:
                    logger.info(
                        "resolve_judge: roster match %r -> %r has low "
                        "confidence %.2f (threshold=%.2f), skipping",
                        canonical,
                        roster_canonical,
                        confidence,
                        _ROSTER_MATCH_CONFIDENCE_THRESHOLD,
                    )
                    roster_match = None
            if roster_match is not None:
                roster_canonical, confidence = roster_match
                # Normalize the roster name through the same pipeline
                roster_normalized = normalize_judge_name(roster_canonical)
                if roster_normalized is not None:
                    # Check if a judge with the roster's canonical name exists
                    cur.execute(
                        """
                        SELECT id FROM judges
                        WHERE canonical_name = %s AND court_id = %s::uuid
                        LIMIT 1
                        """,
                        (roster_normalized, court_id),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        judge_id = str(row[0])
                        # Link this raw name as an alias of the roster-matched judge
                        cur.execute(
                            """
                            INSERT INTO judge_aliases
                                (judge_id, raw_name, source, confidence, is_verified)
                            VALUES (%s::uuid, %s, 'roster_match', %s, FALSE)
                            ON CONFLICT DO NOTHING
                            """,
                            (judge_id, raw_name, confidence),
                        )
                        logger.info(
                            "resolve_judge: roster match %r -> %r (confidence=%.2f) -> %s",
                            canonical,
                            roster_normalized,
                            confidence,
                            judge_id,
                        )
                        return judge_id
                    else:
                        # No existing judge with the roster name — use roster name
                        # as the canonical name instead of the raw-derived name.
                        canonical = roster_normalized
                        logger.info(
                            "resolve_judge: using roster canonical name %r "
                            "instead of %r (confidence=%.2f)",
                            roster_normalized,
                            normalize_judge_name(raw_name),
                            confidence,
                        )

        # Step 4: No match found — create new judge with ON CONFLICT
        # handling for the UNIQUE constraint (race condition safety net).
        cur.execute(
            """
            INSERT INTO judges (canonical_name, court_id)
            VALUES (%s, %s::uuid)
            ON CONFLICT (canonical_name, court_id) DO UPDATE
                SET updated_at = NOW()
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
            ON CONFLICT DO NOTHING
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
    hearing_date: date | None,
    ruling_text: str | None,
    department: str | None,
    judge_id: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    ruling_text_html: str | None = None,
    summary: str | None = None,
    summary_model: str | None = None,
    summary_generated_at: datetime | None = None,
    *,
    force_update: bool = False,
) -> None:
    """Upsert a ruling row linked to the document, with content-based dedup.

    Deduplication has two layers:

    1. **Exact match** (``ON CONFLICT (document_id)``): same scraper document
       produces the same deterministic document_id, so the INSERT becomes an
       UPDATE of extracted fields.  This is the fast path.

    2. **Content match** (savepoint + UniqueViolation on ``ruling_text_hash``):
       if a ruling with the same ``case_id`` and normalized text hash already
       exists (from a prior scrape that produced different raw bytes but
       semantically identical text), the unique index
       ``uq_rulings_case_text_hash`` raises a UniqueViolation.  We catch it
       inside a savepoint and fall back to an UPDATE of the existing row.
       The hash is a SHA-256 of the lowercased, whitespace-collapsed ruling
       text (see ``normalize_ruling_text_hash``).

    Requires a UNIQUE constraint on ``rulings.document_id`` (migration 3) and
    a partial UNIQUE index on ``(case_id, ruling_text_hash)`` where
    ``ruling_text_hash IS NOT NULL`` (migration 11).

    ``outcome`` must be a valid ``ruling_outcome`` enum value (e.g.
    ``"granted"``, ``"denied"``) or ``None``.  ``motion_type`` is free-text.

    ``ruling_text_html`` is LLM-formatted semantic HTML for display.

    ``summary``, ``summary_model``, and ``summary_generated_at`` are
    AI-generated plain-English summaries produced at ingestion time
    (gated behind ``ENABLE_RULING_SUMMARIZATION``).

    **force_update (keyword-only, default False):**
    Controls COALESCE vs direct-overwrite semantics on conflict.  When False
    (live-ingestion default), the ON CONFLICT clause uses
    ``COALESCE(EXCLUDED.col, rulings.col)`` so a re-ingest that happens to
    miss a field does not erase the previously-good value.  When True
    (reingest path), the ON CONFLICT clause overwrites ``case_id``,
    ``judge_id``, ``hearing_date``, ``outcome``, ``motion_type``, and
    ``department`` with ``EXCLUDED.*`` unconditionally — including NULL —
    so reingest can correct bad historical data (#2405).  Text fields
    (``ruling_text``, ``ruling_text_html``, ``summary``) always use
    COALESCE regardless of force_update: we never erase a good ruling text
    just because a re-extraction happened to miss it.
    """
    ruling_text = _strip_nul(ruling_text)
    ruling_text_html = _strip_nul(ruling_text_html)
    department = _strip_nul(department)
    outcome = _strip_nul(outcome)
    motion_type = _strip_nul(motion_type)
    summary = _strip_nul(summary)

    text_hash = normalize_ruling_text_hash(ruling_text)

    # Build ON CONFLICT set clauses based on force_update mode.  In the default
    # (live-ingestion) mode, structured fields use COALESCE so a miss during
    # re-extraction does not erase the previously-good value.  In force_update
    # mode (reingest), case_id / judge_id / hearing_date / outcome /
    # motion_type / department are overwritten with EXCLUDED.* unconditionally
    # so reingest can correct bad historical data (#2405).  Text and summary
    # fields always use COALESCE — we never erase good ruling text just
    # because a re-extraction happened to miss it.
    if force_update:
        conflict_case_id = "case_id = EXCLUDED.case_id"
        conflict_judge_id = "judge_id = EXCLUDED.judge_id"
        conflict_hearing_date = "hearing_date = EXCLUDED.hearing_date"
        conflict_outcome = "outcome = EXCLUDED.outcome"
        conflict_motion_type = "motion_type = EXCLUDED.motion_type"
        conflict_department = "department = EXCLUDED.department"
    else:
        conflict_case_id = "case_id = COALESCE(EXCLUDED.case_id, rulings.case_id)"
        conflict_judge_id = "judge_id = COALESCE(EXCLUDED.judge_id, rulings.judge_id)"
        conflict_hearing_date = (
            "hearing_date = COALESCE(EXCLUDED.hearing_date, rulings.hearing_date)"
        )
        conflict_outcome = "outcome = COALESCE(EXCLUDED.outcome, rulings.outcome)"
        conflict_motion_type = "motion_type = COALESCE(EXCLUDED.motion_type, rulings.motion_type)"
        conflict_department = "department = COALESCE(EXCLUDED.department, rulings.department)"

    insert_sql = f"""
        INSERT INTO rulings (
            document_id, case_id, court_id, judge_id,
            hearing_date, ruling_text, ruling_text_html,
            department, is_tentative,
            outcome, motion_type,
            summary, summary_model, summary_generated_at,
            ruling_text_hash
        )
        VALUES (
            %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::date, %s, %s,
            %s, TRUE,
            %s::ruling_outcome, %s,
            %s, %s, %s,
            %s
        )
        ON CONFLICT (document_id) DO UPDATE SET
            {conflict_case_id},
            {conflict_judge_id},
            {conflict_hearing_date},
            {conflict_outcome},
            {conflict_motion_type},
            {conflict_department},
            ruling_text = COALESCE(EXCLUDED.ruling_text, rulings.ruling_text),
            ruling_text_html = COALESCE(
                EXCLUDED.ruling_text_html, rulings.ruling_text_html
            ),
            summary = COALESCE(EXCLUDED.summary, rulings.summary),
            summary_model = COALESCE(EXCLUDED.summary_model, rulings.summary_model),
            summary_generated_at = COALESCE(
                EXCLUDED.summary_generated_at, rulings.summary_generated_at
            ),
            ruling_text_hash = COALESCE(EXCLUDED.ruling_text_hash, rulings.ruling_text_hash)
    """

    # Attempt the INSERT inside a savepoint so that a UniqueViolation from the
    # content-hash index (uq_rulings_case_text_hash) does not abort the
    # surrounding transaction.  The ON CONFLICT (document_id) handles exact
    # document_id matches.  A content-hash collision (different document_id,
    # same normalized text for the same case) triggers the savepoint rollback
    # and falls through to the UPDATE below.
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT ruling_insert")
            cur.execute(
                insert_sql,
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
                    summary,
                    summary_model,
                    summary_generated_at,
                    text_hash,
                ),
            )
            cur.execute("RELEASE SAVEPOINT ruling_insert")
        logger.debug("insert_ruling: document_id=%s ruling_text_hash=%s", document_id, text_hash)
        return
    except psycopg.errors.UniqueViolation as exc:
        # Roll back the savepoint regardless of which constraint was violated.
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT ruling_insert")

        # Only handle the content-hash constraint; re-raise anything else.
        # When constraint_name is None (e.g. in tests without a real PG
        # connection), we assume it's the expected constraint since the
        # ON CONFLICT (document_id) clause handles the other unique index.
        constraint = getattr(exc.diag, "constraint_name", None)
        if constraint is not None and constraint != "uq_rulings_case_text_hash":
            raise

        # Content-hash collision — a ruling with the same normalized text
        # already exists for this case under a different document_id.
        # Update the existing row instead.

    # Fallback: update the existing ruling that has the same content hash.
    if text_hash is not None:
        # Mirror the force_update semantics on the fallback UPDATE so reingest
        # can correct bad judge_id / hearing_date / outcome / motion_type /
        # department on rulings that matched by content hash rather than by
        # document_id.  Text/summary fields always keep COALESCE.
        if force_update:
            judge_id_clause = "judge_id = %s::uuid"
            hearing_date_clause = "hearing_date = %s::date"
            outcome_clause = "outcome = %s::ruling_outcome"
            motion_type_clause = "motion_type = %s"
            department_clause = "department = %s"
        else:
            judge_id_clause = "judge_id = COALESCE(%s::uuid, judge_id)"
            hearing_date_clause = "hearing_date = COALESCE(%s::date, hearing_date)"
            outcome_clause = "outcome = COALESCE(%s::ruling_outcome, outcome)"
            motion_type_clause = "motion_type = COALESCE(%s, motion_type)"
            department_clause = "department = COALESCE(%s, department)"

        update_sql = f"""
            UPDATE rulings SET
                {judge_id_clause},
                {hearing_date_clause},
                {outcome_clause},
                {motion_type_clause},
                ruling_text = COALESCE(%s, ruling_text),
                ruling_text_html = COALESCE(%s, ruling_text_html),
                {department_clause},
                summary = COALESCE(%s, summary),
                summary_model = COALESCE(%s, summary_model),
                summary_generated_at = COALESCE(%s, summary_generated_at),
                updated_at = NOW()
            WHERE case_id = %s::uuid AND ruling_text_hash = %s
        """

        with conn.cursor() as cur:
            cur.execute(
                update_sql,
                (
                    judge_id,
                    hearing_date,
                    outcome,
                    motion_type,
                    ruling_text,
                    ruling_text_html,
                    department,
                    summary,
                    summary_model,
                    summary_generated_at,
                    case_id,
                    text_hash,
                ),
            )
            if cur.rowcount == 0:
                logger.warning(
                    "insert_ruling: fallback UPDATE matched 0 rows — "
                    "conflicting row may have been deleted concurrently "
                    "(document_id=%s, case_id=%s, text_hash=%s)",
                    document_id,
                    case_id,
                    text_hash,
                )
        logger.info(
            "insert_ruling: content-hash dedup — updated existing ruling "
            "instead of inserting duplicate (document_id=%s, case_id=%s)",
            document_id,
            case_id,
        )


def insert_document_and_ruling(
    conn: psycopg.Connection,
    *,
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
    ruling_text: str | None = None,
    ruling_text_html: str | None = None,
    department: str | None = None,
    judge_id: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    summary: str | None = None,
    summary_model: str | None = None,
    summary_generated_at: datetime | None = None,
    force_update: bool = False,
) -> bool:
    """Insert a document and its associated ruling in a single call.

    Encapsulates the ``insert_document`` + ``insert_ruling`` pair so that the
    same ``document_id`` is guaranteed to be passed to both operations.  This
    prevents the FK divergence fixed in #1775 by construction: callers no
    longer need to remember to thread the same ID through two separate calls.

    Parameters match the union of ``insert_document`` and ``insert_ruling``.
    ``hearing_date`` may be ``None`` — the ruling is still inserted with a
    NULL hearing_date.  A missing date is preferable to a missing ruling
    (#2215).

    ``force_update`` (default False) is threaded through to both
    ``insert_document`` and ``insert_ruling``.  When True, ON CONFLICT
    clauses overwrite ``case_id``, ``judge_id``, ``hearing_date``,
    ``outcome``, ``motion_type``, and ``department`` with ``EXCLUDED.*``
    unconditionally (including NULL) so reingest can correct bad historical
    data (#2405).  Text and summary fields always keep COALESCE.

    Returns ``True`` if the document row was newly inserted, ``False`` if it
    already existed (same semantics as ``insert_document``).
    """
    is_new = insert_document(
        conn,
        document_id=document_id,
        case_id=case_id,
        court_id=court_id,
        content_format=content_format,
        content_hash=content_hash,
        s3_key=s3_key,
        s3_bucket=s3_bucket,
        source_url=source_url,
        scraper_id=scraper_id,
        captured_at=captured_at,
        hearing_date=hearing_date,
        force_update=force_update,
    )

    insert_ruling(
        conn,
        document_id=document_id,
        case_id=case_id,
        court_id=court_id,
        hearing_date=hearing_date,
        ruling_text=ruling_text,
        ruling_text_html=ruling_text_html,
        department=department,
        judge_id=judge_id,
        outcome=outcome,
        motion_type=motion_type,
        summary=summary,
        summary_model=summary_model,
        summary_generated_at=summary_generated_at,
        force_update=force_update,
    )

    return is_new


def delete_stale_split_children(
    conn: psycopg.Connection,
    s3_key: str,
    valid_document_ids: list[str],
) -> int:
    """Delete split-child document records that are no longer valid.

    When a multi-case PDF is re-processed and the LLM extracts a different
    number of rulings, old split-child documents beyond the new split count
    become orphans.  This function proactively removes them before the new
    split events are inserted.

    A document is eligible for deletion when:
      1. It shares the same ``s3_key`` as the document being re-processed.
      2. Its ``id`` is a UUID version 5 (deterministic split child ID).
      3. Its ``id`` is NOT in the ``valid_document_ids`` list (the new set
         of split IDs that will be created/upserted by this processing run).

    Cascade-deletes dependent rows in: ``rulings``, ``alert_events``,
    ``validation_results``.

    Args:
        conn: Active database connection (caller manages transaction).
        s3_key: The S3 key shared by the original document and its split
            children.
        valid_document_ids: The document IDs that will be created/upserted
            in this processing run.  These are NOT deleted.

    Returns:
        The number of document rows deleted.
    """
    if not s3_key:
        return 0

    with conn.cursor() as cur:
        # Find all split-child documents for this S3 key that are NOT in the
        # new valid set.  UUID v5 documents have version byte = 5; we filter
        # for that to avoid accidentally deleting the original parent document
        # (which is UUID v4).
        #
        # The version nibble sits at position 13 of the hex representation
        # (the first nibble of the 3rd group in the standard UUID format).
        # PostgreSQL's uuid type supports substring extraction on the text
        # representation.
        cur.execute(
            """
            SELECT id FROM documents
            WHERE s3_key = %s
              AND substring(id::text, 15, 1) = '5'
              AND id != ALL(%s::uuid[])
            """,
            (s3_key, valid_document_ids),
        )
        stale_ids = [str(row[0]) for row in cur.fetchall()]

    if not stale_ids:
        return 0

    with conn.cursor() as cur:
        # Cascade-delete dependent rows first.
        cur.execute(
            "DELETE FROM alert_events WHERE document_id = ANY(%s::uuid[])",
            (stale_ids,),
        )
        cur.execute(
            "DELETE FROM validation_results WHERE document_id = ANY(%s::uuid[])",
            (stale_ids,),
        )
        cur.execute(
            "DELETE FROM rulings WHERE document_id = ANY(%s::uuid[])",
            (stale_ids,),
        )
        # Delete the stale document records.
        cur.execute(
            "DELETE FROM documents WHERE id = ANY(%s::uuid[])",
            (stale_ids,),
        )
        deleted = cur.rowcount

    logger.info(
        "delete_stale_split_children: removed %d stale split-child document(s)",
        deleted,
        extra={"s3_key": s3_key, "stale_ids": stale_ids},
    )
    return deleted


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

    # Safety net: reject contaminated party names (court headers, ruling text,
    # motion descriptions) that slipped past upstream extraction.  Returns empty
    # string so the caller can detect the skip.  See #1932.
    if is_contaminated_party_name(raw_name):
        logger.warning(
            "upsert_party: skipping contaminated party name: %r",
            raw_name[:80],
        )
        return ""

    canonical = normalize_party_name(raw_name)

    with conn.cursor() as cur:
        # Look up existing alias for this raw name (case-insensitive to prevent
        # duplicate party records when the same name arrives in different casing)
        cur.execute(
            """
            SELECT pa.party_id
            FROM party_aliases pa
            WHERE LOWER(pa.raw_name) = LOWER(%s)
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
        # Safety net: skip contaminated party names (court headers, ruling
        # text, motion descriptions).  See #1932.
        if is_contaminated_party_name(raw_name):
            logger.warning(
                "batch_upsert_parties: skipping contaminated party name: %r",
                raw_name[:80],
            )
            continue
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
        # Step 1: Batch-lookup existing aliases — case-insensitive to prevent
        # duplicate party records when the same name arrives in different casing.
        cur.execute(
            "SELECT LOWER(raw_name), party_id FROM party_aliases WHERE LOWER(raw_name) = ANY(%s)",
            ([rn.lower() for rn in raw_names],),
        )
        existing: dict[str, str] = {row[0]: str(row[1]) for row in cur.fetchall()}

        # Step 2: Insert new parties + aliases for names not yet in the DB.
        # Use lowercased key for lookup to match the case-insensitive query above.
        new_entries = [(rn, cn) for rn, cn, _role in entries if rn.lower() not in existing]
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

            # Merge new IDs into the lookup dict (lowercased keys)
            for (rn, _cn), pid in zip(new_entries, new_ids):
                existing[rn.lower()] = pid

        # Step 3: Batch-insert case_party links
        link_params = []
        for raw_name, _canonical, role in entries:
            party_id = existing.get(raw_name.lower())
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
