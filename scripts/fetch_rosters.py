#!/usr/bin/env python3
# venv: scraper-framework
"""Fetch all court directory snapshots and archive to S3 + DB.

Runs every CourtDirectory subclass, archives raw HTML to S3, and stores
parsed dept→judge mappings in court_directory_snapshots. Idempotent —
content-hash dedup skips unchanged directories.

Usage (local):
  DATABASE_URL=postgres://judgemind:localdev@localhost:5432/judgemind \
  JUDGEMIND_ARCHIVE_BUCKET=judgemind-document-archive-dev \
  scripts/run-py.sh scripts/fetch_rosters.py

Usage (ECS):
  scripts/ecs-run-task.sh scripts/fetch_rosters.py
"""

from __future__ import annotations

import os
import sys
import time

import psycopg
import structlog

from framework.s3_cache import make_s3_client

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger(__name__)

# Registry of all CourtDirectory subclasses.
# Each entry: (module_path, class_name, court_id)
DIRECTORIES = [
    ("courts.ca.oc_dept_judges", "OCCourtDirectory", "ca_orange"),
    ("courts.ca.la_dept_judges", "LACourtDirectory", "ca_los_angeles"),
    ("courts.ca.fresno_dept_judges", "FresnoCourtDirectory", "ca_fresno"),
    ("courts.ca.kern_dept_judges", "KernCourtDirectory", "ca_kern"),
    ("courts.ca.sd_dept_judges", "SanDiegoCourtDirectory", "ca_san_diego"),
    ("courts.ca.sb_dept_judges", "SanBernardinoCourtDirectory", "ca_san_bernardino"),
    ("courts.ca.riverside_dept_judges", "RiversideCourtDirectory", "ca_riverside"),
    ("courts.ca.ventura_dept_judges", "VenturaCourtDirectory", "ca_ventura"),
    ("courts.ca.sf_dept_judges", "SFCourtDirectory", "ca_san_francisco"),
    ("courts.ca.sc_tentatives", "SantaClaraCourtDirectory", "ca_santa_clara"),
]

# Polite delay between fetches (seconds)
REQUEST_DELAY = 2.0


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    bucket = os.environ.get(
        "JUDGEMIND_ARCHIVE_BUCKET", "judgemind-document-archive-dev"
    )
    s3 = make_s3_client()
    conn = psycopg.connect(database_url, autocommit=False)

    succeeded = 0
    failed = 0
    unchanged = 0

    for module_path, class_name, court_id in DIRECTORIES:
        logger.info("Fetching directory", court_id=court_id, class_name=class_name)
        try:
            import importlib

            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            directory = cls(s3_client=s3, s3_bucket=bucket, db_conn=conn)
            mapping = directory.fetch_and_snapshot(court_id)
            if mapping:
                logger.info(
                    "Directory fetched",
                    court_id=court_id,
                    departments=len(mapping),
                    sample=dict(list(mapping.items())[:3]),
                )
                succeeded += 1
            else:
                logger.warning("Empty mapping returned", court_id=court_id)
                unchanged += 1
        except Exception as exc:
            failed += 1
            logger.error("Failed to fetch directory", court_id=court_id, error=str(exc))

        time.sleep(REQUEST_DELAY)

    logger.info(
        "Roster fetch complete",
        succeeded=succeeded,
        unchanged=unchanged,
        failed=failed,
        total=len(DIRECTORIES),
    )

    conn.close()


if __name__ == "__main__":
    main()
