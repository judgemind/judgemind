#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Audit (and optionally repair) document supersede integrity — #4813.

Two invariants on ``derived.documents``:

* **No superseded document holds a ruling.**  A document with a
  ``derived.rulings`` row is live; the API answers 410 for a superseded one
  (#4809).  ``insert_document_and_ruling`` revives a row that takes a ruling
  back, and the content-hash dedup path revives its winner (#4813).
* **No ``previous_version_id`` cycle.**  The dedup path links a loser to the
  winner.  Before #4813 it did not look at the winner, so a winner that
  pointed back at the loser formed a two-row cycle (Orange 5e2650f8 /
  8a8f866c).  Cycles of any length are reported.

Read-only by default: prints one ``SUPERSEDE_INTEGRITY`` JSON line with the
counts and sample ids, and exits 1 when either count is non-zero.

``--repair`` fixes both in one transaction and re-runs the audit:

1. every superseded document that holds a ruling becomes ``active`` with
   ``change_type`` and ``previous_version_id`` cleared (the #4809 revive);
2. any cycle still left has the ``previous_version_id`` of its smallest id
   cleared.

These are ``derived.*`` rows, rebuildable from S3; the repair only restores
the state the ingestion code now keeps by construction.  It does not touch
the search index: a revived document's search doc is written by the next
re-ingest or ``reindex_search_from_db.py --apply``.

Usage (ECS, against dev):
    scripts/ecs-run-task.sh scripts/audit_supersede_integrity.py
    scripts/ecs-run-task.sh scripts/audit_supersede_integrity.py -- --repair
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

SAMPLES = 20

SUPERSEDED_WITH_RULING_SQL = """
SELECT d.id::text
FROM documents d
WHERE d.status = 'superseded'
  AND EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)
ORDER BY d.id
"""

# Every document that lies on a previous_version_id cycle.  The walk starts
# at each row with a link and stops when it revisits a row; a row is on a
# cycle when the walk gets back to it.
CYCLE_MEMBERS_SQL = """
WITH RECURSIVE walk(start_id, cur_id, path) AS (
    SELECT d.id, d.previous_version_id, ARRAY[d.id]
    FROM documents d
    WHERE d.previous_version_id IS NOT NULL
  UNION ALL
    SELECT w.start_id, d.previous_version_id, w.path || d.id
    FROM walk w
    JOIN documents d ON d.id = w.cur_id
    WHERE d.previous_version_id IS NOT NULL
      AND NOT d.id = ANY(w.path)
)
SELECT DISTINCT start_id::text
FROM walk
WHERE cur_id = start_id
ORDER BY 1
"""

REVIVE_SQL = """
UPDATE documents d
SET status = 'active', change_type = NULL, previous_version_id = NULL
WHERE d.status = 'superseded'
  AND EXISTS (SELECT 1 FROM rulings r WHERE r.document_id = d.id)
"""

BREAK_LINK_SQL = "UPDATE documents SET previous_version_id = NULL WHERE id = %s::uuid"

CHAIN_SQL = "SELECT previous_version_id::text FROM documents WHERE id = %s::uuid"


def audit(conn: Any) -> dict[str, Any]:
    """Return counts and sample ids for both invariants."""
    with conn.cursor() as cur:
        cur.execute(SUPERSEDED_WITH_RULING_SQL)
        holding = [row[0] for row in cur.fetchall()]
        cur.execute(CYCLE_MEMBERS_SQL)
        cycle_members = [row[0] for row in cur.fetchall()]
    return {
        "superseded_with_ruling": len(holding),
        "cycle_members": len(cycle_members),
        "superseded_with_ruling_sample": holding[:SAMPLES],
        "cycle_members_sample": cycle_members[:SAMPLES],
        "_cycle_members": cycle_members,
    }


def cycles_of(conn: Any, members: list[str]) -> list[list[str]]:
    """Group cycle member ids into cycles by walking ``previous_version_id``."""
    remaining = set(members)
    cycles: list[list[str]] = []
    with conn.cursor() as cur:
        for start in sorted(members):
            if start not in remaining:
                continue
            cycle = [start]
            remaining.discard(start)
            cur.execute(CHAIN_SQL, (start,))
            nxt = cur.fetchone()[0]
            while nxt is not None and nxt != start and nxt in remaining:
                cycle.append(nxt)
                remaining.discard(nxt)
                cur.execute(CHAIN_SQL, (nxt,))
                nxt = cur.fetchone()[0]
            cycles.append(cycle)
    return cycles


def repair(conn: Any) -> dict[str, int]:
    """Revive superseded ruling holders, then break any cycle left."""
    with conn.cursor() as cur:
        cur.execute(REVIVE_SQL)
        revived = cur.rowcount
    left = audit(conn)["_cycle_members"]
    broken = 0
    with conn.cursor() as cur:
        for cycle in cycles_of(conn, left):
            cur.execute(BREAK_LINK_SQL, (min(cycle),))
            broken += 1
    return {"revived": revived, "cycles_broken": broken}


def report(result: dict[str, Any]) -> str:
    return "SUPERSEDE_INTEGRITY " + json.dumps(
        {k: v for k, v in result.items() if not k.startswith("_")}, sort_keys=True
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repair", action="store_true", help="fix what the audit finds"
    )
    args = parser.parse_args(argv)

    import psycopg

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn:
        before = audit(conn)
        print(report(before))
        if not args.repair:
            clean = (
                before["superseded_with_ruling"] == 0 and before["cycle_members"] == 0
            )
            return 0 if clean else 1
        fixed = repair(conn)
        conn.commit()
        print("SUPERSEDE_REPAIR " + json.dumps(fixed, sort_keys=True))
        after = audit(conn)
        print(report(after))
        return (
            0
            if after["superseded_with_ruling"] == 0 and after["cycle_members"] == 0
            else 1
        )


if __name__ == "__main__":
    sys.exit(main())
