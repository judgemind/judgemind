#!/usr/bin/env python3
"""Detect and merge duplicate judge records using court directory roster names.

For each court, fetches the latest roster snapshot and fuzzy-matches all
judge records against it. Where multiple judges match the same roster entry,
merges them: reassigns rulings, case_judges, and judge_aliases to the
surviving record, then deletes the duplicate.

Usage (local):
  DATABASE_URL=postgres://judgemind:localdev@localhost:5432/judgemind \
  scripts/run-py.sh scripts/dedup_judges.py --dry-run

Usage (ECS):
  scripts/ecs-run-task.sh scripts/dedup_judges.py -- --dry-run
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import psycopg

from ingestion.db import (
    _normalize_for_comparison,
    _strip_middle_initials,
    fuzzy_match_roster_name,
    normalize_judge_name,
)


@dataclass
class JudgeRecord:
    """A judge record from the database."""

    id: str
    canonical_name: str
    court_id: str
    ruling_count: int = 0


@dataclass
class MergeAction:
    """A planned merge of duplicate judge records."""

    roster_name: str
    survivor: JudgeRecord
    duplicates: list[JudgeRecord]
    confidence: float


def get_courts(conn: psycopg.Connection) -> list[dict[str, str]]:
    """Get all courts with their UUIDs and court codes."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, court_code, county FROM courts ORDER BY county")
        return [
            {"id": str(row[0]), "court_code": row[1], "county": row[2]}
            for row in cur.fetchall()
        ]


def get_judges_for_court(conn: psycopg.Connection, court_id: str) -> list[JudgeRecord]:
    """Get all judges for a court with their ruling counts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.id, j.canonical_name, j.court_id,
                   COUNT(r.id) AS ruling_count
            FROM judges j
            LEFT JOIN rulings r ON r.judge_id = j.id
            WHERE j.court_id = %s::uuid
            GROUP BY j.id, j.canonical_name, j.court_id
            ORDER BY j.canonical_name
            """,
            (court_id,),
        )
        return [
            JudgeRecord(
                id=str(row[0]),
                canonical_name=row[1],
                court_id=str(row[2]),
                ruling_count=row[3],
            )
            for row in cur.fetchall()
        ]


def get_roster_names_for_court(conn: psycopg.Connection, court_code: str) -> list[str]:
    """Get unique judge names from the latest roster snapshot."""
    snapshot_court_id = court_code.replace("-", "_")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT mapping FROM court_directory_snapshots
            WHERE court_id = %s
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (snapshot_court_id,),
        )
        row = cur.fetchone()

    if row is None or row[0] is None:
        return []

    mapping_data = row[0]
    mapping: dict[str, str]
    if isinstance(mapping_data, dict):
        mapping = mapping_data
    else:
        mapping = json.loads(mapping_data)

    return list(set(mapping.values()))


# Minimum confidence for a roster match to be used for dedup merging.
# Matches below this threshold are ignored to prevent merging distinct judges.
_MIN_MERGE_CONFIDENCE = 0.85


def detect_duplicates(
    judges: list[JudgeRecord],
    roster_names: list[str],
) -> list[MergeAction]:
    """Detect duplicate judge records by matching against roster names.

    Groups judges that match the same roster entry (using the normalized
    roster name as the group key to handle different raw spellings of the
    same canonical name). For each group with more than one judge, creates
    a MergeAction with the best-matching judge as the survivor.

    Only considers matches above the confidence threshold to avoid
    false-positive merges of distinct judges with similar names.

    Also detects duplicates among judges that don't match any roster entry
    by comparing them pairwise using fuzzy matching.
    """
    # Match each judge against the roster, grouping by normalized name
    roster_groups: dict[str, list[tuple[JudgeRecord, float, str]]] = {}

    for judge in judges:
        match = fuzzy_match_roster_name(judge.canonical_name, roster_names)
        if match is not None:
            roster_name, confidence = match
            # Skip low-confidence matches to prevent false-positive merges
            if confidence < _MIN_MERGE_CONFIDENCE:
                continue
            # Group by normalized name to avoid UniqueViolation when
            # different raw roster names normalize to the same canonical
            normalized_key = normalize_judge_name(roster_name)
            if normalized_key is None:
                continue
            if normalized_key not in roster_groups:
                roster_groups[normalized_key] = []
            roster_groups[normalized_key].append((judge, confidence, roster_name))

    # Build merge actions for groups with multiple judges
    actions: list[MergeAction] = []
    for normalized_name, members_raw in roster_groups.items():
        if len(members_raw) <= 1:
            continue

        members = [(j, c) for j, c, _ in members_raw]

        # Pick the survivor: prefer highest confidence, then most rulings,
        # then the one whose canonical_name best matches the roster name.
        members.sort(
            key=lambda m: (m[1], m[0].ruling_count),
            reverse=True,
        )
        survivor_judge, best_confidence = members[0]
        duplicates = [j for j, _ in members[1:]]

        actions.append(
            MergeAction(
                roster_name=normalized_name,
                survivor=survivor_judge,
                duplicates=duplicates,
                confidence=best_confidence,
            )
        )

    # Also detect pairwise duplicates among judges that didn't match
    # the roster at high confidence
    def _is_unmatched(judge: JudgeRecord) -> bool:
        match = fuzzy_match_roster_name(judge.canonical_name, roster_names)
        return match is None or match[1] < _MIN_MERGE_CONFIDENCE

    unmatched = [j for j in judges if _is_unmatched(j)]
    _detect_pairwise_duplicates(unmatched, actions)

    return actions


