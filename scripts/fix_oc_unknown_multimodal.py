#!/usr/bin/env python3
# venv: scraper-framework
"""Fix remaining OC UNKNOWN case numbers via multi-strategy cleanup (#1953).

After the regex-based cleanup in #1929, 163 OC cases still have
``UNKNOWN-{uuid}`` case numbers. This script applies four strategies
in order to resolve as many as possible:

Phase 1 — Enhanced regex extraction
    Broader regex patterns that handle spaces in case numbers (e.g.
    ``25- 01509741``) and case numbers embedded deeper in ruling text.

Phase 2 — Exact title matching
    Match UNKNOWN case titles against known OC cases with very high
    similarity (>= 0.95). Only exact or near-exact matches to avoid
    false positives.

Phase 3 — Multimodal LLM index page extraction
    For cases grouped by parent PDF, use the LLM to read calendar index
    pages which sometimes list entry numbers alongside case numbers.

Phase 4 — Delete orphans
    Cases with no ruling text, no title, and no user-facing value are
    deleted along with their documents.

Usage:
    scripts/ecs-run-task.sh scripts/fix_oc_unknown_multimodal.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_oc_unknown_multimodal.py
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from difflib import SequenceMatcher

import boto3
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# OC court UUID (constant across environments)
OC_COURT_ID = "785660ca-0169-431c-8cab-34c55ca4d3b0"

# ---------------------------------------------------------------------------
# SQL queries
# ---------------------------------------------------------------------------

FETCH_ALL_UNKNOWN = """
    SELECT
        c.id AS case_id,
        c.case_number,
        c.case_title,
        d.id AS document_id,
        d.s3_key,
        d.s3_bucket,
        d.hearing_date AS doc_hearing_date,
        r.ruling_text,
        r.department
    FROM cases c
    JOIN courts co ON co.id = c.court_id
    JOIN documents d ON d.case_id = c.id AND d.status = 'active'
    LEFT JOIN LATERAL (
        SELECT r2.ruling_text, r2.department
        FROM rulings r2
        WHERE r2.case_id = c.id
          AND r2.ruling_text IS NOT NULL
        ORDER BY r2.hearing_date DESC
        LIMIT 1
    ) r ON TRUE
    WHERE co.county = 'Orange'
      AND c.case_number LIKE 'UNKNOWN-%%'
    ORDER BY c.id
"""

FETCH_KNOWN_OC_CASES = """
    SELECT c.id, c.case_number, c.case_title
    FROM cases c
    JOIN courts co ON co.id = c.court_id
    WHERE co.county = 'Orange'
      AND c.case_number NOT LIKE 'UNKNOWN-%%'
"""

FETCH_ORPHAN_UNKNOWNS = """
    SELECT c.id AS case_id, c.case_number
    FROM cases c
    JOIN courts co ON co.id = c.court_id
    WHERE co.county = 'Orange'
      AND c.case_number LIKE 'UNKNOWN-%%'
      AND c.case_title IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM rulings r
          WHERE r.case_id = c.id AND r.ruling_text IS NOT NULL
      )
"""

CHECK_EXISTING_CASE = """
    SELECT id FROM cases
    WHERE court_id = %s AND case_number = %s
    LIMIT 1
"""

UPDATE_CASE_NUMBER = """
    UPDATE cases
    SET case_number = %s, updated_at = NOW()
    WHERE id = %s AND case_number LIKE 'UNKNOWN-%%'
"""

MERGE_RULINGS = """
    UPDATE rulings SET case_id = %s, updated_at = NOW()
    WHERE case_id = %s
"""

MERGE_DOCUMENTS = """
    UPDATE documents SET case_id = %s
    WHERE case_id = %s
"""

DELETE_DUPLICATE_RULINGS = """
    DELETE FROM rulings
    WHERE case_id = %s
      AND ruling_text_hash IN (
          SELECT ruling_text_hash FROM rulings WHERE case_id = %s
      )
"""

DELETE_ORPHAN_RULINGS = """
    DELETE FROM rulings WHERE case_id = %s
"""

DELETE_ORPHAN_DOCUMENTS = """
    DELETE FROM documents
    WHERE case_id = %s
      AND id NOT IN (
          SELECT document_id FROM rulings WHERE document_id IS NOT NULL
      )
