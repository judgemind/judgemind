#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Backfill null case_title values for OC rulings using LLM extraction (#2637).

Finds all Orange County cases with NULL case_title and UNKNOWN-% case_number,
sends the ruling text to an LLM with a focused prompt asking it to infer party
names from context clues, and updates the case_title in the database.

This targets the ~194 OC cases that have NULL case_title post-rebuild — cases
whose ruling text was captured but the enrichment layer returned an empty or
null case_title. The UNKNOWN-% filter scopes the backfill to cases where the
case number was also unresolvable (the known problematic cluster from #780).

This is an ECS oneshot script: no local imports from scripts/, only stdlib +
installed packages.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_oc_null_titles_llm.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_oc_null_titles_llm.py -- --dry-run --limit 5
    scripts/ecs-run-task.sh scripts/backfill_oc_null_titles_llm.py

Options:
    --dry-run       Show what would be updated without writing to DB.
    --limit N       Maximum number of cases to process (default: all).
    --timeout N     Per-call LLM timeout in seconds (default: 30).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402
import structlog  # noqa: E402

from framework.logging import configure_structlog  # noqa: E402
from ingestion.llm_providers import call_llm  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# LLM prompt for case title extraction from ruling text
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a legal document parser. You will receive the text of a California "
    "court ruling that lacks a formal party caption block.\n\n"
    "Your task is to infer the case title (party names) from context clues in "
    "the ruling text. Look for:\n"
    "1. Proper nouns associated with 'Plaintiff', 'Defendant', 'Petitioner', "
    "'Respondent', 'Cross-Complainant', 'Cross-Defendant'\n"
    "2. Party names mentioned in phrases like 'Plaintiff Smith's motion', "
    "'Defendant Jones argues', 'counsel for Acme Corp'\n"
    "3. Names in the ruling body that are clearly litigants\n\n"
    "## Output format\n\n"
    "Respond with ONLY a JSON object:\n"
    '{"case_title": "Plaintiff Name v. Defendant Name"}\n\n'
    "Rules:\n"
    "- Use 'v.' as the separator (not 'vs.' or 'vs')\n"
    "- Use title case for names\n"
    "- If multiple plaintiffs/defendants, use the first name + ', et al.'\n"
    "- Strip legal entity descriptors ('an individual', 'a corporation', etc.)\n"
    "- If you truly cannot determine any party names, respond with:\n"
    '  {"case_title": null}\n'
    "- Do NOT guess or fabricate names — only extract names that actually "
    "appear in the text\n"
)

# Procedural words that should never appear in a valid case title.
_PROCEDURAL_WORDS = frozenset(
    {
        "granted",
        "denied",
        "motion",
        "hearing",
        "tentative",
        "department",
        "ruling",
        "demurrer",
        "order",
    }
)


def parse_llm_title(response_text: str) -> str | None:
    """Parse a case title from an LLM JSON response.

    Handles markdown code fences and basic validation.  Returns the title
    string or ``None`` if the response is invalid or the LLM could not
    determine party names.
    """
    raw = response_text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw = "\n".join(lines)

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    title = data.get("case_title")
    if not title or not isinstance(title, str):
        return None

    # Basic validation
    title = title.strip()
    if len(title) < 5 or len(title) > 150:
        return None

    # Reject titles that look like procedural text
    lower_title = title.lower()
    if any(word in lower_title for word in _PROCEDURAL_WORDS):
        return None

    return title


def extract_title_via_llm(
    ruling_text: str,
    *,
    provider: str = "google",
    model: str = "gemini-2.5-flash-lite",
    timeout: float = 30.0,
) -> str | None:
    """Send ruling text to LLM and extract a case title.

    Returns the extracted title string, or None if the LLM cannot determine
    party names.
    """
    # Truncate very long ruling texts to save tokens — party names are
    # typically mentioned in the first portion of the ruling.
    truncated = ruling_text[:4000] if len(ruling_text) > 4000 else ruling_text

    response = call_llm(
        system_prompt=SYSTEM_PROMPT,
        user_message=truncated,
        provider=provider,
        model=model,
        max_tokens=256,
        timeout=timeout,
    )

    if response is None:
        return None

    return parse_llm_title(response.text)


def fetch_null_title_cases(
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Fetch OC cases with null case_title and UNKNOWN-% case_number."""
    query = """
        SELECT
            ca.id AS case_id,
            ca.case_number,
            r.ruling_text
        FROM cases ca
        JOIN courts co ON ca.court_id = co.id
        JOIN LATERAL (
            SELECT r2.ruling_text
            FROM rulings r2
            WHERE r2.case_id = ca.id
              AND r2.ruling_text IS NOT NULL
            ORDER BY r2.hearing_date DESC
            LIMIT 1
        ) r ON TRUE
        WHERE co.county = 'Orange'
          AND ca.case_title IS NULL
          AND ca.case_number LIKE 'UNKNOWN-%'
        ORDER BY ca.case_number
    """
    if limit:
        query += f" LIMIT {limit}"

    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    return [
        {
            "case_id": str(row[0]),
            "case_number": row[1],
            "ruling_text": row[2],
        }
        for row in rows
    ]


def update_case_title(
    conn: psycopg.Connection,
    case_id: str,
    case_title: str,
) -> None:
    """Update the case_title for a specific case."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE cases
            SET case_title = %s, updated_at = NOW()
            WHERE id = %s::uuid AND case_title IS NULL
            """,
            (case_title, case_id),
        )


def main() -> None:
    """Run the OC case title backfill."""
    parser = argparse.ArgumentParser(
        description="Backfill null case_title values for OC rulings using LLM.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to DB.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cases to process.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-call LLM timeout in seconds (default: 30).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        cases = fetch_null_title_cases(conn, limit=args.limit)
        logger.info("Found cases with null case_title", count=len(cases))

        if not cases:
            logger.info("No cases to process — all OC UNKNOWN cases have titles")
            return

        updated = 0
        failed = 0
        skipped = 0

        for case in cases:
            case_number = case["case_number"]
            ruling_text = case["ruling_text"]

            if not ruling_text or len(ruling_text.strip()) < 50:
                logger.info(
                    "Skipping case with insufficient ruling text",
                    case_number=case_number,
                )
                skipped += 1
                continue

            title = extract_title_via_llm(
                ruling_text,
                timeout=args.timeout,
            )

            if title:
                if args.dry_run:
                    logger.info(
                        "DRY RUN: would update case_title",
                        case_number=case_number,
                        case_title=title,
                    )
                else:
                    update_case_title(conn, case["case_id"], title)
                    conn.commit()
                    logger.info(
                        "Updated case_title",
                        case_number=case_number,
                        case_title=title,
                    )
                updated += 1
            else:
                logger.info(
                    "LLM could not extract case_title",
                    case_number=case_number,
                )
                failed += 1

            # Rate limiting: small delay between LLM calls
            time.sleep(0.5)

        logger.info(
            "Backfill complete",
            total=len(cases),
            updated=updated,
            failed=failed,
            skipped=skipped,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
