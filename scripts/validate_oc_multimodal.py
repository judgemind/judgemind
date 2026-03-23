#!/usr/bin/env python3
# venv: scraper-framework
"""Validate multimodal extraction against every OC ruling in the dev DB.

For each OC document in the database, fetches the raw PDF from S3, runs
per-page multimodal extraction via LlmExtractor, and compares the extracted
case count and ruling text against the existing DB values.

Usage:
    scripts/with-secret.sh \
        -e DATABASE_URL=judgemind/dev/db/connection:.url \
        -e GOOGLE_API_KEY=judgemind/dev/google-api-key \
        -- packages/scraper-framework/.venv/bin/python3 \
        scripts/validate_oc_multimodal.py

Options:
    --limit N       Maximum number of documents to validate (default: all).
    --dry-run       Show documents that would be validated without calling LLM.
    --json          Machine-readable JSON output.
    --verbose       Show full ruling text diffs for mismatches.
    --date-from     Only validate documents captured on or after this date.
    --date-to       Only validate documents captured on or before this date.

Exit code: 0 if all match, 1 if discrepancies found.
"""

from __future__ import annotations

import argparse
import json as json_mod
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher

# Ensure the scraper-framework source is importable.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import psycopg  # noqa: E402

from framework.llm_extractor import LlmExtractor  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL — fetch OC documents with their ruling data
# ---------------------------------------------------------------------------

DOCUMENTS_QUERY = """
    SELECT
        d.id            AS doc_id,
        d.s3_key,
        d.s3_bucket,
        d.format,
        d.captured_at,
        COUNT(r.id)     AS ruling_count,
        ARRAY_AGG(
            jsonb_build_object(
                'ruling_id',     r.id,
                'case_number',   c.case_number,
                'case_title',    c.case_title,
                'ruling_text',   r.ruling_text
            )
            ORDER BY c.case_number
        ) AS rulings
    FROM documents d
    JOIN courts ct ON ct.id = d.court_id
    JOIN rulings r ON r.document_id = d.id
    JOIN cases c   ON c.id = r.case_id
    WHERE ct.county = 'Orange'
      AND d.format = 'pdf'
      AND d.s3_key IS NOT NULL
      AND d.s3_bucket IS NOT NULL
      {date_filter}
    GROUP BY d.id, d.s3_key, d.s3_bucket, d.format, d.captured_at
    ORDER BY d.captured_at DESC
    {limit_clause}
"""


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of validating one document."""

    doc_id: str
    s3_key: str
    captured_at: str
    db_ruling_count: int
    extracted_ruling_count: int
    case_count_match: bool
    text_similarity_avg: float
    mismatches: list[dict] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------


def _text_similarity(a: str, b: str) -> float:
    """Compute text similarity ratio (0.0 to 1.0)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _normalize_text(text: str | None) -> str:
    """Normalize ruling text for comparison."""
    if not text:
        return ""
    # Strip whitespace, normalize line endings.
    return " ".join(text.split())


