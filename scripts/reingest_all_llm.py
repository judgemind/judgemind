#!/usr/bin/env python3
# venv: scraper-framework
"""Re-ingest all historical documents via LLM extraction with quality metrics.

Orchestrates a full LLM-based re-ingestion of all archived documents from S3,
with before/after data quality metrics and cost tracking.  This is the "one
command" script for issue #1474.

Runs the existing ``reingest_from_s3.py`` logic internally (shared code via
the scraper-framework package) with ``--force-llm --full-reparse
--report-metrics`` equivalent flags enabled.

Usage (local)::

    scripts/run-py.sh scripts/reingest_all_llm.py -- --dry-run

For ECS, use ``reingest_from_s3.py`` directly with the equivalent flags::

    scripts/ecs-run-task.sh scripts/reingest_from_s3.py -- \\
        --force-llm --full-reparse --report-metrics

This wrapper imports ``reingest_from_s3`` as a sibling module, which is
not available in the ECS oneshot environment (single-file upload).

Options:
    --dry-run           Parse and show what would be updated, but don't write to DB.
    --county NAME       Process only this county (default: all counties).
    --batch-size N      Number of documents per batch (default: 25).
    --limit N           Maximum total documents to re-ingest.
    --concurrency N     Number of parallel S3 fetch threads (default: 10).
    --parse-workers N   Number of parallel scraper parse threads (default: 4).
    --llm-timeout N     Per-call LLM API timeout in seconds (default: 60).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date

# Ensure the scraper-framework source is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "packages", "scraper-framework", "src"
    ),
)
# Also ensure scripts/ is importable (for reingest_from_s3)
sys.path.insert(
    0,
    os.path.dirname(__file__),
)

import structlog  # noqa: E402

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()

# Import after structlog is configured so the reingest module uses it.
import reingest_from_s3  # noqa: E402


def main() -> None:
    """Run full LLM-based re-ingestion with quality metrics."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-ingest all historical documents via LLM extraction. "
            "Runs quality metrics before and after, tracks cost."
        ),
    )
    parser.add_argument(
        "--county",
        type=str,
        default=None,
        help="Scope to this county (default: all counties).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse but don't update DB.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Batch size (default: 25).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to process.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of parallel S3 fetch threads (default: 10).",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=4,
        help="Number of parallel scraper parse threads (default: 4).",
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=60.0,
        help="Per-call LLM API timeout in seconds (default: 60).",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info(
        "reingest_all_llm.starting",
        county=args.county or "ALL",
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        limit=args.limit,
    )

    stats = reingest_from_s3.run_reingest(
        dsn,
        county=args.county,
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        parse_workers=args.parse_workers,
        no_llm=False,
        llm_timeout=args.llm_timeout,
        force_llm=True,
        full_reparse=True,
        report_metrics=True,
    )

    # Print final summary as JSON for machine parsing
    logger.info("reingest_all_llm.complete")
    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
