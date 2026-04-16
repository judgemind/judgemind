#!/usr/bin/env python3
"""Backfill: normalize department names in existing rulings.

Applies county-specific normalization to all existing rulings.department
values to consolidate inconsistent variants (e.g. LA "X #14" -> "X",
Riverside "07" -> "7", SB "S-17" -> "S17").

Usage:
    scripts/ecs-run-task.sh scripts/backfill_normalize_departments.py -- --dry-run
    scripts/ecs-run-task.sh scripts/backfill_normalize_departments.py

See: https://github.com/judgemind/judgemind/issues/2141
"""

# venv: scraper-framework
# one-off: true
from __future__ import annotations

import argparse
import os
import re
import sys


# ---------------------------------------------------------------------------
# Inline normalization rules (ECS oneshot constraint: cannot import siblings)
# ---------------------------------------------------------------------------

_LA_COURTROOM_SUFFIX_RE = re.compile(r"\s*#\d+$")
_LA_LETTER_PLUS_DIGITS_RE = re.compile(r"^([A-Za-z])\d+$")
_LA_SSC_PREFIX_RE = re.compile(r"^SSC-(.+)$", re.IGNORECASE)
_LA_COURTHOUSE_ALIASES: dict[str, str] = {"SEP": "P"}
_SB_HYPHEN_RE = re.compile(r"^([A-Za-z]+)-(\d+)$")


def _normalize_la(dept: str) -> str:
    m = _LA_SSC_PREFIX_RE.match(dept)
    if m:
        dept = m.group(1)
    dept_upper = dept.upper()
    if dept_upper in _LA_COURTHOUSE_ALIASES:
        dept = _LA_COURTHOUSE_ALIASES[dept_upper]
    dept = _LA_COURTROOM_SUFFIX_RE.sub("", dept).strip()
    m = _LA_LETTER_PLUS_DIGITS_RE.match(dept)
    if m:
        dept = m.group(1)
    return dept


def _normalize_riverside(dept: str) -> str:
    if dept.isdigit():
        return str(int(dept))
    return dept


def _normalize_san_bernardino(dept: str) -> str:
    m = _SB_HYPHEN_RE.match(dept)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    return dept


def normalize_department(county: str, department: str | None) -> str | None:
    """Normalize a department name using county-specific rules."""
    if department is None:
        return None
    dept = department.strip()
    if not dept:
        return None
    county_lower = county.lower()
    if county_lower == "los angeles":
        dept = _normalize_la(dept)
    elif county_lower == "riverside":
        dept = _normalize_riverside(dept)
    elif county_lower == "san bernardino":
        dept = _normalize_san_bernardino(dept)
    return dept if dept else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize department names in rulings"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without updating the database",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is required", file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg
    except ImportError:
        print(
            "ERROR: psycopg is required. Install with: pip install psycopg[binary]",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = psycopg.connect(database_url, autocommit=False)

    # Fetch all distinct (county, department) pairs that have rulings.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ct.county, r.department
            FROM rulings r
            JOIN cases c ON r.case_id = c.id
            JOIN courts ct ON c.court_id = ct.id
            WHERE r.department IS NOT NULL
            ORDER BY ct.county, r.department
        """)
        rows = cur.fetchall()

    updates: list[tuple[str, str, str]] = []  # (county, old_dept, new_dept)
    for county, dept in rows:
        normalized = normalize_department(county, dept)
        if normalized is not None and normalized != dept:
            updates.append((county, dept, normalized))

    if not updates:
        print("No department names need normalization.")
        conn.close()
        return

    print(f"Found {len(updates)} department name(s) to normalize:")
    for county, old, new in updates:
        print(f"  {county}: {old!r} -> {new!r}")

    if args.dry_run:
        print("\nDry run — no changes made.")
        conn.close()
        return

    # Apply updates.
    total_updated = 0
    with conn.cursor() as cur:
        for county, old_dept, new_dept in updates:
            cur.execute(
                """
                UPDATE rulings r
                SET department = %s
                FROM cases c
                JOIN courts ct ON c.court_id = ct.id
                WHERE r.case_id = c.id
                  AND r.department = %s
                  AND ct.county = %s
                """,
                (new_dept, old_dept, county),
            )
            count = cur.rowcount
            total_updated += count
            print(f"  Updated {count} ruling(s): {county} {old_dept!r} -> {new_dept!r}")

    conn.commit()
    conn.close()
    print(f"\nDone. Updated {total_updated} ruling(s) total.")


if __name__ == "__main__":
    main()
