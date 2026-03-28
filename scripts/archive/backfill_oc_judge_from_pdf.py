#!/usr/bin/env python3
"""Backfill missing judge_id on OC rulings by extracting judge names from PDF headers.

Each OC tentative ruling PDF has a header like:
    TENTATIVE RULINGS / LAW & MOTION / DEPT C25 / Judge Gassia Apkarian

The multimodal reingest path (#2063) lost the scraper-provided judge_name when
the LLM didn't extract one, leaving ~1000 rulings without judge_id.  This script
reads the archived PDFs from S3, extracts the judge name from the header via regex,
resolves it to a judge record, and updates the rulings.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_oc_judge_from_pdf.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_oc_judge_from_pdf.py -- --apply

Options:
    --dry-run   Report what would change without modifying the database (default).
    --apply     Actually update rulings in the database.
    --limit N   Maximum number of unique S3 keys (PDFs) to process.

See: https://github.com/judgemind/judgemind/issues/2063
"""

# venv: scraper-framework
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
from collections import defaultdict
from typing import Any

# Ensure the scraper-framework source is importable.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import boto3  # noqa: E402
import pdfplumber  # noqa: E402
import psycopg  # noqa: E402

from ingestion.db import resolve_judge  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Judge name extraction patterns (inlined from ingestion/extract.py)
# ---------------------------------------------------------------------------
# These are the patterns most likely to match OC PDF headers. The full set in
# extract.py is larger, but we only need the relevant subset here.

_JUDGE_NAME_PATTERNS: list[re.Pattern[str]] = [
    # "Judge Gassia Apkarian" — standalone "Judge Name" in headers.
    # Uses literal spaces to stay on one line.
    re.compile(
        r"(?<![a-zA-Z])"
        r"Judge[:  ][ ]*"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"  # first name
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"  # middle initials/names
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"  # last name
        r")"
        r"(?![ ]+of\b)",
    ),
    # ALL-CAPS: "JUDGE GASSIA APKARIAN"
    re.compile(
        r"(?:DEPARTMENT\s+\S+\s+)?"
        r"JUDGE\s+"
        r"(?P<judge_name>"
        r"[A-Z]{2,}"  # first name (all caps, 2+ chars)
        r"(?:\s+[A-Z]\.?)*"  # optional middle initials
        r"\s+[A-Z]{2,}"  # last name (all caps, 2+ chars)
        r"(?:-[A-Z]{2,})?"  # optional hyphenated surname
        r")"
        r"(?!\s+[Oo][Ff]\b)",
    ),
    # "Honorable Firstname Lastname"
    re.compile(
        r"\bHon(?:orable)?\.?[ ]+"
        r"(?P<judge_name>"
        r"[A-Z][a-z]+"
        r"(?:[ ]+[A-Z][a-z.'\-]*)*"
        r"[ ]+[A-Z][a-z]+(?:-[A-Z][a-z]+)*"
        r")",
    ),
    # "William A. Crowfoot Judge of the Superior Court"
    re.compile(
        r"([^\n]+?)\s+Judge of the Superior Court",
        re.IGNORECASE,
    ),
]

# Department extraction from OC PDF header: "DEPT C25"
_DEPT_RE = re.compile(r"\bDEPT\.?\s+(\S+)", re.IGNORECASE)


def _looks_like_person_name(name: str) -> bool:
    """Reject obvious non-person names (organizations, single words, etc.).

    Mirrors the check in ingestion/extract.py to avoid false positives.
    """
    parts = name.split()
    if len(parts) < 2:
        return False
    # Reject all-numeric or very short fragments
    if all(len(p) <= 1 for p in parts):
        return False
    # Reject common false positive patterns
    lower = name.lower()
    if any(
        kw in lower
        for kw in (
            "court",
            "county",
            "department",
            "tentative",
            "motion",
            "ruling",
            "calendar",
        )
    ):
        return False
    return True


def extract_judge_name(text: str) -> str | None:
    """Extract a judge name from ruling text using regex patterns."""
    for pattern in _JUDGE_NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                name = m.group("judge_name")
            except IndexError:
                name = m.group(1)
            name = " ".join(name.strip().split())
            if name and _looks_like_person_name(name):
                return name
    return None


def extract_department(text: str) -> str | None:
    """Extract department code from OC PDF header (e.g., 'DEPT C25' -> 'C25')."""
    m = _DEPT_RE.search(text)
    if m:
        return m.group(1)
    return None


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract full text from a PDF using pdfplumber."""
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------

# SQL to find OC rulings without judge_id, grouped by S3 key.
FIND_MISSING_JUDGE_QUERY = """
    SELECT
        r.id            AS ruling_id,
        d.s3_key,
        d.s3_bucket,
        d.court_id,
        r.department
    FROM rulings r
    JOIN documents d ON d.id = r.document_id
    JOIN courts ct ON ct.id = d.court_id
    WHERE ct.county = 'Orange'
      AND r.judge_id IS NULL
      AND d.status = 'active'
    ORDER BY d.s3_key
"""

UPDATE_RULING_JUDGE_QUERY = """
    UPDATE rulings
    SET judge_id = %s,
        department = COALESCE(department, %s),
        updated_at = NOW()
    WHERE id = %s
      AND judge_id IS NULL
