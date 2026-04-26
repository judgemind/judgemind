#!/usr/bin/env python3
"""Clean up 98 fused OC case_titles by rebuilding derived.* for Orange County.

The LLM extractor bug (fixed in PR #2515) caused multi-case PDFs in Orange
County to produce a single fused case_title like "Smith v. Jones v. Brown"
instead of two separate rulings.  Now that rebuild_db.py routes raw PDFs
through the post-fix splitter (_split_fused_case_info), a clean
--reset --county Orange rebuild re-derives correctly-split rulings.

Dry-run by default; pass --apply to execute the rebuild.

Usage (via ECS):
    scripts/ecs-run-task.sh --cpu 2048 --memory 8192 \\
        scripts/cleanup_oc_fused_titles_2522.py -- --apply
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import psycopg
import structlog

from framework.logging import configure_structlog

configure_structlog()
logger = structlog.get_logger()

# Acceptance-criterion SQL (AC#2):
# returns the count of OC rulings whose case_title contains a double-v pattern
# indicating a fused multi-case title.
_FUSED_TITLE_SQL = r"""
SELECT COUNT(*) FROM derived.documents d
JOIN derived.courts ct ON ct.id = d.court_id
JOIN derived.rulings r ON r.document_id = d.id
LEFT JOIN derived.cases c ON c.id = r.case_id
WHERE ct.county ILIKE '%orange%'
AND c.case_title ~* '\sv[s]?\.?\s.+\sv[s]?\.?\s'
"""

# Spot-check content_hash prefixes from the issue body (documented fused docs).
_SPOT_CHECK_PREFIXES = ("1578e425", "37ef41f8", "bec66550")

_SPOT_CHECK_SQL = """
SELECT COUNT(DISTINCT r.case_id) FROM derived.documents d
JOIN derived.rulings r ON r.document_id = d.id
WHERE d.content_hash LIKE %s
"""


def _count_fused(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(_FUSED_TITLE_SQL)
        return cur.fetchone()[0]


def _spot_check(conn: psycopg.Connection) -> None:
    for prefix in _SPOT_CHECK_PREFIXES:
        with conn.cursor() as cur:
            cur.execute(_SPOT_CHECK_SQL, (prefix + "%",))
            distinct_cases = cur.fetchone()[0]
        logger.info(
            "spot_check",
            content_hash_prefix=prefix,
            distinct_case_ids=distinct_cases,
        )
        assert distinct_cases >= 2, (
            f"Expected >= 2 distinct case_ids for hash prefix {prefix!r}, "
            f"got {distinct_cases}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the rebuild (default: dry-run only).",
    )
    parser.add_argument(
        "--rebuild-script",
        default="scripts/rebuild_db.py",
        help="Path to rebuild_db.py (injectable for testing).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(2)

    with psycopg.connect(db_url) as conn:
        # Step 1: count fused OC titles
        baseline = _count_fused(conn)
        logger.info("fused_title_baseline", count=baseline)

        # Step 2: already clean?
        if baseline == 0:
            logger.info(
                "already_clean", message="No fused OC titles found — nothing to do."
            )
            return 0

        # Step 3: dry-run
        rebuild_cmd = [
            sys.executable,
            args.rebuild_script,
            "--county",
            "Orange",
            "--reset",
        ]
        if not args.apply:
            logger.info(
                "dry_run",
                fused_count=baseline,
                would_run=" ".join(rebuild_cmd),
            )
            return 0

        # Step 4: execute rebuild
        logger.info("running_rebuild", cmd=rebuild_cmd)
        subprocess.run(rebuild_cmd, check=True)

        # Step 5: re-count and verify
        post_count = _count_fused(conn)
        logger.info("post_rebuild_count", count=post_count)
        if post_count != 0:
            raise RuntimeError(
                f"Rebuild completed but {post_count} fused OC titles remain. "
                "Manual investigation required."
            )

        # Step 6: spot-check three known content_hash prefixes
        _spot_check(conn)

    logger.info("cleanup_complete", status="ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
