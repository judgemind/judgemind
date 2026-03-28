#!/usr/bin/env python3
"""Apply missing UNIQUE constraints to case_parties and case_attorneys tables.

Fixes #1318: schema.sql declares UNIQUE constraints that are missing from dev DB.
Migrations 7 and 8 defined these constraints but were never applied.

This script:
1. Checks which constraints are missing
2. Deduplicates rows (keeping the oldest by UUID sort order)
3. Adds the UNIQUE constraints

Safe to run multiple times — skips constraints that already exist.
"""
# venv: scraper-framework
from __future__ import annotations

import os
import sys

import psycopg


def get_connection() -> psycopg.Connection:
    """Connect to the database using DATABASE_URL."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set")
        sys.exit(1)
    return psycopg.connect(database_url, autocommit=False)


def constraint_exists(
    cur: psycopg.Cursor,
    table: str,
    constraint_name: str,
) -> bool:
    """Check if a named constraint exists on a table."""
    cur.execute(
        "SELECT 1 FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND conname = %s",
        (table, constraint_name),
    )
    return cur.fetchone() is not None


def dedup_and_add_constraint(
    cur: psycopg.Cursor,
    table: str,
    constraint_name: str,
    columns: list[str],
    dry_run: bool,
) -> None:
    """Remove duplicates and add a UNIQUE constraint if missing."""
    if constraint_exists(cur, table, constraint_name):
        print(f"  SKIP: {constraint_name} already exists on {table}")
        return

    cols_str = ", ".join(columns)

    # Count duplicates before dedup
    cur.execute(
        f"SELECT COUNT(*) FROM {table} "  # noqa: S608
        f"WHERE id NOT IN ("
        f"  SELECT DISTINCT ON ({cols_str}) id "
        f"  FROM {table} "
        f"  ORDER BY {cols_str}, id"
        f")"
    )
    row = cur.fetchone()
    dup_count = row[0] if row else 0
    print(f"  Found {dup_count} duplicate rows in {table}")

    if dry_run:
        print(f"  DRY RUN: Would delete {dup_count} duplicates and add {constraint_name}")
        return

    if dup_count > 0:
        cur.execute(
            f"DELETE FROM {table} "  # noqa: S608
            f"WHERE id NOT IN ("
            f"  SELECT DISTINCT ON ({cols_str}) id "
            f"  FROM {table} "
            f"  ORDER BY {cols_str}, id"
            f")"
        )
        print(f"  Deleted {dup_count} duplicate rows")

    cur.execute(
        f"ALTER TABLE {table} "
        f"ADD CONSTRAINT {constraint_name} UNIQUE ({cols_str})"
    )
    print(f"  Added constraint {constraint_name}")


def main() -> None:
    """Apply missing UNIQUE constraints."""
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN MODE ===\n")

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            print("Checking case_parties...")
            dedup_and_add_constraint(
                cur,
                "case_parties",
                "uq_case_parties_case_party_role",
                ["case_id", "party_id", "role"],
                dry_run,
            )

            print("\nChecking case_attorneys...")
            dedup_and_add_constraint(
                cur,
                "case_attorneys",
                "uq_case_attorneys_case_attorney_role",
                ["case_id", "attorney_id", "role"],
                dry_run,
            )

        if not dry_run:
            conn.commit()
            print("\nAll constraints applied successfully.")
        else:
            conn.rollback()
            print("\nDry run complete — no changes made.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
