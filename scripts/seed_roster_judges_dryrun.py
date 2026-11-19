#!/usr/bin/env python3
# venv: scraper-framework
# one-off: true
"""Per-court dry run of the roster judge seed, with a near-duplicate audit (#5161).

Runs ``ingestion.judge_seed.seed_judges_from_directory_snapshots`` once per
snapshot court on one connection, lists every judge each court would gain,
and checks each new name against the court's other judges: a near-duplicate
canonical name (``find_near_duplicate_judge_name``, the seed's own
comparison) or a case-insensitive alias held by another judge.  Then rolls
everything back, unless ``--commit`` is given.

Exit status is 1 when any would-be insert is a near-duplicate, so a dirty
result cannot pass as a clean one.

Usage (ECS; the dev DB is VPC-only):
    scripts/ecs-run-task.sh scripts/seed_roster_judges_dryrun.py
    scripts/ecs-run-task.sh scripts/seed_roster_judges_dryrun.py -- --commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from ingestion.db import find_near_duplicate_judge_name
from ingestion.judge_seed import seed_judges_from_directory_snapshots


def _snapshot_court_ids(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT court_id FROM court_directory_snapshots ORDER BY court_id"
        )
        return [str(row[0]) for row in cur.fetchall()]


def _court_judges(conn: psycopg.Connection, snapshot_court_id: str) -> dict[str, str]:
    """Return ``{judge_id: canonical_name}`` for the court, or ``{}``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.canonical_name FROM judges j JOIN courts c ON c.id = j.court_id "
            "WHERE c.court_code = %s",
            (snapshot_court_id.replace("_", "-"),),
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def _alias_holders(
    conn: psycopg.Connection, judge_ids: list[str], name: str
) -> list[str]:
    """Return the ids of *judge_ids* holding *name* as an alias (any case)."""
    if not judge_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT judge_id FROM judge_aliases "
            "WHERE judge_id = ANY(%s::uuid[]) AND lower(raw_name) = lower(%s)",
            (judge_ids, name),
        )
        return [str(row[0]) for row in cur.fetchall()]


def run(dsn: str, *, commit: bool) -> int:
    report: dict[str, dict[str, object]] = {}
    problems = 0
    with psycopg.connect(dsn, autocommit=False) as conn:
        for court in _snapshot_court_ids(conn):
            before = _court_judges(conn, court)
            stats = seed_judges_from_directory_snapshots(conn, only_court_id=court)
            after = _court_judges(conn, court)
            new_ids = sorted(set(after) - set(before), key=lambda i: after[i])
            near_duplicates = []
            for judge_id in new_ids:
                name = after[judge_id]
                others = {i: n for i, n in after.items() if i != judge_id}
                held = find_near_duplicate_judge_name(name, others.values())
                aliased = _alias_holders(conn, list(others), name)
                if held is not None or aliased:
                    near_duplicates.append(
                        {"name": name, "held_by": held, "alias_of": aliased}
                    )
            problems += len(near_duplicates)
            report[court] = {
                "stats": stats,
                "would_insert": [after[i] for i in new_ids],
                "near_duplicates": near_duplicates,
            }
        if commit:
            conn.commit()
        else:
            conn.rollback()

    print(
        json.dumps(
            {"mode": "commit" if commit else "dry-run (rolled back)", "courts": report}
        )
    )
    total = sum(int(r["stats"]["inserted"]) for r in report.values())  # type: ignore[index]
    print(
        f"SUMMARY total_inserted={total} near_duplicates={problems} committed={commit}"
    )
    for court, entry in report.items():
        stats = entry["stats"]
        print(
            f"COURT {court} inserted={stats['inserted']} "  # type: ignore[index]
            f"skipped_existing={stats['skipped_existing']} "  # type: ignore[index]
            f"skipped_no_court={stats['skipped_no_court']}"  # type: ignore[index]
        )
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--commit", action="store_true", help="Commit instead of rolling back."
    )
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(dsn, commit=args.commit))


if __name__ == "__main__":
    main()