"""

REASSIGN_SHARED_DOCUMENTS = """
    UPDATE documents
    SET case_id = (
        SELECT r.case_id FROM rulings r
        WHERE r.document_id = documents.id
        LIMIT 1
    )
    WHERE case_id = %s
      AND id IN (
          SELECT document_id FROM rulings WHERE document_id IS NOT NULL
      )
"""

DELETE_CASE = """
    DELETE FROM cases WHERE id = %s
"""

# ---------------------------------------------------------------------------
# Case number patterns — enhanced for OC
# ---------------------------------------------------------------------------

# Standard OC case number formats
_CASE_NUMBER_PATTERNS = [
    # 30-2025-01476705 or 30-2024-01420730
    re.compile(r"\b(30-\d{4}-\d{7,})\b"),
    # 2025-01476705 or 2024-01420730
    re.compile(r"\b(\d{4}-\d{7,})\b"),
    # 25-01476705 (two-digit year, no prefix)
    re.compile(r"\b(\d{2}-\d{7,})\b"),
    # 24AVCV01623 format
    re.compile(r"\b(\d{2}[A-Z]{2,4}\d{4,})\b"),
    # 8-digit numbers starting with 0 (e.g. 01420730)
    re.compile(r"\b(0\d{7,})\b"),
]

# Enhanced patterns for extraction from ruling text — handles spaces and
# line breaks within case numbers (e.g. "25- 01509741" or "2021-01234131")
_TEXT_CASE_NUMBER_PATTERNS = [
    # "30-2025-01476705" standard
    re.compile(r"\b(30-\d{4}-\d{7,})\b"),
    # "2025-01476705" or "2024-01420730"
    re.compile(r"\b(\d{4}-\d{7,})\b"),
    # "25-01509741" or "25- 01509741" (optional space after dash)
    re.compile(r"\b(\d{2})-\s*(0\d{6,})\b"),
    # "24AVCV01623"
    re.compile(r"\b(\d{2}[A-Z]{2,4}\d{4,})\b"),
    # Standalone 8+ digit starting with 0 (must be on its own line or
    # surrounded by whitespace, not part of a larger number)
    re.compile(r"(?:^|\n)\s*(0\d{7,})\s*(?:\n|$)", re.MULTILINE),
]


def _is_valid_oc_case_number(case_number: str | None) -> bool:
    """Check if a string looks like a valid OC case number."""
    if not case_number:
        return False
    if case_number.startswith("UNKNOWN"):
        return False
    for pattern in _CASE_NUMBER_PATTERNS:
        if pattern.fullmatch(case_number):
            return True
    return False


def _extract_case_number_from_text(text: str | None) -> str | None:
    """Extract a case number from ruling text using enhanced regex patterns.

    Searches the first 1000 characters of the text (case header area).
    Handles spaces within case numbers (e.g. "25- 01509741").
    """
    if not text:
        return None
    search_area = text[:1000]
    for pattern in _TEXT_CASE_NUMBER_PATTERNS:
        m = pattern.search(search_area)
        if m:
            if m.lastindex and m.lastindex == 2:
                # Pattern with space: join the two groups
                result = f"{m.group(1)}-{m.group(2)}"
            else:
                result = m.group(1)
            # Normalize: remove internal spaces
            result = result.replace(" ", "")
            if _is_valid_oc_case_number(result):
                return result
    return None


# ---------------------------------------------------------------------------
# Entry number extraction from ruling text
# ---------------------------------------------------------------------------

_ENTRY_NUMBER_PATTERN = re.compile(r"^\s*(\d{1,3})\s")


def _extract_entry_number(ruling_text: str | None) -> int | None:
    """Extract the calendar entry number from the beginning of ruling text."""
    if not ruling_text:
        return None
    m = _ENTRY_NUMBER_PATTERN.match(ruling_text)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------

def _normalize_title(title: str | None) -> str:
    """Normalize a title for comparison."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[.,;:'\"\(\)\[\]!?]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(?:versus|vs\.?)\b", "v", t)
    return t


def _title_similarity(title1: str | None, title2: str | None) -> float:
    """Compute similarity between two case titles (0.0 to 1.0)."""
    n1 = _normalize_title(title1)
    n2 = _normalize_title(title2)
    if not n1 or not n2:
        return 0.0
    return SequenceMatcher(None, n1, n2).ratio()