def _detect_pairwise_duplicates(
    judges: list[JudgeRecord],
    actions: list[MergeAction],
) -> None:
    """Detect duplicates among judges not matched to any roster entry.

    Uses the same fuzzy comparison as fuzzy_match_roster_name but
    compares judges against each other instead of against the roster.
    Appends any found merge actions to the provided list.
    """
    if len(judges) < 2:
        return

    merged_ids: set[str] = set()

    for i, judge_a in enumerate(judges):
        if judge_a.id in merged_ids:
            continue
        group = [judge_a]
        for judge_b in judges[i + 1 :]:
            if judge_b.id in merged_ids:
                continue
            # Compare normalized forms
            norm_a = _normalize_for_comparison(judge_a.canonical_name)
            norm_b = _normalize_for_comparison(judge_b.canonical_name)
            stripped_a = _strip_middle_initials(judge_a.canonical_name).lower()
            stripped_b = _strip_middle_initials(judge_b.canonical_name).lower()

            if norm_a == norm_b or stripped_a == stripped_b:
                group.append(judge_b)
                merged_ids.add(judge_b.id)

        if len(group) > 1:
            # Survivor = most rulings, then longest name
            group.sort(
                key=lambda j: (j.ruling_count, len(j.canonical_name)),
                reverse=True,
            )
            actions.append(
                MergeAction(
                    roster_name=group[0].canonical_name,
                    survivor=group[0],
                    duplicates=group[1:],
                    confidence=0.9,
                )
            )