"""


def run_backfill(
    dsn: str,
    *,
    apply: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Backfill judge_id on OC rulings from PDF headers.

    Returns a dict with summary statistics.
    """
    stats: dict[str, Any] = {
        "total_missing": 0,
        "unique_s3_keys": 0,
        "s3_keys_processed": 0,
        "rulings_updated": 0,
        "rulings_still_missing": 0,
        "s3_keys_no_judge": [],
    }

    s3 = boto3.client("s3")

    with psycopg.connect(dsn, autocommit=False) as conn:
        # Step 1: Find all OC rulings missing judge_id
        with conn.cursor() as cur:
            cur.execute(FIND_MISSING_JUDGE_QUERY)
            rows = cur.fetchall()

        stats["total_missing"] = len(rows)
        logger.info("Found %d OC rulings missing judge_id", len(rows))

        if not rows:
            logger.info("Nothing to backfill.")
            return stats

        # Step 2: Group by S3 key
        s3_key_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ruling_id, s3_key, s3_bucket, court_id, department in rows:
            s3_key_groups[s3_key].append(
                {
                    "ruling_id": str(ruling_id),
                    "s3_bucket": s3_bucket,
                    "court_id": str(court_id),
                    "department": department,
                }
            )

        stats["unique_s3_keys"] = len(s3_key_groups)
        logger.info("Grouped into %d unique S3 keys (PDFs)", len(s3_key_groups))

        # Step 3: Process each S3 key
        keys_to_process = list(s3_key_groups.items())
        if limit is not None:
            keys_to_process = keys_to_process[:limit]

        for s3_key, ruling_group in keys_to_process:
            stats["s3_keys_processed"] += 1
            bucket = ruling_group[0]["s3_bucket"]
            court_id = ruling_group[0]["court_id"]

            # Download PDF from S3
            try:
                obj = s3.get_object(Bucket=bucket, Key=s3_key)
                pdf_bytes = obj["Body"].read()
            except Exception:
                logger.warning(
                    "Failed to download S3 object",
                    exc_info=True,
                    extra={"s3_key": s3_key, "bucket": bucket},
                )
                stats["rulings_still_missing"] += len(ruling_group)
                stats["s3_keys_no_judge"].append(s3_key)
                continue

            # Extract text and judge name
            try:
                text = extract_pdf_text(pdf_bytes)
            except Exception:
                logger.warning(
                    "Failed to extract text from PDF",
                    exc_info=True,
                    extra={"s3_key": s3_key},
                )
                stats["rulings_still_missing"] += len(ruling_group)
                stats["s3_keys_no_judge"].append(s3_key)
                continue

            judge_name = extract_judge_name(text)
            dept = extract_department(text)

            if not judge_name:
                logger.info(
                    "No judge name found in PDF header",
                    extra={
                        "s3_key": s3_key,
                        "ruling_count": len(ruling_group),
                    },
                )
                stats["rulings_still_missing"] += len(ruling_group)
                stats["s3_keys_no_judge"].append(s3_key)
                continue

            # Resolve judge
            judge_id = resolve_judge(conn, judge_name, court_id)
            if not judge_id:
                logger.warning(
                    "resolve_judge returned None for %r at court %s",
                    judge_name,
                    court_id,
                )
                stats["rulings_still_missing"] += len(ruling_group)
                stats["s3_keys_no_judge"].append(s3_key)
                continue

            # Update rulings
            updated_in_group = 0
            with conn.cursor() as cur:
                for ruling_info in ruling_group:
                    # Use existing department if present, otherwise use extracted
                    effective_dept = ruling_info["department"] or dept
                    cur.execute(
                        UPDATE_RULING_JUDGE_QUERY,
                        (judge_id, effective_dept, ruling_info["ruling_id"]),
                    )
                    if cur.rowcount > 0:
                        updated_in_group += 1

            if apply:
                conn.commit()
            else:
                conn.rollback()

            stats["rulings_updated"] += updated_in_group
            logger.info(
                "Processed PDF: judge=%r, dept=%s, updated=%d/%d rulings",
                judge_name,
                dept,
                updated_in_group,
                len(ruling_group),
                extra={"s3_key": s3_key},
            )

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill missing judge_id on OC rulings by extracting "
            "judge names from archived PDF headers."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update rulings in the database. Without this flag, runs in dry-run mode (default).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of unique S3 keys (PDFs) to process.",
    )
    args = parser.parse_args()

    apply = args.apply

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Mode: %s", "APPLY" if apply else "DRY-RUN")

    stats = run_backfill(dsn, apply=apply, limit=args.limit)

    logger.info(
        "Summary: %d rulings missing judge_id, %d unique PDFs (%d processed), "
        "%d rulings %s, %d still missing",
        stats["total_missing"],
        stats["unique_s3_keys"],
        stats["s3_keys_processed"],
        stats["rulings_updated"],
        "updated" if apply else "would be updated",
        stats["rulings_still_missing"],
    )

    if stats["s3_keys_no_judge"]:
        logger.info(
            "PDFs where no judge name could be extracted (%d):",
            len(stats["s3_keys_no_judge"]),
        )
        for key in stats["s3_keys_no_judge"]:
            logger.info("  %s", key)


if __name__ == "__main__":
    main()