# ---------------------------------------------------------------------------
# LLM index page extraction
# ---------------------------------------------------------------------------

_INDEX_PROMPT = """\
You are reading a court calendar index page from the Orange County Superior Court.

This page lists cases scheduled for hearing. Each entry has:
- An entry number (sequential integer)
- A case number (e.g. "30-2024-01476705" or "01476705")
- A case title (e.g. "Smith vs. Jones")

Extract ALL entries visible on this page. Return a JSON array where each element has:
- "entry_number": integer
- "case_number": string (the court case number, NOT the entry number)
- "case_title": string

Important:
- The case_number is typically an 8-digit number (like 01476705) or in format
  30-YYYY-NNNNNNN.  Do NOT confuse the entry number (1, 2, 3...) with the case number.
- If you cannot determine the case number for an entry, set case_number to null.
- Return ONLY the JSON array, no other text.

Example output:
[
  {"entry_number": 1, "case_number": "01476705", "case_title": "Smith vs. Jones"},
  {"entry_number": 2, "case_number": "30-2024-01453401", "case_title": "Doe vs. Roe"}
]
"""


def _extract_index_from_pdf(
    raw_content: bytes,
    google_client: object,
    model: str = "gemini-2.5-flash-lite",
    max_index_pages: int = 5,
) -> dict[int, dict]:
    """Extract entry_number -> case info mapping from PDF index pages."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=raw_content, filetype="pdf")
    total_pages = len(doc)
    pages_to_read = min(max_index_pages, total_pages)

    mapping: dict[int, dict] = {}

    for page_idx in range(pages_to_read):
        page = doc[page_idx]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        img_b64 = base64.b64encode(img_bytes).decode("ascii")

        try:
            response = google_client.models.generate_content(  # type: ignore[union-attr]
                model=model,
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {"text": _INDEX_PROMPT},
                            {
                                "inline_data": {
                                    "mime_type": "image/png",
                                    "data": img_b64,
                                }
                            },
                        ],
                    }
                ],
            )
        except Exception:
            logger.warning("LLM call failed for page %d", page_idx, exc_info=True)
            continue

        text = response.text  # type: ignore[union-attr]
        if not text:
            continue

        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)

        try:
            entries = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from page %d: %.100s", page_idx, text)
            continue

        if not isinstance(entries, list):
            continue

        for entry in entries:
            entry_num = entry.get("entry_number")
            case_num = entry.get("case_number")
            case_title = entry.get("case_title")

            if entry_num is not None and case_num is not None:
                if _is_valid_oc_case_number(str(case_num)):
                    mapping[int(entry_num)] = {
                        "case_number": str(case_num),
                        "case_title": case_title,
                    }

    doc.close()
    return mapping


def _create_google_client() -> object:
    """Create a Google GenAI client using GOOGLE_API_KEY env var."""
    from google import genai

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        msg = "GOOGLE_API_KEY environment variable is required"
        raise RuntimeError(msg)
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Update / merge helpers
# ---------------------------------------------------------------------------

def _apply_case_number(
    conn: psycopg.Connection,
    case_id: str,
    old_case_number: str,
    new_case_number: str,
    case_title: str | None,
    match_method: str,
    *,
    dry_run: bool = False,
) -> str:
    """Update or merge a case with a new case number.

    Returns "updated", "merged", or "skipped".
    """
    with conn.cursor() as cur:
        cur.execute(CHECK_EXISTING_CASE, (OC_COURT_ID, new_case_number))
        existing = cur.fetchone()

    if existing:
        existing_id = existing[0]
        if str(existing_id) == str(case_id):
            return "skipped"

        logger.info(
            "  MERGE: %s -> %s [%s] ('%s')",
            old_case_number, new_case_number, match_method, case_title,
        )

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(DELETE_DUPLICATE_RULINGS, (str(case_id), str(existing_id)))
                cur.execute(MERGE_RULINGS, (str(existing_id), str(case_id)))
                cur.execute(MERGE_DOCUMENTS, (str(existing_id), str(case_id)))
                cur.execute(DELETE_CASE, (str(case_id),))

        return "merged"

    logger.info(
        "  UPDATE: %s -> %s [%s] ('%s')",
        old_case_number, new_case_number, match_method, case_title,
    )

    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(UPDATE_CASE_NUMBER, (new_case_number, str(case_id)))

    return "updated"


# ---------------------------------------------------------------------------
# Phase 1: Enhanced regex extraction
# ---------------------------------------------------------------------------

def phase1_regex(
    conn: psycopg.Connection,
    unknown_cases: list[dict],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Extract case numbers from ruling text using enhanced regex patterns."""
    logger.info("=== Phase 1: Enhanced regex extraction ===")
    stats = {"updated": 0, "merged": 0, "skipped": 0}

    for uc in unknown_cases:
        ruling_text = uc.get("ruling_text")
        case_number = _extract_case_number_from_text(ruling_text)
        if case_number is None:
            continue

        result = _apply_case_number(
            conn,
            str(uc["case_id"]),
            uc["case_number"],
            case_number,
            uc.get("case_title"),
            "regex",
            dry_run=dry_run,
        )
        stats[result] += 1

    logger.info(
        "Phase 1 complete: %d updated, %d merged, %d skipped",
        stats["updated"], stats["merged"], stats["skipped"],
    )
    return stats


