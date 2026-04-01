#!/usr/bin/env python3
"""Backfill existing rulings through LLM enrichment extraction.

Re-runs LLM enrichment on existing rulings to fix data quality gaps:
null motion_type, null outcome, contaminated case_titles, incorrect
party extraction.  This is a text-only operation — it reads the
existing ruling_text and extracts structured fields via the LLM
enrichment prompt.

Usage (ECS):
    scripts/ecs-run-task.sh scripts/backfill_llm_enrichment.py -- --all
    scripts/ecs-run-task.sh scripts/backfill_llm_enrichment.py -- --county Riverside --dry-run

Usage (local):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -e GOOGLE_API_KEY=judgemind/dev/google_api_key \\
        -- packages/scraper-framework/.venv/bin/python3 \\
           scripts/backfill_llm_enrichment.py --all

Options:
    --county NAME     Scope to a single county.
    --all             Process all counties (required if --county not given).
    --dry-run         Show what would change without writing to DB.
    --force           Re-enrich even if fields are already populated.
    --batch-size N    Rulings per DB batch (default: 50).
    --concurrency N   Parallel LLM call threads (default: 8).
    --limit N         Maximum total rulings to process.
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)

import psycopg  # noqa: E402
import structlog  # noqa: E402

from framework.llm_enrichment import enrich_ruling  # noqa: E402
from framework.logging import configure_structlog  # noqa: E402
from ingestion.db import batch_upsert_parties  # noqa: E402
from ingestion.llm_providers import create_client as create_llm_client  # noqa: E402

configure_structlog(contextvars=True)
logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------


@dataclass
class CountyStats:
    """Per-county statistics for the backfill."""

    county: str
    total_rulings: int = 0
    processed: int = 0
    skipped_no_text: int = 0
    skipped_already_populated: int = 0
    llm_api_failures: int = 0
    skipped_no_update: int = 0
    updated_motion_type: int = 0
    updated_outcome: int = 0
    updated_case_title: int = 0
    updated_parties: int = 0
    errors: int = 0

    # Before/after null counts
    before_null_motion_type: int = 0
    before_null_outcome: int = 0
    before_null_case_title: int = 0
    after_null_motion_type: int = 0
    after_null_outcome: int = 0
    after_null_case_title: int = 0


@dataclass
class BackfillStats:
    """Aggregate statistics across all counties."""

    counties: dict[str, CountyStats] = field(default_factory=dict)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    start_time: float = 0.0

    def get_county(self, county: str) -> CountyStats:
        if county not in self.counties:
            self.counties[county] = CountyStats(county=county)
        return self.counties[county]

    def totals(self) -> dict[str, int]:
        return {
            "total_rulings": sum(c.total_rulings for c in self.counties.values()),
            "processed": sum(c.processed for c in self.counties.values()),
            "skipped_no_text": sum(c.skipped_no_text for c in self.counties.values()),
            "skipped_already_populated": sum(
                c.skipped_already_populated for c in self.counties.values()
            ),
            "llm_api_failures": sum(c.llm_api_failures for c in self.counties.values()),
            "skipped_no_update": sum(
                c.skipped_no_update for c in self.counties.values()
            ),
            "updated_motion_type": sum(
                c.updated_motion_type for c in self.counties.values()
            ),
            "updated_outcome": sum(c.updated_outcome for c in self.counties.values()),
            "updated_case_title": sum(
                c.updated_case_title for c in self.counties.values()
            ),
            "updated_parties": sum(c.updated_parties for c in self.counties.values()),
            "errors": sum(c.errors for c in self.counties.values()),
        }


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


def fetch_counties(conn: psycopg.Connection) -> list[str]:
    """Get all county names from the courts table."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT county FROM courts ORDER BY county")
        return [row[0] for row in cur.fetchall()]


def fetch_before_stats(conn: psycopg.Connection, county: str) -> dict[str, int]:
    """Get null counts for key fields before the backfill."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE r.motion_type IS NULL) AS null_motion_type,
                COUNT(*) FILTER (WHERE r.outcome IS NULL) AS null_outcome,
                COUNT(*) FILTER (WHERE c.case_title IS NULL OR c.case_title = '') AS null_case_title
            FROM rulings r
            JOIN cases c ON r.case_id = c.id
            JOIN courts ct ON c.court_id = ct.id
            WHERE ct.county = %s
            """,
            (county,),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "total": 0,
                "null_motion_type": 0,
                "null_outcome": 0,
                "null_case_title": 0,
            }
        return {
            "total": row[0],
            "null_motion_type": row[1],
            "null_outcome": row[2],
            "null_case_title": row[3],
        }


