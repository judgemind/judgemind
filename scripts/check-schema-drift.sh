#!/usr/bin/env bash
# check-schema-drift.sh — Detect drift between schema.sql and migration files.
#
# Two checks:
# 1. TABLE DRIFT: Every CREATE TABLE in a migration must also exist in schema.sql.
# 2. CONSTRAINT DRIFT: Every multi-column UNIQUE constraint in schema.sql must
#    have a corresponding definition in a migration file (either inline in a
#    CREATE TABLE or via ALTER TABLE ADD CONSTRAINT).
#
# This catches drift at PR time so that local dev (docker-compose) and
# schema validation tests stay in sync with production.
#
# Usage:
#   scripts/check-schema-drift.sh            # exits 0 if clean, 1 if drift
#   scripts/check-schema-drift.sh [repo-dir] # scan a specific repo root
#
# Exit codes:
#   0 — No drift detected.
#   1 — Drift detected (missing tables or constraints).

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

echo "Table check: all clean — every migration table is present in schema.sql."

# =========================================================================
# CHECK 2: Constraint drift — UNIQUE constraints in schema.sql must be
# covered by a migration.
# =========================================================================

# extract_unique_constraints FILE
# Extracts multi-column UNIQUE constraints from a SQL file.
# Matches both inline "UNIQUE (col1, col2)" and named
# "CONSTRAINT name UNIQUE (col1, col2)" and
# "ADD CONSTRAINT name UNIQUE (col1, col2)".
# Only considers lines before "-- Down Migration" for migration files.
# Output format: one "table:col1,col2,..." per line.
#
# For CREATE TABLE blocks, we track which table we are inside of so we can
# attribute inline UNIQUE constraints to the correct table.
extract_unique_constraints() {
    local file="$1"
    # Preprocess: read the up-migration portion, lowercase, and join
    # continuation lines so multi-line statements become single lines.
    # A continuation line starts with whitespace (not a SQL keyword).
    local content
    content=$(sed -n '/^-- Down Migration/q;p' "$file" | tr '[:upper:]' '[:lower:]')

    local current_table=""
    local joined=""

    # Join continuation lines: if a line starts with whitespace and does not
    # start with a SQL keyword (create, alter, --, insert, delete, update, drop)
    # append it to the previous logical line.
    local lines=""
    while IFS= read -r line; do
        if [[ -z "$line" ]] || echo "$line" | grep -qE '^[[:space:]]*--'; then
            # Blank line or comment — flush and skip
            if [[ -n "$joined" ]]; then
                lines="${lines}${joined}"$'\n'
                joined=""
            fi
            continue
        fi
        if echo "$line" | grep -qE '^[[:space:]]+(create|alter|insert|delete|update|drop)[[:space:]]' \
           || echo "$line" | grep -qE '^(create|alter|insert|delete|update|drop)[[:space:]]'; then
            # New statement — flush previous
            if [[ -n "$joined" ]]; then
                lines="${lines}${joined}"$'\n'
            fi
            joined="$line"
        else
            # Continuation — append
            joined="${joined} ${line}"
        fi
    done <<< "$content"
    if [[ -n "$joined" ]]; then
        lines="${lines}${joined}"$'\n'
    fi

    # Now parse the joined lines
    while IFS= read -r lower; do
        [[ -z "$lower" ]] && continue

        # Track which CREATE TABLE block we are in
        if echo "$lower" | grep -qE '(^|[[:space:]])create[[:space:]]+table'; then
            current_table=$(echo "$lower" \
                | sed -E 's/.*(^|[[:space:]])create[[:space:]]+table[[:space:]]+//' \
                | sed -E 's/^if[[:space:]]+not[[:space:]]+exists[[:space:]]+//' \
                | sed -E 's/[[:space:]]*\(.*//' \
                | sed -E 's/[[:space:]]*$//')
        fi

        # Match ALTER TABLE ... ADD CONSTRAINT ... UNIQUE (...)
        # After joining, this is always on one logical line.
        if echo "$lower" | grep -qE 'alter[[:space:]]+table.*add[[:space:]]+constraint.*unique[[:space:]]*\('; then
            local tbl cols
            tbl=$(echo "$lower" \
                | sed -E 's/.*alter[[:space:]]+table[[:space:]]+//' \
                | sed -E 's/[[:space:]]+add[[:space:]]+.*//')
            cols=$(echo "$lower" \
                | sed -E 's/.*unique[[:space:]]*\(//' \
                | sed -E 's/\).*//' \
                | sed -E 's/[[:space:]]//g')
            if [[ -n "$tbl" && -n "$cols" ]]; then
                echo "${tbl}:${cols}"
            fi
            continue
        fi

        # Match inline UNIQUE (col1, col2) — must be multi-column
        # Skip single-column UNIQUE (column-level constraint like "email TEXT UNIQUE")
        if echo "$lower" | grep -qE '(^|[[:space:]])(constraint[[:space:]]+[^[:space:]]+[[:space:]]+)?unique[[:space:]]*\('; then
            local cols
            cols=$(echo "$lower" \
                | sed -E 's/.*unique[[:space:]]*\(//' \
                | sed -E 's/\).*//' \
                | sed -E 's/[[:space:]]//g')
            # Only report multi-column constraints (contain a comma)
            if [[ -n "$current_table" && -n "$cols" && "$cols" == *","* ]]; then
                echo "${current_table}:${cols}"
            fi
            continue
        fi

    done <<< "$lines"
}

# Collect constraints from schema.sql
schema_constraints=$(extract_unique_constraints "$SCHEMA_FILE" | sort -u)

# Collect constraints from all migration files
migration_constraints=""
for migration_file in "$MIGRATIONS_DIR"/*.sql; do
    constraints=$(extract_unique_constraints "$migration_file")
    if [[ -n "$constraints" ]]; then
        if [[ -n "$migration_constraints" ]]; then
            migration_constraints="$migration_constraints
$constraints"
        else
            migration_constraints="$constraints"
        fi
    fi
done

if [[ -z "$schema_constraints" ]]; then
    echo "Constraint check: all clean — no multi-column UNIQUE constraints in schema.sql."
    exit 0
fi

migration_constraints=$(echo "$migration_constraints" | sort -u)

# Compare: find schema constraints not covered by any migration
missing_constraints=()
while IFS= read -r constraint; do
    [[ -z "$constraint" ]] && continue
    if [[ -z "$migration_constraints" ]] || ! echo "$migration_constraints" | grep -qxF "$constraint"; then
        missing_constraints+=("$constraint")
    fi
done <<< "$schema_constraints"

if [[ ${#missing_constraints[@]} -gt 0 ]]; then
    echo ""
    echo "ERROR: Constraint drift detected — UNIQUE constraints in schema.sql"
    echo "       have no matching definition in any migration file."
    echo ""
    for entry in "${missing_constraints[@]}"; do
        local_table="${entry%%:*}"
        local_cols="${entry#*:}"
        echo "    - $local_table: UNIQUE ($local_cols)"
    done
    echo ""
    echo "  Fix: Add a migration with ALTER TABLE ... ADD CONSTRAINT ... UNIQUE"
    echo "  for each missing constraint. Every constraint in schema.sql must be"
    echo "  reproduced in a migration so it is applied to production."
    exit 1
fi

echo "Constraint check: all clean — every schema.sql UNIQUE constraint is in migrations."
exit 0
