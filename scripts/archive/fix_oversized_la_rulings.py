#!/usr/bin/env python3
"""Fix oversized LA rulings by re-extracting ruling_text from raw HTML.

For LA rulings with ruling_text > 20k characters, the root causes are:
1. UUID v4 documents: Multi-case calendar pages stored as single rulings
   because the LLM splitter was not invoked during reingest (#2007 fix).
2. UUID v5 documents: Split children where the LLM reproduced ruling_text
   verbatim but hit the output token limit (~50k char truncation).

This script reduces ruling_text size for BOTH categories by:
- Downloading raw HTML from S3
- Splitting multi-case pages using _split_cases_html()
- Extracting ruling_text from each per-case HTML chunk via BeautifulSoup
- Updating ruling_text in the database to the matched per-case chunk

NOTE: For v4 multi-case documents, this script only updates the existing
ruling row with the text from its matched case — it does NOT create new
split children for the other cases on the page.  To fully split v4
documents, re-run ``reingest_from_s3.py --full-reparse`` (which now
correctly invokes the LLM splitter for LA documents).

Usage:
    scripts/ecs-run-task.sh scripts/fix_oversized_la_rulings.py -- --dry-run
    scripts/ecs-run-task.sh scripts/fix_oversized_la_rulings.py
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import uuid
from typing import Any

import boto3
import psycopg
import structlog
from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
S3_BUCKET = os.environ.get("S3_BUCKET", "judgemind-documents-dev")

# NOTE: The patterns and helpers below are duplicated from la_tentatives.py.
# This is intentional — ECS oneshot scripts are uploaded as single files and
# cannot import from other packages (see CLAUDE.md §ECS oneshot constraint).

# Case number pattern from la_tentatives.py.
_CASE_NUMBER_RE = re.compile(r"Case Number:\s*(\w+)")

# Split boundary from la_tentatives.py.
_CASE_SPLIT_RE = re.compile(
    r"<HR[^>]*>\s*(?:<P>)?\s*(?=<B>\s*Case Number:\s*</B>)",
    re.IGNORECASE,
)

# Lightweight tag stripper for "Case Number:" substring check.
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _split_cases_html(ruling_html: str) -> list[str]:
    """Split a department HTML page into per-case HTML sections."""
    soup = BeautifulSoup(ruling_html, "lxml")
    speech_div = soup.find("div", id="speechSynthesis")
    if speech_div is None:
        return []

    inner_html = speech_div.decode_contents()
    sections = _CASE_SPLIT_RE.split(inner_html)

    result: list[str] = []
    for section in sections:
        stripped = section.strip()
        if not stripped:
            continue
        section_text = _HTML_TAG_RE.sub("", stripped)
        if not _CASE_NUMBER_RE.search(section_text):
            continue
        wrapped = (
            f'<html><body><div id="speechSynthesis">{stripped}</div></body></html>'
        )
        result.append(wrapped)
    return result


def _extract_text_from_html(html_chunk: str) -> str:
    """Extract plain text from an HTML chunk."""
    soup = BeautifulSoup(html_chunk, "lxml")
    content = soup.find("div", id="speechSynthesis")
    if content:
        return content.get_text(separator="\n", strip=True)
    return soup.get_text(separator="\n", strip=True)


def _extract_case_number(text: str) -> str | None:
    """Extract first case number from text."""
    m = _CASE_NUMBER_RE.search(text)
    return m.group(1) if m else None


def _make_split_document_id(parent_id: str, index: int) -> str:
    """Generate deterministic UUID v5 for a split ruling."""
    namespace = uuid.UUID(parent_id)
    return str(uuid.uuid5(namespace, str(index)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix oversized LA rulings")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without modifying the database",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N rulings (0 = all)",
    )
    args = parser.parse_args()

    s3 = boto3.client("s3")

    with psycopg.connect(DATABASE_URL) as conn:
        # Find all oversized LA rulings.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    r.id::text as ruling_id,
                    d.id::text as document_id,
                    d.s3_key,
                    d.s3_bucket,
                    d.content_hash,
                    r.case_id::text,
                    r.court_id::text,
                    LENGTH(r.ruling_text) as text_length,
                    CASE
                        WHEN d.id::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}'
                        THEN 'v5'
                        ELSE 'v4'
                    END as uuid_ver
                FROM rulings r
                JOIN courts co ON r.court_id = co.id
                JOIN documents d ON r.document_id = d.id
                WHERE co.county = 'Los Angeles'
                  AND LENGTH(r.ruling_text) > 20000
                ORDER BY LENGTH(r.ruling_text) DESC
            """)
            rows = cur.fetchall()
            cols = [desc.name for desc in cur.description]

        rulings = [dict(zip(cols, row)) for row in rows]
        if args.limit:
            rulings = rulings[: args.limit]

        print(f"Found {len(rulings)} oversized LA rulings to fix")
        v4_count = sum(1 for r in rulings if r["uuid_ver"] == "v4")
        v5_count = sum(1 for r in rulings if r["uuid_ver"] == "v5")
        print(f"  UUID v4 (multi-case pages): {v4_count}")
        print(f"  UUID v5 (split children):   {v5_count}")

        # Group by S3 key to avoid downloading the same file multiple times.
        by_s3_key: dict[str, list[dict[str, Any]]] = {}
        for r in rulings:
            key = r["s3_key"]
            by_s3_key.setdefault(key, []).append(r)

        print(f"  Unique S3 keys: {len(by_s3_key)}")

        fixed_count = 0
        skipped_count = 0
        error_count = 0

        for s3_key, group in by_s3_key.items():
            bucket = group[0]["s3_bucket"] or S3_BUCKET
            try:
                obj = s3.get_object(Bucket=bucket, Key=s3_key)
                raw_html = obj["Body"].read().decode("utf-8", errors="replace")
            except Exception as e:
                print(f"  ERROR downloading {s3_key}: {e}")
                error_count += len(group)
                continue

            # Split the HTML into per-case sections.
            case_htmls = _split_cases_html(raw_html)

            # Build a map from case number to HTML-extracted text.
            chunk_map: dict[str, str] = {}
            chunk_list: list[tuple[str | None, str]] = []
            for html_chunk in case_htmls:
                text = _extract_text_from_html(html_chunk)
                case_num = _extract_case_number(text)
                if case_num:
                    chunk_map[case_num] = text
                chunk_list.append((case_num, text))

            for ruling in group:
                ruling_id = ruling["ruling_id"]
                uuid_ver = ruling["uuid_ver"]
                text_length = ruling["text_length"]

                # For v5 (split children): find the matching chunk and
                # update ruling_text.  The case_number should already be
                # correct since the LLM extraction populated it.
                if uuid_ver == "v5":
                    # Get current case_number for matching.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT c.case_number FROM cases c "
                            "JOIN rulings r ON r.case_id = c.id "
                            "WHERE r.id = %s::uuid",
                            (ruling_id,),
                        )
                        case_row = cur.fetchone()
                    case_number = case_row[0] if case_row else None

                    matched_text: str | None = None
                    if case_number and case_number in chunk_map:
                        matched_text = chunk_map[case_number]
                    elif len(chunk_list) == 1:
                        # Single-case document — use the only chunk.
                        matched_text = chunk_list[0][1]

                    if matched_text and len(matched_text) <= 20000:
                        if args.dry_run:
                            print(
                                f"  DRY-RUN: Would update v5 ruling {ruling_id} "
                                f"({text_length} -> {len(matched_text)} chars)"
                            )
                        else:
                            text_hash = hashlib.sha256(
                                matched_text.encode("utf-8")
                            ).hexdigest()
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE rulings "
                                    "SET ruling_text = %s, "
                                    "    ruling_text_hash = %s, "
                                    "    updated_at = NOW() "
                                    "WHERE id = %s::uuid",
                                    (matched_text, text_hash, ruling_id),
                                )
                            conn.commit()
                            print(
                                f"  FIXED v5 ruling {ruling_id} "
                                f"({text_length} -> {len(matched_text)} chars)"
                            )
                        fixed_count += 1
                    elif matched_text and len(matched_text) > 20000:
                        # Genuinely long single-case ruling.
                        print(
                            f"  SKIP v5 ruling {ruling_id}: "
                            f"HTML text is still {len(matched_text)} chars "
                            f"(genuinely long ruling)"
                        )
                        skipped_count += 1
                    else:
                        print(
                            f"  SKIP v5 ruling {ruling_id}: "
                            f"no matching HTML chunk found "
                            f"(case_number={case_number}, "
                            f"chunks={len(chunk_list)})"
                        )
                        skipped_count += 1

                else:
                    # v4 (multi-case pages): These need full re-ingestion
                    # through the pipeline.  For now, if the page has multiple
                    # cases, just update the ruling_text for the first case
                    # and log the others.
                    if len(chunk_list) <= 1:
                        # Single-case page — just update ruling_text.
                        if chunk_list:
                            matched_text = chunk_list[0][1]
                            if matched_text and len(matched_text) <= 20000:
                                if args.dry_run:
                                    print(
                                        f"  DRY-RUN: Would update v4 "
                                        f"single-case ruling {ruling_id} "
                                        f"({text_length} -> "
                                        f"{len(matched_text)} chars)"
                                    )
                                else:
                                    text_hash = hashlib.sha256(
                                        matched_text.encode("utf-8")
                                    ).hexdigest()
                                    with conn.cursor() as cur:
                                        cur.execute(
                                            "UPDATE rulings "
                                            "SET ruling_text = %s, "
                                            "    ruling_text_hash = %s, "
                                            "    updated_at = NOW() "
                                            "WHERE id = %s::uuid",
                                            (matched_text, text_hash, ruling_id),
                                        )
                                    conn.commit()
                                    print(
                                        f"  FIXED v4 single-case ruling "
                                        f"{ruling_id} ({text_length} -> "
                                        f"{len(matched_text)} chars)"
                                    )
                                fixed_count += 1
                            else:
                                print(
                                    f"  SKIP v4 ruling {ruling_id}: "
                                    f"single-case but still "
                                    f"{len(matched_text) if matched_text else 0} "
                                    f"chars"
                                )
                                skipped_count += 1
                        else:
                            print(
                                f"  SKIP v4 ruling {ruling_id}: "
                                f"no case sections found in HTML"
                            )
                            skipped_count += 1
                    else:
                        # Multi-case page: update the current ruling's text
                        # to the first chunk (which matches the assigned
                        # case_number), and log that additional cases exist.
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT c.case_number FROM cases c "
                                "JOIN rulings r ON r.case_id = c.id "
                                "WHERE r.id = %s::uuid",
                                (ruling_id,),
                            )
                            case_row = cur.fetchone()
                        case_number = case_row[0] if case_row else None

                        matched_text = None
                        if case_number and case_number in chunk_map:
                            matched_text = chunk_map[case_number]
                        else:
                            # Use the first chunk as fallback.
                            matched_text = chunk_list[0][1]

                        if matched_text and len(matched_text) <= 20000:
                            if args.dry_run:
                                print(
                                    f"  DRY-RUN: Would update v4 multi-case "
                                    f"ruling {ruling_id} ({text_length} -> "
                                    f"{len(matched_text)} chars, "
                                    f"{len(chunk_list)} cases in page)"
                                )
                            else:
                                text_hash = hashlib.sha256(
                                    matched_text.encode("utf-8")
                                ).hexdigest()
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE rulings "
                                        "SET ruling_text = %s, "
                                        "    ruling_text_hash = %s, "
                                        "    updated_at = NOW() "
                                        "WHERE id = %s::uuid",
                                        (matched_text, text_hash, ruling_id),
                                    )
                                conn.commit()
                                print(
                                    f"  FIXED v4 multi-case ruling "
                                    f"{ruling_id} ({text_length} -> "
                                    f"{len(matched_text)} chars, "
                                    f"{len(chunk_list)} cases in page)"
                                )
                            fixed_count += 1
                        else:
                            print(
                                f"  SKIP v4 ruling {ruling_id}: "
                                f"matched text is "
                                f"{len(matched_text) if matched_text else 0} "
                                f"chars"
                            )
                            skipped_count += 1

        print("\nSummary:")
        print(f"  Fixed:   {fixed_count}")
        print(f"  Skipped: {skipped_count}")
        print(f"  Errors:  {error_count}")

        if args.dry_run:
            print("\n(Dry run — no changes were made)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