def fetch_rulings_batch(
    conn: psycopg.Connection,
    county: str,
    *,
    force: bool = False,
    batch_size: int = 50,
    offset: int = 0,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch a batch of rulings for enrichment.

    Returns rulings joined with case and court info.  By default only
    fetches rulings where at least one enrichment field is missing.
    With ``force=True``, fetches all rulings with ruling_text.
    """
    conditions = ["ct.county = %s", "r.ruling_text IS NOT NULL", "r.ruling_text != ''"]
    params: list[Any] = [county]

    if not force:
        conditions.append(
            "(r.motion_type IS NULL OR r.outcome IS NULL "
            "OR c.case_title IS NULL OR c.case_title = '')"
        )

    effective_limit = batch_size
    if limit is not None:
        remaining = limit - offset
        if remaining <= 0:
            return []
        effective_limit = min(batch_size, remaining)

    query = f"""
        SELECT
            r.id AS ruling_id,
            r.case_id,
            r.ruling_text,
            r.motion_type,
            r.outcome::text AS outcome,
            c.case_title,
            c.id AS case_id_check
        FROM rulings r
        JOIN cases c ON r.case_id = c.id
        JOIN courts ct ON c.court_id = ct.id
        WHERE {" AND ".join(conditions)}
        ORDER BY r.created_at
        OFFSET %s
        LIMIT %s
    """  # noqa: S608
    params.extend([offset, effective_limit])

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# LLM enrichment worker
# ---------------------------------------------------------------------------


_SKIPPED_NO_UPDATE = "skipped_no_update"
_LLM_API_FAILURE = "llm_api_failure"


def enrich_one_ruling(
    ruling: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    client: object | None,
    force: bool,
) -> dict[str, Any] | str:
    """Enrich a single ruling via the LLM.

    Returns one of:
    - A dict with the ruling_id, case_id, and extracted fields (success
      with updates).
    - ``"skipped_no_update"`` if the LLM responded successfully but no
      fields were updated (text too short, or extracted values were all
      None).
    - ``"llm_api_failure"`` if the LLM call itself failed (503, timeout,
      auth error, etc.).
    """
    ruling_text = ruling["ruling_text"]
    ruling_id = ruling["ruling_id"]
    case_id = ruling["case_id"]

    # Determine which fields need filling
    needs_motion_type = force or ruling["motion_type"] is None
    needs_outcome = force or ruling["outcome"] is None
    needs_case_title = force or not ruling["case_title"]

    if not needs_motion_type and not needs_outcome and not needs_case_title:
        return _SKIPPED_NO_UPDATE

    result = enrich_ruling(
        ruling_text,
        provider=provider,
        model=model,
        client=client,
    )

    if result is None:
        return _LLM_API_FAILURE

    # Check if the result has any useful data
    has_updates = False
    updates: dict[str, Any] = {
        "ruling_id": str(ruling_id),
        "case_id": str(case_id),
        "result": result,
    }

    if needs_motion_type and result.motion_type is not None:
        updates["new_motion_type"] = result.motion_type
        has_updates = True

    if needs_outcome and result.outcome is not None:
        updates["new_outcome"] = result.outcome
        has_updates = True

    if needs_case_title and result.case_title is not None:
        updates["new_case_title"] = result.case_title
        has_updates = True

    if result.parties.plaintiffs or result.parties.defendants:
        updates["new_parties"] = []
        for name in result.parties.plaintiffs:
            updates["new_parties"].append({"name": name, "role": "plaintiff"})
        for name in result.parties.defendants:
            updates["new_parties"].append({"name": name, "role": "defendant"})
        has_updates = True

    return updates if has_updates else _SKIPPED_NO_UPDATE


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------


def apply_updates(
    conn: psycopg.Connection,
    updates: list[dict[str, Any]],
    stats: CountyStats,
    *,
    dry_run: bool = False,
) -> None:
    """Apply enrichment updates to the database.

    Updates ruling motion_type/outcome, case title, and parties.
    """
    for update in updates:
        ruling_id = update["ruling_id"]
        case_id = update["case_id"]

        # Update ruling fields (motion_type, outcome)
        ruling_sets: list[str] = []
        ruling_params: list[Any] = []

        if "new_motion_type" in update:
            ruling_sets.append("motion_type = %s")
            ruling_params.append(update["new_motion_type"])
            stats.updated_motion_type += 1

        if "new_outcome" in update:
            ruling_sets.append("outcome = %s::ruling_outcome")
            ruling_params.append(update["new_outcome"])
            stats.updated_outcome += 1

        if ruling_sets:
            ruling_sets.append("updated_at = NOW()")
            ruling_params.append(ruling_id)
            if not dry_run:
                with conn.cursor() as cur:
                    query = f"UPDATE rulings SET {', '.join(ruling_sets)} WHERE id = %s::uuid"  # noqa: S608
                    cur.execute(query, ruling_params)

        # Update case title
        if "new_case_title" in update:
            stats.updated_case_title += 1
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cases SET case_title = %s, updated_at = NOW() WHERE id = %s::uuid",
                        (update["new_case_title"], case_id),
                    )

        # Update parties
        if "new_parties" in update and update["new_parties"]:
            stats.updated_parties += 1
            if not dry_run:
                batch_upsert_parties(
                    conn,
                    case_id,
                    update["new_parties"],
                    alias_source="llm_enrichment",
                )

    if not dry_run:
        conn.commit()


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------


def process_county(
    conn: psycopg.Connection,
    county: str,
    stats: BackfillStats,
    *,
    provider: str,
    model: str | None,
    client: object | None,
    force: bool,
    dry_run: bool,
    batch_size: int,
    concurrency: int,
    limit: int | None,
) -> None:
    """Process all rulings in a county through LLM enrichment."""
    county_stats = stats.get_county(county)

    # Get before stats
    before = fetch_before_stats(conn, county)
    county_stats.total_rulings = before["total"]
    county_stats.before_null_motion_type = before["null_motion_type"]
    county_stats.before_null_outcome = before["null_outcome"]
    county_stats.before_null_case_title = before["null_case_title"]

    logger.info(
        "backfill.county_start",
        county=county,
        total_rulings=before["total"],
        null_motion_type=before["null_motion_type"],
        null_outcome=before["null_outcome"],
        null_case_title=before["null_case_title"],
    )

    if before["total"] == 0:
        logger.info("backfill.county_empty", county=county)
        return

    offset = 0
    batch_num = 0
    total_processed = 0

    while True:
        batch = fetch_rulings_batch(
            conn,
            county,
            force=force,
            batch_size=batch_size,
            offset=offset,
            limit=limit,
        )

        if not batch:
            break

        batch_num += 1
        logger.info(
            "backfill.batch_start",
            county=county,
            batch=batch_num,
            size=len(batch),
            offset=offset,
        )

        # Run LLM enrichment in parallel
        updates: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(
                    enrich_one_ruling,
                    ruling,
                    provider=provider,
                    model=model,
                    client=client,
                    force=force,
                ): ruling
                for ruling in batch
            }

            for future in as_completed(futures):
                ruling = futures[future]
                county_stats.processed += 1
                total_processed += 1

                try:
                    result = future.result()
                    if result == _LLM_API_FAILURE:
                        county_stats.llm_api_failures += 1
                    elif result == _SKIPPED_NO_UPDATE:
                        county_stats.skipped_no_update += 1
                    else:
                        updates.append(result)
                except Exception:
                    logger.exception(
                        "backfill.ruling_error",
                        ruling_id=str(ruling["ruling_id"]),
                    )
                    county_stats.errors += 1

        # Apply updates
        if updates:
            apply_updates(conn, updates, county_stats, dry_run=dry_run)
            action = "would update" if dry_run else "updated"
            logger.info(
                "backfill.batch_complete",
                county=county,
                batch=batch_num,
                updates=len(updates),
                action=action,
            )
        else:
            logger.info(
                "backfill.batch_complete",
                county=county,
                batch=batch_num,
                updates=0,
            )

        offset += len(batch)

        # Check limit
        if limit is not None and total_processed >= limit:
            break

    # Get after stats
    if not dry_run:
        after = fetch_before_stats(conn, county)
        county_stats.after_null_motion_type = after["null_motion_type"]
        county_stats.after_null_outcome = after["null_outcome"]
        county_stats.after_null_case_title = after["null_case_title"]
    else:
        # Estimate after stats from what we found
        county_stats.after_null_motion_type = (
            county_stats.before_null_motion_type - county_stats.updated_motion_type
        )
        county_stats.after_null_outcome = (
            county_stats.before_null_outcome - county_stats.updated_outcome
        )
        county_stats.after_null_case_title = (
            county_stats.before_null_case_title - county_stats.updated_case_title
        )

    logger.info(
        "backfill.county_complete",
        county=county,
        processed=county_stats.processed,
        updated_motion_type=county_stats.updated_motion_type,
        updated_outcome=county_stats.updated_outcome,
        updated_case_title=county_stats.updated_case_title,
        updated_parties=county_stats.updated_parties,
        llm_api_failures=county_stats.llm_api_failures,
        skipped_no_update=county_stats.skipped_no_update,
        errors=county_stats.errors,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(stats: BackfillStats) -> None:
    """Print a summary report of the backfill."""
    elapsed = time.time() - stats.start_time
    totals = stats.totals()

    print("\n" + "=" * 70)
    print("BACKFILL SUMMARY")
    print("=" * 70)
    print(f"Elapsed:    {elapsed:.1f}s")
    print(f"Processed:  {totals['processed']}")
    print(f"API fails:  {totals['llm_api_failures']}")
    print(f"No-update:  {totals['skipped_no_update']}")
    print(f"Errors:     {totals['errors']}")
    print()

    # Per-county table
    header = (
        f"{'County':<20} {'Total':>6} {'Proc':>6} "
        f"{'MT+':>4} {'OC+':>4} {'CT+':>4} {'Pty+':>5} "
        f"{'MT% before':>10} {'MT% after':>10} "
        f"{'OC% before':>10} {'OC% after':>10}"
    )
    print(header)
    print("-" * len(header))

    for county_name in sorted(stats.counties):
        cs = stats.counties[county_name]
        if cs.total_rulings == 0:
            continue

        mt_pct_before = (
            (cs.before_null_motion_type / cs.total_rulings * 100)
            if cs.total_rulings > 0
            else 0
        )
        mt_pct_after = (
            (cs.after_null_motion_type / cs.total_rulings * 100)
            if cs.total_rulings > 0
            else 0
        )
        oc_pct_before = (
            (cs.before_null_outcome / cs.total_rulings * 100)
            if cs.total_rulings > 0
            else 0
        )
        oc_pct_after = (
            (cs.after_null_outcome / cs.total_rulings * 100)
            if cs.total_rulings > 0
            else 0
        )

        print(
            f"{cs.county:<20} {cs.total_rulings:>6} {cs.processed:>6} "
            f"{cs.updated_motion_type:>4} {cs.updated_outcome:>4} "
            f"{cs.updated_case_title:>4} {cs.updated_parties:>5} "
            f"{mt_pct_before:>9.1f}% {mt_pct_after:>9.1f}% "
            f"{oc_pct_before:>9.1f}% {oc_pct_after:>9.1f}%"
        )

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill rulings through LLM enrichment extraction."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--county", help="Process a single county.")
    group.add_argument(
        "--all", action="store_true", dest="all_counties", help="Process all counties."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to DB.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich even if fields are already populated.",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50, help="Rulings per DB batch (default: 50)."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Parallel LLM call threads (default: 8).",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum total rulings to process."
    )
    parser.add_argument(
        "--provider",
        default="google",
        help="LLM provider: google or anthropic (default: google).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model override (defaults to provider default).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable is required.", file=sys.stderr)
        sys.exit(1)

    # Create LLM client for connection reuse
    client = create_llm_client(provider=args.provider)
    if client is None:
        print(
            "ERROR: Could not create LLM client. Check API key environment variables.",
            file=sys.stderr,
        )
        sys.exit(1)

    stats = BackfillStats(start_time=time.time())

    conn = psycopg.connect(db_url, autocommit=False)
    try:
        # Determine which counties to process
        if args.all_counties:
            counties = fetch_counties(conn)
        else:
            counties = [args.county]

        logger.info(
            "backfill.start",
            counties=counties,
            force=args.force,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            limit=args.limit,
            provider=args.provider,
            model=args.model,
        )

        for county in counties:
            process_county(
                conn,
                county,
                stats,
                provider=args.provider,
                model=args.model,
                client=client,
                force=args.force,
                dry_run=args.dry_run,
                batch_size=args.batch_size,
                concurrency=args.concurrency,
                limit=args.limit,
            )

        print_report(stats)

        totals = stats.totals()
        if totals["errors"] > 0:
            logger.warning(
                "backfill.completed_with_errors",
                errors=totals["errors"],
            )
            sys.exit(1)
        else:
            logger.info("backfill.completed_successfully")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
