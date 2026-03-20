#!/usr/bin/env bash
# check-schema-drift.sh — Detect tables in migration files missing from schema.sql.
#
# Parses CREATE TABLE statements from both packages/api/migrations/*.sql and
# packages/api/src/data-access/schema.sql, then fails if any table created by a
# migration is not also present in schema.sql.
#
# This catches drift at PR time: every migration that creates a table must have
# a corresponding CREATE TABLE in schema.sql so that local dev (docker-compose)
# and schema validation tests stay in sync.
#
# Usage:
#   scripts/check-schema-drift.sh            # exits 0 if clean, 1 if drift
#   scripts/check-schema-drift.sh [repo-dir] # scan a specific repo root
#
# Exit codes:
#   0 — All migration tables are present in schema.sql.
#   1 — One or more migration tables are missing from schema.sql.

set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

SCHEMA_FILE="$REPO_ROOT/packages/api/src/data-access/schema.sql"
MIGRATIONS_DIR="$REPO_ROOT/packages/api/migrations"

if [[ ! -f "$SCHEMA_FILE" ]]; then
    echo "ERROR: schema.sql not found at $SCHEMA_FILE"
    exit 1
fi

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "ERROR: migrations directory not found at $MIGRATIONS_DIR"
    exit 1
fi

# extract_tables FILE
# Extracts table names from CREATE TABLE statements in a SQL file.
# Handles: CREATE TABLE name, CREATE TABLE IF NOT EXISTS name,
#          CREATE TABLE schema.name (e.g. staging.captures).
# Only considers lines before "-- Down Migration" to avoid counting
# tables that appear only in DROP statements.
# Returns empty string (not an error) if no CREATE TABLE found.
extract_tables() {
    local file="$1"
    # Read only the up-migration portion (before "-- Down Migration").
    # Convert to lowercase first so we can match case-insensitively.
    # Use "|| true" on grep to avoid pipefail exit when no matches.
    sed -n '/^-- Down Migration/q;p' "$file" \
        | tr '[:upper:]' '[:lower:]' \
        | { grep -E '^\s*create\s+table' || true; } \
        | sed -E 's/^[[:space:]]*create[[:space:]]+table[[:space:]]+//' \
        | sed -E 's/^if[[:space:]]+not[[:space:]]+exists[[:space:]]+//' \
        | sed -E 's/[[:space:]]*\(.*//' \
        | sed -E 's/[[:space:]]*$//'
}

# Collect tables from schema.sql
schema_tables=$(extract_tables "$SCHEMA_FILE" | sort -u)

# Collect tables from all migration files
migration_tables=""
for migration_file in "$MIGRATIONS_DIR"/*.sql; do
    tables=$(extract_tables "$migration_file")
    if [[ -n "$tables" ]]; then
        if [[ -n "$migration_tables" ]]; then
            migration_tables="$migration_tables
$tables"
        else
            migration_tables="$tables"
        fi
    fi
done

if [[ -z "$migration_tables" ]]; then
    echo "All clean — no CREATE TABLE statements found in migrations."
    exit 0
fi

migration_tables=$(echo "$migration_tables" | sort -u)

# Compare: find migration tables not in schema.sql
missing=()
while IFS= read -r table; do
    [[ -z "$table" ]] && continue
    if ! echo "$schema_tables" | grep -qxF "$table"; then
        missing+=("$table")
    fi
done <<< "$migration_tables"

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Schema drift detected — migration tables missing from schema.sql."
    echo ""
    echo "  The following tables are created in migration files but not in"
    echo "  packages/api/src/data-access/schema.sql:"
    echo ""
    for table in "${missing[@]}"; do
        # Find which migration file defines this table
        source_file=""
        for migration_file in "$MIGRATIONS_DIR"/*.sql; do
            if extract_tables "$migration_file" | grep -qxF "$table"; then
                source_file=$(basename "$migration_file")
                break
            fi
        done
        echo "    - $table  (from migrations/$source_file)"
    done
    echo ""
    echo "  Fix: Add the missing CREATE TABLE statement(s) to schema.sql."
    echo "  schema.sql must contain every table so that docker-compose local dev"
    echo "  and schema validation tests stay in sync with production."
    exit 1
fi

echo "All clean — every migration table is present in schema.sql."
exit 0