def merge_judges(
    conn: psycopg.Connection,
    action: MergeAction,
    *,
    dry_run: bool = True,
) -> dict[str, int]:
    """Merge duplicate judge records into the survivor.

    Reassigns rulings, case_judges, and judge_aliases from each duplicate
    to the survivor, then deletes the duplicate judge record.

    Returns a dict with counts of rows updated/deleted.
    """
    stats: dict[str, int] = {
        "rulings_reassigned": 0,
        "case_judges_reassigned": 0,
        "case_judges_deleted_dup": 0,
        "aliases_reassigned": 0,
        "judges_deleted": 0,
    }

    survivor_id = action.survivor.id

    # Optionally update survivor's canonical_name to match roster
    roster_normalized = normalize_judge_name(action.roster_name)

    for dup in action.duplicates:
        dup_id = dup.id

        if dry_run:
            # Just count what would be affected
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM rulings WHERE judge_id = %s::uuid",
                    (dup_id,),
                )
                stats["rulings_reassigned"] += cur.fetchone()[0]

                cur.execute(
                    "SELECT COUNT(*) FROM case_judges WHERE judge_id = %s::uuid",
                    (dup_id,),
                )
                stats["case_judges_reassigned"] += cur.fetchone()[0]

                cur.execute(
                    "SELECT COUNT(*) FROM judge_aliases WHERE judge_id = %s::uuid",
                    (dup_id,),
                )
                stats["aliases_reassigned"] += cur.fetchone()[0]

                stats["judges_deleted"] += 1
            continue

        with conn.cursor() as cur:
            # 1. Reassign rulings
            cur.execute(
                """
                UPDATE rulings SET judge_id = %s::uuid, updated_at = NOW()
                WHERE judge_id = %s::uuid
                """,
                (survivor_id, dup_id),
            )
            stats["rulings_reassigned"] += cur.rowcount

            # 2. Reassign case_judges (handle conflicts — the survivor
            #    may already be linked to the same case)
            # First, delete case_judges rows where the survivor is already
            # linked to the same case (to avoid unique constraint violation).
            cur.execute(
                """
                DELETE FROM case_judges
                WHERE judge_id = %s::uuid
                  AND case_id IN (
                    SELECT case_id FROM case_judges
                    WHERE judge_id = %s::uuid
                  )
                """,
                (dup_id, survivor_id),
            )
            stats["case_judges_deleted_dup"] += cur.rowcount

            # Then reassign the remaining case_judges
            cur.execute(
                """
                UPDATE case_judges SET judge_id = %s::uuid
                WHERE judge_id = %s::uuid
                """,
                (survivor_id, dup_id),
            )
            stats["case_judges_reassigned"] += cur.rowcount

            # 3. Reassign judge_aliases
            # Add the duplicate's canonical_name as an alias of the survivor
            cur.execute(
                """
                INSERT INTO judge_aliases
                    (judge_id, raw_name, source, confidence, is_verified)
                VALUES (%s::uuid, %s, 'dedup_merge', 0.95, FALSE)
                ON CONFLICT DO NOTHING
                """,
                (survivor_id, dup.canonical_name),
            )

            # Reassign existing aliases from the duplicate to the survivor
            cur.execute(
                """
                UPDATE judge_aliases SET judge_id = %s::uuid
                WHERE judge_id = %s::uuid
                """,
                (survivor_id, dup_id),
            )
            stats["aliases_reassigned"] += cur.rowcount

            # 4. Delete the duplicate judge record
            cur.execute(
                "DELETE FROM judges WHERE id = %s::uuid",
                (dup_id,),
            )
            stats["judges_deleted"] += cur.rowcount

    # Update survivor's canonical_name to match roster if it differs
    if (
        not dry_run
        and roster_normalized is not None
        and roster_normalized != action.survivor.canonical_name
    ):
        with conn.cursor() as cur:
            # Add old canonical name as an alias first
            cur.execute(
                """
                INSERT INTO judge_aliases
                    (judge_id, raw_name, source, confidence, is_verified)
                VALUES (%s::uuid, %s, 'dedup_rename', 1.0, FALSE)
                ON CONFLICT DO NOTHING
                """,
                (survivor_id, action.survivor.canonical_name),
            )
            cur.execute(
                """
                UPDATE judges SET canonical_name = %s, updated_at = NOW()
                WHERE id = %s::uuid
                """,
                (roster_normalized, survivor_id),
            )

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect and merge duplicate judge records"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be merged without making changes",
    )
    parser.add_argument(
        "--court",
        type=str,
        help="Process only the specified court (by county name)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)

    courts = get_courts(conn)
    if args.court:
        courts = [c for c in courts if c["county"].lower() == args.court.lower()]
        if not courts:
            print(f"ERROR: Court not found: {args.court}", file=sys.stderr)
            sys.exit(1)

    total_actions = 0
    total_stats: dict[str, int] = {
        "rulings_reassigned": 0,
        "case_judges_reassigned": 0,
        "case_judges_deleted_dup": 0,
        "aliases_reassigned": 0,
        "judges_deleted": 0,
    }
    all_results: list[dict] = []

    for court in courts:
        court_id = court["id"]
        court_code = court["court_code"]
        county = court["county"]

        judges = get_judges_for_court(conn, court_id)
        roster_names = get_roster_names_for_court(conn, court_code)

        if not judges:
            continue

        actions = detect_duplicates(judges, roster_names)

        if not actions:
            continue

        if not args.json:
            print(f"\n{'=' * 60}")
            print(f"Court: {county} ({court_code})")
            print(f"  Judges: {len(judges)}, Roster names: {len(roster_names)}")
            print(f"  Duplicate groups found: {len(actions)}")

        for action in actions:
            total_actions += 1

            if not args.json:
                print(f"\n  Roster name: {action.roster_name}")
                print(
                    f"  Survivor: {action.survivor.canonical_name} "
                    f"(id={action.survivor.id[:8]}..., "
                    f"rulings={action.survivor.ruling_count})"
                )
                for dup in action.duplicates:
                    print(
                        f"  Duplicate: {dup.canonical_name} "
                        f"(id={dup.id[:8]}..., rulings={dup.ruling_count})"
                    )

            stats = merge_judges(conn, action, dry_run=args.dry_run)

            for key, val in stats.items():
                total_stats[key] += val

            if not args.json:
                if args.dry_run:
                    print(f"  [DRY RUN] Would merge: {stats}")
                else:
                    print(f"  Merged: {stats}")

            all_results.append(
                {
                    "court": county,
                    "roster_name": action.roster_name,
                    "survivor": action.survivor.canonical_name,
                    "duplicates": [d.canonical_name for d in action.duplicates],
                    "stats": stats,
                }
            )

    if not args.dry_run:
        conn.commit()

    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "total_groups": total_actions,
                    "total_stats": total_stats,
                    "results": all_results,
                },
                indent=2,
            )
        )
    else:
        print(f"\n{'=' * 60}")
        print(f"Total duplicate groups: {total_actions}")
        print(f"Total stats: {total_stats}")
        if args.dry_run:
            print("\n[DRY RUN] No changes made. Remove --dry-run to apply.")

    conn.close()


if __name__ == "__main__":
    main()