def validate_document(
    extractor: LlmExtractor,
    s3_client: object,
    doc_id: str,
    s3_key: str,
    s3_bucket: str,
    db_rulings: list[dict],
) -> ValidationResult:
    """Validate extraction for a single document against DB values."""
    captured_at = ""
    try:
        # Fetch PDF from S3.
        response = s3_client.get_object(  # type: ignore[union-attr]
            Bucket=s3_bucket, Key=s3_key
        )
        pdf_bytes = response["Body"].read()

        # Run per-page multimodal extraction.
        extracted = extractor.extract_from_pdf(pdf_bytes)

        db_count = len(db_rulings)
        ext_count = len(extracted)
        count_match = db_count == ext_count

        # Compare ruling texts — match by case number where possible.
        # Use defaultdict(list) to handle duplicate case numbers correctly.
        db_by_case: dict[str, list[dict]] = defaultdict(list)
        for r in db_rulings:
            if r:
                db_by_case[r.get("case_number", "")].append(r)
        ext_by_case: dict[str, list[object]] = defaultdict(list)
        for r in extracted:
            ext_by_case[r.extracted_case_number or ""].append(r)

        similarities: list[float] = []
        mismatches: list[dict] = []

        for case_num, db_ruling_list in db_by_case.items():
            ext_ruling_list = ext_by_case.get(case_num, [])
            if not ext_ruling_list:
                for db_ruling in db_ruling_list:
                    mismatches.append(
                        {
                            "case_number": case_num,
                            "type": "missing_extraction",
                            "db_title": db_ruling.get("case_title", ""),
                        }
                    )
                    similarities.append(0.0)
                continue

            # Compare pairwise (zip longest, treating extras as mismatches).
            for i, db_ruling in enumerate(db_ruling_list):
                if i < len(ext_ruling_list):
                    ext_ruling = ext_ruling_list[i]
                    db_text = _normalize_text(db_ruling.get("ruling_text"))
                    ext_text = _normalize_text(ext_ruling.ruling_text)
                    sim = _text_similarity(db_text, ext_text)
                    similarities.append(sim)

                    if sim < 0.8:
                        mismatches.append(
                            {
                                "case_number": case_num,
                                "type": "text_mismatch",
                                "similarity": round(sim, 3),
                                "db_title": db_ruling.get("case_title", ""),
                                "db_text_len": len(db_text),
                                "ext_text_len": len(ext_text),
                            }
                        )
                else:
                    mismatches.append(
                        {
                            "case_number": case_num,
                            "type": "missing_extraction",
                            "db_title": db_ruling.get("case_title", ""),
                        }
                    )
                    similarities.append(0.0)

        # Check for extra extracted cases not in DB and over-extraction.
        for case_num, ext_list in ext_by_case.items():
            db_list = db_by_case.get(case_num, [])
            if not db_list:
                # Entirely new case number not in DB.
                if case_num:
                    mismatches.append(
                        {
                            "case_number": case_num,
                            "type": "extra_extraction",
                        }
                    )
            elif len(ext_list) > len(db_list):
                # More rulings extracted than exist in DB for this case.
                for i in range(len(db_list), len(ext_list)):
                    mismatches.append(
                        {
                            "case_number": case_num,
                            "type": "extra_ruling_for_case",
                            "extra_index": i,
                        }
                    )

        avg_sim = sum(similarities) / len(similarities) if similarities else 0.0

        return ValidationResult(
            doc_id=doc_id,
            s3_key=s3_key,
            captured_at=captured_at,
            db_ruling_count=db_count,
            extracted_ruling_count=ext_count,
            case_count_match=count_match,
            text_similarity_avg=round(avg_sim, 3),
            mismatches=mismatches,
        )

    except Exception as exc:
        return ValidationResult(
            doc_id=doc_id,
            s3_key=s3_key,
            captured_at=captured_at,
            db_ruling_count=len(db_rulings),
            extracted_ruling_count=0,
            case_count_match=False,
            text_similarity_avg=0.0,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:  # noqa: ANN201
    """Run OC multimodal validation."""
    parser = argparse.ArgumentParser(
        description="Validate multimodal extraction against OC dev DB."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to validate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List documents without running extraction.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Machine-readable JSON output.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full text diffs for mismatches.",
    )
    parser.add_argument(
        "--date-from",
        type=date.fromisoformat,
        default=None,
        help="Only validate docs captured on or after this date.",
    )
    parser.add_argument(
        "--date-to",
        type=date.fromisoformat,
        default=None,
        help="Only validate docs captured on or before this date.",
    )
    args = parser.parse_args()

    # Build query.
    date_parts: list[str] = []
    if args.date_from:
        date_parts.append(f"AND d.captured_at >= '{args.date_from.isoformat()}'")
    if args.date_to:
        date_parts.append(f"AND d.captured_at <= '{args.date_to.isoformat()}'")
    date_filter = " ".join(date_parts)
    limit_clause = f"LIMIT {args.limit}" if args.limit else ""

    query = DOCUMENTS_QUERY.format(
        date_filter=date_filter,
        limit_clause=limit_clause,
    )

    # Connect to DB.
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required.")
        return 1

    logger.info("Connecting to database...")
    conn = psycopg.connect(db_url)

    logger.info("Querying OC PDF documents...")
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    logger.info("Found %d OC PDF documents to validate.", len(rows))

    if args.dry_run:
        for row in rows:
            doc_id, s3_key, _, _, captured_at, ruling_count, _ = row
            logger.info(
                "  doc=%s  s3=%s  captured=%s  rulings=%d",
                doc_id,
                s3_key,
                captured_at,
                ruling_count,
            )
        return 0

    # Initialize extractor and S3 client.
    google_key = os.environ.get("GOOGLE_API_KEY")
    if not google_key:
        logger.error("GOOGLE_API_KEY environment variable is required.")
        return 1

    extractor = LlmExtractor(
        provider="google",
        model="gemini-2.5-flash-lite",
        api_key=google_key,
    )
    s3_client = boto3.client("s3")

    # Validate each document.
    results: list[ValidationResult] = []
    total_match = 0
    total_mismatch = 0
    total_error = 0

    for i, row in enumerate(rows):
        doc_id, s3_key, s3_bucket, _, captured_at, ruling_count, rulings = row
        logger.info(
            "[%d/%d] Validating doc=%s (%d rulings)...",
            i + 1,
            len(rows),
            doc_id,
            ruling_count,
        )

        result = validate_document(
            extractor=extractor,
            s3_client=s3_client,
            doc_id=str(doc_id),
            s3_key=s3_key,
            s3_bucket=s3_bucket,
            db_rulings=rulings if rulings else [],
        )
        result.captured_at = str(captured_at) if captured_at else ""
        results.append(result)

        if result.error:
            total_error += 1
            logger.warning(
                "  ERROR: %s",
                result.error,
            )
        elif result.case_count_match and not result.mismatches:
            total_match += 1
            logger.info(
                "  MATCH: %d rulings, avg similarity=%.3f",
                result.db_ruling_count,
                result.text_similarity_avg,
            )
        else:
            total_mismatch += 1
            logger.warning(
                "  MISMATCH: db=%d extracted=%d, avg similarity=%.3f, issues=%d",
                result.db_ruling_count,
                result.extracted_ruling_count,
                result.text_similarity_avg,
                len(result.mismatches),
            )
            if args.verbose:
                for m in result.mismatches:
                    logger.warning("    %s", m)

    # Summary.
    total = len(results)
    logger.info("=" * 60)
    logger.info("Validation complete: %d documents", total)
    logger.info(
        "  Match: %d (%.1f%%)",
        total_match,
        100.0 * total_match / total if total else 0,
    )
    logger.info(
        "  Mismatch: %d (%.1f%%)",
        total_mismatch,
        100.0 * total_mismatch / total if total else 0,
    )
    logger.info(
        "  Error: %d (%.1f%%)",
        total_error,
        100.0 * total_error / total if total else 0,
    )

    if args.json_output:
        output = {
            "total_documents": total,
            "matches": total_match,
            "mismatches": total_mismatch,
            "errors": total_error,
            "match_rate": round(total_match / total if total else 0, 3),
            "results": [
                {
                    "doc_id": r.doc_id,
                    "s3_key": r.s3_key,
                    "captured_at": r.captured_at,
                    "db_ruling_count": r.db_ruling_count,
                    "extracted_ruling_count": r.extracted_ruling_count,
                    "case_count_match": r.case_count_match,
                    "text_similarity_avg": r.text_similarity_avg,
                    "mismatches": r.mismatches,
                    "error": r.error,
                }
                for r in results
            ],
        }
        print(json_mod.dumps(output, indent=2))

    conn.close()
    return 1 if total_mismatch > 0 or total_error > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
