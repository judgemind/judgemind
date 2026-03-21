#!/usr/bin/env python3
# venv: scraper-framework
"""Backfill OC rulings with corrected table-aware PDF text extraction (#1269).

PR #1262 fixed the OC calendar PDF text extraction going forward, but existing
OC rulings in the database still have garbled text where case names are
interleaved with ruling text (e.g., "Illingworth vs. Before the court is an
unopposed motion").

This script:
1. Queries for all OC PDF rulings that have an associated S3-archived document.
2. Fetches the raw PDF bytes from S3.
3. Re-extracts text using the table-aware pdfplumber extraction logic.
4. Updates the ruling_text in the rulings table.

The script is idempotent: re-running it produces the same result because the
same extraction logic is applied deterministically to the same PDF content.

Usage:
    scripts/ecs-run.sh --script scripts/backfill_oc_ruling_text.py
    scripts/ecs-run.sh --script scripts/backfill_oc_ruling_text.py -- --dry-run
    scripts/ecs-run.sh --script scripts/backfill_oc_ruling_text.py -- --limit 10

Options:
    --dry-run       Print what would be updated without writing to the database.
    --batch-size N  Number of rulings to process per batch (default: 50).
    --limit N       Maximum total rulings to process (default: unlimited).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys

import boto3
import pdfplumber
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table-aware PDF text extraction — inlined from oc_tentatives.py (#1241)
# (ECS oneshot constraint: scripts cannot import from courts/ modules)
# ---------------------------------------------------------------------------

_TABLE_HEADER_RE = re.compile(
    r"^(?:#|Case\s+Name|Tentative|MATTER)$",
    re.IGNORECASE,
)


def _is_header_row(row: list[str | None]) -> bool:
    """Return True if the row is a table header."""
    non_empty = [c.strip() for c in row if c and c.strip()]
    if not non_empty:
        return True
    return all(_TABLE_HEADER_RE.match(cell) for cell in non_empty)


def _reconstruct_entry_text(
    entry_num: str,
    case_name_cell: str,
    ruling_cell: str,
) -> str:
    """Reconstruct a single case entry with columns properly separated."""
    parts: list[str] = []
    case_lines = case_name_cell.strip().split("\n") if case_name_cell else []
    if case_lines:
        parts.append(f"{entry_num} {case_lines[0]}")
        for line in case_lines[1:]:
            parts.append(line)
    else:
        parts.append(entry_num)
    if ruling_cell:
        parts.append(ruling_cell.strip())
    return "\n".join(parts)


def _extract_header_text_above_table(
    page: pdfplumber.page.Page,
    table_top: float,
) -> str | None:
    """Extract text from the region above the first table on a page."""
    if table_top < 20:
        return None
    header_region = page.within_bbox((0, 0, page.width, table_top))
    text = header_region.extract_text()
    if text and text.strip():
        return text.strip()
    return None


def _extract_table_entries_from_page(
    page: pdfplumber.page.Page,
) -> tuple[list[str], str | None] | None:
    """Extract case entries from a page using table detection."""
    tables = page.find_tables()
    if not tables:
        return None

    table = tables[0]
    rows = table.extract()
    header_text = _extract_header_text_above_table(page, table.bbox[1])

    if not rows:
        return None

    entries: list[str] = []
    current_ruling_continuation: list[str] = []

    for row in rows:
        if _is_header_row(row):
            continue

        cells = [c if c else "" for c in row]

        if len(cells) <= 3:
            entry_num_cell = cells[0].strip() if len(cells) > 0 else ""
            case_name_cell = cells[1].strip() if len(cells) > 1 else ""
            ruling_cell = cells[2].strip() if len(cells) > 2 else ""
        else:
            entry_num_cell = cells[0].strip()
            case_name_cell = ""
            ruling_cell = ""
            found_case = False
            for c in cells[1:]:
                cv = c.strip()
                if not cv:
                    continue
                if not found_case:
                    case_name_cell = cv
                    found_case = True
                else:
                    ruling_cell = cv
                    break

        is_new_entry = bool(
            entry_num_cell and re.match(r"^\d", entry_num_cell.replace("\n", " "))
        )

        if is_new_entry:
            if current_ruling_continuation and entries:
                entries[-1] = (
                    entries[-1] + "\n" + "\n".join(current_ruling_continuation)
                )
            current_ruling_continuation = []

            entry_num_clean = entry_num_cell.replace("\n", " ").strip()
            entry_num_clean = entry_num_clean.rstrip(".")

            entry_text = _reconstruct_entry_text(
                entry_num_clean, case_name_cell, ruling_cell
            )
            entries.append(entry_text)
        else:
            continuation_parts = []
            if case_name_cell:
                continuation_parts.append(case_name_cell)
            if ruling_cell:
                continuation_parts.append(ruling_cell)
            if continuation_parts:
                current_ruling_continuation.append("\n".join(continuation_parts))

    if current_ruling_continuation and entries:
        entries[-1] = entries[-1] + "\n" + "\n".join(current_ruling_continuation)

    if entries:
        return entries, header_text
    return None


def _extract_oc_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from an OC calendar PDF using table-aware extraction."""
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            result = _extract_table_entries_from_page(page)
            if result is not None:
                table_entries, header_text = result
                if header_text:
                    lines.append(header_text)
                lines.extend(table_entries)
            else:
                text = page.extract_text()
                if text:
                    lines.append(text)
    return "\n".join(lines)