# ---------------------------------------------------------------------------
# Phase 2: Exact title matching against known OC cases
# ---------------------------------------------------------------------------

def phase2_title_match(
    conn: psycopg.Connection,
    unknown_cases: list[dict],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Match UNKNOWN cases to known OC cases by exact/near-exact title."""
    logger.info("=== Phase 2: Exact title matching ===")
    stats = {"updated": 0, "merged": 0, "skipped": 0}

    # Fetch known OC cases
    with conn.cursor() as cur:
        cur.execute(FETCH_KNOWN_OC_CASES)
        known_rows = cur.fetchall()

    logger.info("  Known OC cases for matching: %d", len(known_rows))

    # Build index of known cases by normalized title
    known_by_title: dict[str, list[tuple[str, str, str]]] = {}
    for kid, kcn, ktitle in known_rows:
        norm = _normalize_title(ktitle)
        if norm:
            known_by_title.setdefault(norm, []).append((kid, kcn, ktitle))

    # Filter to UNKNOWN cases that still need fixing (not already resolved in phase 1)
    remaining = [uc for uc in unknown_cases if uc.get("_resolved") is not True]

    for uc in remaining:
        case_title = uc.get("case_title")
        if not case_title:
            continue

        norm_title = _normalize_title(case_title)
        if not norm_title:
            continue

        # Try exact normalized match first
        if norm_title in known_by_title:
            matches = known_by_title[norm_title]
            # Use the first match (they all have the same title)
            kid, kcn, ktitle = matches[0]

            result = _apply_case_number(
                conn,
                str(uc["case_id"]),
                uc["case_number"],
                kcn,
                case_title,
                f"exact_title_match ('{ktitle}')",
                dry_run=dry_run,
            )
            stats[result] += 1
            if result in ("updated", "merged"):
                uc["_resolved"] = True
            continue

        # Try fuzzy match with very high threshold
        best_score = 0.95
        best_match = None
        for norm_k, k_entries in known_by_title.items():
            score = SequenceMatcher(None, norm_title, norm_k).ratio()
            if score > best_score:
                best_score = score
                best_match = k_entries[0]

        if best_match:
            kid, kcn, ktitle = best_match
            result = _apply_case_number(
                conn,
                str(uc["case_id"]),
                uc["case_number"],
                kcn,
                case_title,
                f"fuzzy_title_match (score={best_score:.2f}, '{ktitle}')",
                dry_run=dry_run,
            )
            stats[result] += 1
            if result in ("updated", "merged"):
                uc["_resolved"] = True

    logger.info(
        "Phase 2 complete: %d updated, %d merged, %d skipped",
        stats["updated"], stats["merged"], stats["skipped"],
    )
    return stats


# ---------------------------------------------------------------------------
# Phase 3: LLM index page extraction
# ---------------------------------------------------------------------------

def phase3_llm_index(
    conn: psycopg.Connection,
    unknown_cases: list[dict],
    s3_client: object,
    google_client: object,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Use multimodal LLM to read calendar index pages for case numbers."""
    logger.info("=== Phase 3: LLM index page extraction ===")
    stats = {"updated": 0, "merged": 0, "skipped": 0, "errors": 0}

    # Filter to unresolved cases with S3 keys
    remaining = [
        uc for uc in unknown_cases
        if uc.get("_resolved") is not True and uc.get("s3_key")
    ]

    # Group by S3 key
    by_s3_key: dict[str, list[dict]] = {}
    s3_buckets: dict[str, str] = {}
    for uc in remaining:
        s3_key = uc["s3_key"]
        by_s3_key.setdefault(s3_key, []).append(uc)
        s3_buckets[s3_key] = uc["s3_bucket"]

    logger.info("  %d unresolved cases in %d PDFs", len(remaining), len(by_s3_key))

    for i, (s3_key, cases_in_pdf) in enumerate(sorted(by_s3_key.items())):
        logger.info(
            "  Processing PDF %d/%d: %s (%d cases)",
            i + 1, len(by_s3_key), s3_key.rsplit("/", 1)[-1], len(cases_in_pdf),
        )

        try:
            response = s3_client.get_object(  # type: ignore[union-attr]
                Bucket=s3_buckets[s3_key], Key=s3_key,
            )
            raw_content = response["Body"].read()  # type: ignore[index]
        except Exception:
            logger.error("Failed to fetch s3://%s/%s", s3_buckets[s3_key], s3_key, exc_info=True)
            stats["errors"] += len(cases_in_pdf)
            continue

        try:
            index_mapping = _extract_index_from_pdf(raw_content, google_client)
        except Exception:
            logger.error("Index extraction failed for %s", s3_key, exc_info=True)
            stats["errors"] += len(cases_in_pdf)
            continue

        logger.info("    Index mapping: %d entries with case numbers", len(index_mapping))

        if not index_mapping:
            stats["skipped"] += len(cases_in_pdf)
            continue

        for uc in cases_in_pdf:
            entry_num = _extract_entry_number(uc.get("ruling_text"))
            new_case_number = None
            match_method = None

            # Match by entry number
            if entry_num is not None and entry_num in index_mapping:
                new_case_number = index_mapping[entry_num]["case_number"]
                match_method = f"llm_entry_number={entry_num}"

            # Fallback: match by title similarity against index entries
            if new_case_number is None and uc.get("case_title"):
                best_score = 0.5
                for _en, entry_info in index_mapping.items():
                    score = _title_similarity(uc["case_title"], entry_info.get("case_title"))
                    if score > best_score:
                        best_score = score
                        new_case_number = entry_info["case_number"]
                        match_method = f"llm_title_match (score={score:.2f})"

            if not new_case_number or not _is_valid_oc_case_number(new_case_number):
                logger.info(
                    "    SKIP: No match for '%s' (entry=%s, %s)",
                    uc.get("case_title"), entry_num, uc["case_number"],
                )
                stats["skipped"] += 1
                continue

            result = _apply_case_number(
                conn,
                str(uc["case_id"]),
                uc["case_number"],
                new_case_number,
                uc.get("case_title"),
                match_method,
                dry_run=dry_run,
            )
            stats[result] += 1
            if result in ("updated", "merged"):
                uc["_resolved"] = True

    logger.info(
        "Phase 3 complete: %d updated, %d merged, %d skipped, %d errors",
        stats["updated"], stats["merged"], stats["skipped"], stats["errors"],
    )
    return stats


# ---------------------------------------------------------------------------
# Phase 4: Delete orphans
# ---------------------------------------------------------------------------

def phase4_delete_orphans(
    conn: psycopg.Connection,
    *,
    dry_run: bool = False,
) -> int:
    """Delete UNKNOWN cases with no ruling text and no title (empty orphans)."""
    logger.info("=== Phase 4: Delete orphans ===")

    with conn.cursor() as cur:
        cur.execute(FETCH_ORPHAN_UNKNOWNS)
        orphans = cur.fetchall()

    logger.info("  Found %d orphan UNKNOWN cases (no ruling, no title)", len(orphans))

    deleted = 0
    for case_id, case_number in orphans:
        logger.info("  DELETE orphan: %s (id=%s)", case_number, case_id)
        if not dry_run:
            with conn.cursor() as cur:
                # Delete rulings for this case (may have NULL ruling_text entries)
                cur.execute(DELETE_ORPHAN_RULINGS, (str(case_id),))
                # Reassign documents that are referenced by rulings from other cases
                cur.execute(REASSIGN_SHARED_DOCUMENTS, (str(case_id),))
                # Delete documents that are not referenced by any other ruling
                cur.execute(DELETE_ORPHAN_DOCUMENTS, (str(case_id),))
                # Delete the case itself
                cur.execute(DELETE_CASE, (str(case_id),))
        deleted += 1

    logger.info("Phase 4 complete: %d orphans deleted", deleted)
    return deleted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix OC UNKNOWN case numbers via multi-strategy cleanup (#1953).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without writing to the database.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip Phase 3 (LLM index extraction) to save API calls.",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    # Initialize clients
    google_client = None
    s3_client = None
    if not args.skip_llm:
        google_client = _create_google_client()
        logger.info("Google client initialized")
        s3_client = boto3.client("s3")

    totals = {"updated": 0, "merged": 0, "skipped": 0, "errors": 0, "deleted": 0}

    with psycopg.connect(dsn) as conn:
        # Fetch all UNKNOWN cases
        with conn.cursor() as cur:
            cur.execute(FETCH_ALL_UNKNOWN)
            rows = cur.fetchall()

        logger.info("Found %d UNKNOWN case-document pairs", len(rows))

        unknown_cases = []
        seen_case_ids: set[str] = set()
        for row in rows:
            case_id, case_number, case_title, document_id, s3_key, s3_bucket, hearing_date, ruling_text, department = row
            cid = str(case_id)
            if cid in seen_case_ids:
                continue
            seen_case_ids.add(cid)
            unknown_cases.append({
                "case_id": case_id,
                "case_number": case_number,
                "case_title": case_title,
                "document_id": document_id,
                "s3_key": s3_key,
                "s3_bucket": s3_bucket,
                "hearing_date": hearing_date,
                "ruling_text": ruling_text,
                "department": department,
            })

        logger.info("Unique UNKNOWN cases: %d", len(unknown_cases))

        # Phase 1: Enhanced regex
        p1 = phase1_regex(conn, unknown_cases, dry_run=args.dry_run)
        for k in ("updated", "merged", "skipped"):
            totals[k] += p1[k]
        if not args.dry_run:
            conn.commit()

        # Mark resolved cases
        for uc in unknown_cases:
            if p1["updated"] > 0 or p1["merged"] > 0:
                # Re-check which are still UNKNOWN
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT case_number FROM cases WHERE id = %s",
                        (str(uc["case_id"]),),
                    )
                    result = cur.fetchone()
                    if result and not result[0].startswith("UNKNOWN"):
                        uc["_resolved"] = True
                    elif result is None:
                        uc["_resolved"] = True

        # Phase 2: Exact title matching
        p2 = phase2_title_match(conn, unknown_cases, dry_run=args.dry_run)
        for k in ("updated", "merged", "skipped"):
            totals[k] += p2[k]
        if not args.dry_run:
            conn.commit()

        # Phase 3: LLM index extraction (optional)
        if not args.skip_llm and google_client and s3_client:
            p3 = phase3_llm_index(
                conn, unknown_cases, s3_client, google_client,
                dry_run=args.dry_run,
            )
            for k in ("updated", "merged", "skipped", "errors"):
                totals[k] += p3[k]
            if not args.dry_run:
                conn.commit()

        # Phase 4: Delete orphans
        deleted = phase4_delete_orphans(conn, dry_run=args.dry_run)
        totals["deleted"] = deleted
        if not args.dry_run:
            conn.commit()

        if args.dry_run:
            conn.rollback()
            logger.info("Dry run — all changes rolled back")

    logger.info(
        "Complete: %d updated, %d merged, %d deleted, %d skipped, %d errors",
        totals["updated"], totals["merged"], totals["deleted"],
        totals["skipped"], totals["errors"],
    )

    # Final check: count remaining UNKNOWN
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM cases c "
                "JOIN courts co ON c.court_id = co.id "
                "WHERE co.county = 'Orange' AND c.case_number LIKE 'UNKNOWN-%%'"
            )
            remaining = cur.fetchone()
            if remaining:
                logger.info("Remaining UNKNOWN cases: %d", remaining[0])


if __name__ == "__main__":
    main()
