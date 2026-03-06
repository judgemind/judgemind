"""Postgres write operations for the ingestion worker.

All functions accept a psycopg Connection and operate within a caller-managed
transaction. The caller is responsible for commit/rollback.

Write order per event:
  1. upsert_court  — idempotent on court_code
  2. upsert_case   — idempotent on (court_id, case_number)
  3. insert_document — idempotent on documents.id (= scraper document_id UUID)
  4. insert_ruling   — skipped if document already exists (idempotent via step 3)
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

logger = logging.getLogger(__name__)


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
) -> str:
    """Upsert a case row and return its UUID.

    Uses (court_id, case_number) as the natural key per the schema UNIQUE constraint.
    case_number_normalized strips whitespace and lowercases for search.
    """
    normalized = case_number.strip().lower().replace(" ", "").replace("-", "")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cases (case_number, case_number_normalized, court_id)
            VALUES (%s, %s, %s::uuid)
            ON CONFLICT (court_id, case_number) DO NOTHING
            RETURNING id
            """,
            (case_number, normalized, court_id),
        )
        row = cur.fetchone()
        if row is None:
            # Row already existed — fetch the id
            cur.execute(
                "SELECT id FROM cases WHERE court_id = %s::uuid AND case_number = %s",
                (court_id, case_number),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"upsert_case: could not retrieve case id for case_number={case_number!r}"
        )
    case_id: str = str(row[0])
    logger.debug("upsert_case: case_number=%s id=%s", case_number, case_id)
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
    """Insert a document row using the scraper-assigned document_id as the PK.

    Returns True if a new row was inserted, False if it already existed
    (idempotent — same document_id is a no-op).

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
            ON CONFLICT (id) DO NOTHING
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
        inserted = cur.rowcount == 1
    logger.debug("insert_document: id=%s inserted=%s", document_id, inserted)
    return inserted


def insert_ruling(
    conn: psycopg.Connection,
    document_id: str,
    case_id: str,
    court_id: str,
    hearing_date: date,
    ruling_text: str | None,
    department: str | None,
) -> None:
    """Insert a ruling row linked to the document.

    Skipped if a ruling for this document_id already exists (idempotent).
    document_id is a FK to documents.id and serves as the dedup key.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rulings (
                document_id, case_id, court_id,
                hearing_date, ruling_text, department, is_tentative
            )
            SELECT %s::uuid, %s::uuid, %s::uuid, %s::date, %s, %s, TRUE
            WHERE NOT EXISTS (
                SELECT 1 FROM rulings WHERE document_id = %s::uuid
            )
            """,
            (
                document_id,
                case_id,
                court_id,
                hearing_date,
                ruling_text,
                department,
                document_id,
            ),
        )
    logger.debug("insert_ruling: document_id=%s rowcount=%s", document_id, conn.cursor().rowcount)