# Case number normalization — also inlined from oc_tentatives.py
_SPLIT_CASE_NUMBER_RE = re.compile(
    r"(\d{2,4})-\s*\n\s*(\d{7,8})\b",
)


def _normalize_oc_text(text: str) -> str:
    """Pre-process extracted PDF text to rejoin case numbers split across lines."""
    return _SPLIT_CASE_NUMBER_RE.sub(r"\1-\2", text)


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

FETCH_RULINGS_QUERY = """
    SELECT
        r.id AS ruling_id,
        r.ruling_text,
        d.id AS document_id,
        d.s3_key,
        d.s3_bucket,
        d.captured_at
    FROM rulings r
    JOIN documents d ON r.document_id = d.id
    WHERE d.scraper_id = 'ca-oc-tentatives-civil'
      AND d.format = 'pdf'
      AND d.s3_key IS NOT NULL
      AND d.s3_bucket IS NOT NULL
      AND (d.captured_at, r.id) > (%s, %s)
    ORDER BY d.captured_at, r.id
    LIMIT %s
"""

# Minimum cursor values for the first batch.
_CURSOR_MIN_TIMESTAMP = "1970-01-01T00:00:00+00:00"
_CURSOR_MIN_UUID = "00000000-0000-0000-0000-000000000000"


def main() -> None:
    """Re-extract ruling text from archived OC PDFs using table-aware logic."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    s3_client = boto3.client("s3")
    conn = psycopg.connect(database_url, autocommit=False)

    total_processed = 0
    total_updated = 0
    total_unchanged = 0
    total_errors = 0
    cursor_captured_at = _CURSOR_MIN_TIMESTAMP
    cursor_ruling_id = _CURSOR_MIN_UUID

    try:
        while True:
            effective_limit = args.batch_size
            if args.limit > 0:
                remaining = args.limit - total_processed
                if remaining <= 0:
                    break
                effective_limit = min(args.batch_size, remaining)

            with conn.cursor() as cur:
                cur.execute(
                    FETCH_RULINGS_QUERY,
                    (cursor_captured_at, cursor_ruling_id, effective_limit),
                )
                rows = cur.fetchall()

            if not rows:
                break

            # Advance cursor to the last row in this batch
            last_row = rows[-1]
            cursor_captured_at = last_row[5]  # d.captured_at
            cursor_ruling_id = last_row[0]  # r.id
            logger.info(
                "Processing batch of %d rulings (cursor=%s/%s)",
                len(rows),
                cursor_captured_at,
                cursor_ruling_id,
            )

            for ruling_id, old_text, doc_id, s3_key, s3_bucket, _captured_at in rows:
                total_processed += 1

                try:
                    # Fetch raw PDF from S3
                    response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
                    pdf_bytes = response["Body"].read()

                    # Re-extract using table-aware logic
                    new_text = _extract_oc_pdf_text(pdf_bytes)
                    new_text = _normalize_oc_text(new_text)

                    # Strip null bytes
                    new_text = new_text.replace("\x00", "")

                    if not new_text or not new_text.strip():
                        logger.warning(
                            "Empty extraction for ruling %s (doc %s), skipping",
                            ruling_id,
                            doc_id,
                        )
                        total_errors += 1
                        continue

                    # Compare: did the text actually change?
                    if old_text and old_text.strip() == new_text.strip():
                        total_unchanged += 1
                        continue

                    # Show a sample of what changed
                    old_preview = (old_text or "")[:120].replace("\n", " ")
                    new_preview = new_text[:120].replace("\n", " ")
                    logger.info(
                        "Ruling %s: text changed\n  OLD: %s\n  NEW: %s",
                        ruling_id,
                        old_preview,
                        new_preview,
                    )

                    if not args.dry_run:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE rulings
                                SET ruling_text = %s, updated_at = NOW()
                                WHERE id = %s
                                """,
                                (new_text, ruling_id),
                            )
                        conn.commit()

                    total_updated += 1

                except Exception:
                    logger.exception(
                        "Error processing ruling %s (doc %s, s3=%s)",
                        ruling_id,
                        doc_id,
                        s3_key,
                    )
                    total_errors += 1
                    conn.rollback()

    finally:
        conn.close()

    logger.info(
        "Done: %d processed, %d updated, %d unchanged, %d errors%s",
        total_processed,
        total_updated,
        total_unchanged,
        total_errors,
        " (dry-run)" if args.dry_run else "",
    )

    # Exit with error if there were failures
    if total_errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
