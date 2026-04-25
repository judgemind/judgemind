#!/usr/bin/env python3
# venv: scraper-framework
"""Run all judge data cleanup scripts in the correct order.

This orchestration script runs the full cleanup pipeline for judge name quality
issues (#1106). The scripts must be run in a specific order because later
scripts depend on the output of earlier ones:

1. cleanup_org_judges.py    — Remove org/party names stored as judges
2. cleanup_judge_names.py   — Fix garbage, honorifics, suffixes, exact duplicates
3. merge_honorific_judges.py — Merge "Hon. X" records with "X" records
4. backfill_judge_names.py  — Fix truncated names by re-extracting from rulings
5. cleanup_judge_names.py   — Second pass to catch new duplicates from backfill
6. merge_near_duplicate_judges.py — Merge near-duplicates (fuzzy matching)

Each step is run as a subprocess so that database connections are properly
closed between scripts.

Usage:
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
            scripts/run_judge_cleanup.py

    # Dry run (no DB writes):
    scripts/with-secret.sh \\
        -e DATABASE_URL=judgemind/dev/db/connection:.url \\
        -- packages/scraper-framework/.venv/bin/python3 \\
            scripts/run_judge_cleanup.py --dry-run

Parent issue: #1106
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPTS_DIR)
PYTHON = sys.executable

# Scripts to run in order.  Each tuple is (script_name, description).
PIPELINE = [
    ("cleanup_org_judges.py", "Remove organization/party names stored as judges"),
    (
        "cleanup_judge_names.py",
        "Fix garbage names, honorifics, suffixes, exact duplicates",
    ),
    ("merge_honorific_judges.py", "Merge 'Hon. X' records with 'X' records"),
    ("backfill_judge_names.py", "Fix truncated names by re-extracting from rulings"),
    ("cleanup_judge_names.py", "Second pass: catch new duplicates from backfill"),
    ("merge_near_duplicate_judges.py", "Merge near-duplicate judge records"),
]


def run_script(script_name: str, dry_run: bool) -> int:
    """Run a single cleanup script. Returns the exit code."""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    cmd = [PYTHON, script_path]
    if dry_run:
        cmd.append("--dry-run")

    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, env=os.environ, timeout=120)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run all judge data cleanup scripts in order.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to all scripts (no DB writes).",
    )
    parser.add_argument(
        "--start-from",
        type=int,
        default=1,
        help="Start from step N (1-indexed). Useful for resuming after a failure.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    logger.info("Starting judge data cleanup pipeline (%d steps)", len(PIPELINE))
    if args.dry_run:
        logger.info("[DRY RUN MODE] No database writes will be made.")

    for i, (script_name, description) in enumerate(PIPELINE, start=1):
        if i < args.start_from:
            logger.info(
                "Step %d/%d: SKIPPED (--start-from=%d)",
                i,
                len(PIPELINE),
                args.start_from,
            )
            continue

        logger.info("=" * 70)
        logger.info("Step %d/%d: %s", i, len(PIPELINE), description)
        logger.info("Script: %s", script_name)
        logger.info("=" * 70)

        exit_code = run_script(script_name, args.dry_run)
        if exit_code != 0:
            logger.error(
                "Step %d FAILED (exit code %d). Fix the issue and re-run with --start-from=%d",
                i,
                exit_code,
                i,
            )
            sys.exit(exit_code)

        logger.info("Step %d/%d completed successfully.", i, len(PIPELINE))

    logger.info("=" * 70)
    logger.info("All %d steps completed successfully!", len(PIPELINE))
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
