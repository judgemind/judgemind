#!/usr/bin/env bash
# check-migration-files.sh — Validate migration file naming and sequencing.
#
# Checks:
# 1. All migration files follow the N_name.sql naming pattern.
# 2. Migration numbers are sequential with no gaps.
# 3. No duplicate migration numbers exist.
#
# This runs in CI to catch migration file issues before merge.
#
# Usage:
#   scripts/check-migration-files.sh            # uses default repo root
#   scripts/check-migration-files.sh [repo-dir] # scan a specific repo root
#
# Exit codes:
#   0 — All checks passed.
#   1 — Validation errors found.

set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MIGRATIONS_DIR="$REPO_ROOT/packages/api/migrations"

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "ERROR: Migrations directory not found at $MIGRATIONS_DIR"
    exit 1
fi

errors=0

# Collect migration files
migration_files=()
for f in "$MIGRATIONS_DIR"/*.sql; do
    [[ -e "$f" ]] || continue
    migration_files+=("$(basename "$f")")
done

if [[ ${#migration_files[@]} -eq 0 ]]; then
    echo "No migration files found."
    exit 0
fi

echo "Found ${#migration_files[@]} migration file(s)."

# Check 1: Naming pattern
numbers=()
for file in "${migration_files[@]}"; do
    if [[ ! "$file" =~ ^([0-9]+)_.+\.sql$ ]]; then
        echo "ERROR: Invalid migration filename: $file"
        echo "  Expected pattern: N_name.sql (e.g., 1_initial-schema.sql)"
        errors=$((errors + 1))
    else
        numbers+=("${BASH_REMATCH[1]}")
    fi
done

if [[ ${#numbers[@]} -eq 0 ]]; then
    echo "ERROR: No valid migration numbers found."
    exit 1
fi

# Sort numbers numerically
IFS=$'\n' sorted_numbers=($(sort -n <<<"${numbers[*]}")); unset IFS

# Check 2: No duplicate numbers
prev=""
for num in "${sorted_numbers[@]}"; do
    if [[ "$num" == "$prev" ]]; then
        echo "ERROR: Duplicate migration number: $num"
        errors=$((errors + 1))
    fi
    prev="$num"
done

# Check 3: Sequential numbering (starting from 1)
expected=1
for num in "${sorted_numbers[@]}"; do
    actual=$((10#$num))  # Force base-10 interpretation
    if [[ $actual -ne $expected ]]; then
        echo "ERROR: Migration number gap: expected $expected, found $actual"
        errors=$((errors + 1))
        expected=$((actual + 1))
    else
        expected=$((expected + 1))
    fi
done

if [[ $errors -gt 0 ]]; then
    echo ""
    echo "Migration file validation: $errors error(s) found."
    exit 1
fi

last_num="${sorted_numbers[${#sorted_numbers[@]}-1]}"
echo "Migration file validation: all checks passed."
echo "  Migrations 1 through $last_num are present and sequential."
exit 0
