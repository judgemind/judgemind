#!/usr/bin/env python3
"""Check for unapplied database migrations.

Compares migration files in the repo against the pgmigrations tracking table
in the database. Reports any migrations that exist as files but have not been
applied to the database.

This script is designed to run:
1. Via ECS (scripts/ecs-run-task.sh) for dev DB checks
2. In CI with a test database (after running migrations)
3. Locally against a docker-compose database

Exit codes:
  0 — All migrations applied.
  1 — Unapplied migrations found.
  2 — Error (e.g., cannot connect to database).
"""
# venv: scraper-framework
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def get_migration_files(migrations_dir: str) -> list[str]:
    """Return sorted list of migration names from the migrations directory.

    Migration files follow the pattern: N_name.sql (e.g., 1_initial-schema.sql).
    Returns the name portion without the .sql extension, matching what
    node-pg-migrate stores in the pgmigrations table.
    """
    migration_dir = Path(migrations_dir)
    if not migration_dir.is_dir():
        print(f"ERROR: Migrations directory not found: {migrations_dir}", file=sys.stderr)
        sys.exit(2)

    files = []
    for f in sorted(migration_dir.glob("*.sql")):
        # node-pg-migrate stores the filename without .sql extension
        name = f.stem
        # Validate it matches the expected pattern (number prefix)
        if re.match(r"^\d+", name):
            files.append(name)
    return files


def get_applied_migrations(database_url: str) -> list[str]:
    """Query the pgmigrations table for applied migration names."""
    try:
        import psycopg2  # noqa: I001
    except ImportError:
        try:
            import psycopg  # noqa: I001

            conn = psycopg.connect(database_url)
            cur = conn.cursor()
            # Check if pgmigrations table exists
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'pgmigrations')"
            )
            row = cur.fetchone()
            if not row or not row[0]:
                print("pgmigrations table does not exist — no migrations have been applied.")
                conn.close()
                return []

            cur.execute("SELECT name FROM pgmigrations ORDER BY run_on")
            names = [row[0] for row in cur.fetchall()]
            conn.close()
            return names
        except ImportError:
            print("ERROR: Neither psycopg2 nor psycopg is installed.", file=sys.stderr)
            sys.exit(2)

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    # Check if pgmigrations table exists
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'pgmigrations')"
    )
    row = cur.fetchone()
    if not row or not row[0]:
        print("pgmigrations table does not exist — no migrations have been applied.")
        conn.close()
        return []

    cur.execute("SELECT name FROM pgmigrations ORDER BY run_on")
    names = [row[0] for row in cur.fetchall()]
    conn.close()
    return names


def main() -> None:
    """Compare migration files against applied migrations in the database."""
    import argparse

    parser = argparse.ArgumentParser(description="Check for unapplied database migrations")
    parser.add_argument(
        "--migrations-dir",
        default=None,
        help="Path to migrations directory (default: auto-detect from repo root)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Database connection URL (default: DATABASE_URL env var)",
    )
    args = parser.parse_args()

    # Resolve migrations directory
    migrations_dir = args.migrations_dir
    if not migrations_dir:
        # Auto-detect: look relative to this script's location
        script_dir = Path(__file__).resolve().parent
        repo_root = script_dir.parent
        migrations_dir = str(repo_root / "packages" / "api" / "migrations")

    # Resolve database URL
    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: No database URL provided. Set DATABASE_URL or use --database-url.", file=sys.stderr)
        sys.exit(2)

    # Get file-based migrations
    file_migrations = get_migration_files(migrations_dir)
    print(f"Migration files found: {len(file_migrations)}")
    for m in file_migrations:
        print(f"  {m}")

    # Get applied migrations from DB
    applied_migrations = get_applied_migrations(database_url)
    print(f"\nApplied migrations: {len(applied_migrations)}")
    for m in applied_migrations:
        print(f"  {m}")

    # Find unapplied migrations
    applied_set = set(applied_migrations)
    unapplied = [m for m in file_migrations if m not in applied_set]

    if unapplied:
        print(f"\nERROR: {len(unapplied)} unapplied migration(s) found:")
        for m in unapplied:
            print(f"  - {m}")
        print("\nRun 'npm run db:migrate' or deploy to apply pending migrations.")
        sys.exit(1)
    else:
        print("\nAll migrations applied. No drift detected.")
        sys.exit(0)


if __name__ == "__main__":
    main()
