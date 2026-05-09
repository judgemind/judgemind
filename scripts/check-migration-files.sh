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
fix_blocks=()

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
        # Fix block — suggest a rename. We can't infer the right N without
        # seeing the rest of the directory yet (we may not have parsed all
        # files), so anchor on the existing topic suffix.
        fix_blocks+=(
            ""
            "Fix for naming-pattern violation '$file':"
            "  Rename to a properly-numbered file. The expected shape is"
            "  <N>_<topic-with-hyphens>.sql where <N> is the next available"
            "  sequence number. Run:"
            "    cd packages/api/migrations"
            "    LATEST=\$(ls *.sql | sed 's/_.*//' | sort -n | tail -1)"
            "    NEXT=\$((LATEST + 1))"
            "    git mv '$file' \"\${NEXT}_<topic>.sql\""
            "  Then update any references in CI fixtures or seed scripts."
        )
    else
        numbers+=("${BASH_REMATCH[1]}")
    fi
done

if [[ ${#numbers[@]} -eq 0 ]]; then
    echo "ERROR: No valid migration numbers found."
    # Emit any naming-pattern Fix blocks accumulated above so the operator
    # gets the rename recipe even when no valid numbers exist (#4346).
    if [[ ${#fix_blocks[@]} -gt 0 ]]; then
        for line in "${fix_blocks[@]}"; do
            echo "$line"
        done
    fi
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
        # Fix block — list the colliding files and suggest renaming the
        # newer one to the next free slot.
        colliding=()
        for f in "${migration_files[@]}"; do
            if [[ "$f" =~ ^${num}_ ]]; then
                colliding+=("$f")
            fi
        done
        last_num="${sorted_numbers[${#sorted_numbers[@]}-1]}"
        next_free=$((10#$last_num + 1))
        fix_blocks+=(
            ""
            "Fix for duplicate migration number $num:"
            "  Two files claim sequence number $num:"
        )
        # Length-guard: ``colliding`` was just populated above by
        # the inner ``for f in migration_files`` filter loop. In
        # practice the duplicate-num condition guarantees at least
        # one match (we got here because two files share the same
        # number), but the #4479 static check treats branch-
        # conditional ``+=`` as non-binding.
        if [ "${#colliding[@]}" -gt 0 ]; then
            for c in "${colliding[@]}"; do
                fix_blocks+=("    - $c")
            done
        fi
        fix_blocks+=(
            "  Keep the older one (whichever was merged to main first) at"
            "  '$num' and rename the newer to '${next_free}_<topic>.sql':"
            "    cd packages/api/migrations"
            "    git mv '<newer-file>' '${next_free}_<topic>.sql'"
            "  Then rebase against origin/main so the actual gap closes."
        )
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
        # Fix block — name the file at the gap-start so the operator can
        # rename without grepping.
        gap_file=""
        for f in "${migration_files[@]}"; do
            if [[ "$f" =~ ^0*${num}_ ]]; then
                gap_file="$f"
                break
            fi
        done
        if [[ -n "$gap_file" ]]; then
            fix_blocks+=(
                ""
                "Fix for migration number gap (expected $expected, found $actual):"
                "  The file '$gap_file' is numbered $actual but no migration"
                "  $expected exists. Renumber:"
                "    cd packages/api/migrations"
                "    git mv '$gap_file' \"${expected}_\${gap_file#*_}\""
                "  Or, if a migration $expected was deleted intentionally,"
                "  rename ALL files from $expected onward to close the gap."
            )
        else
            fix_blocks+=(
                ""
                "Fix for migration number gap (expected $expected, found $actual):"
                "  Renumber the next file to '${expected}_<topic>.sql' (or"
                "  rename ALL files from $expected onward to close the gap)."
            )
        fi
        expected=$((actual + 1))
    else
        expected=$((expected + 1))
    fi
done

if [[ $errors -gt 0 ]]; then
    # Emit the accumulated Fix blocks. Length-guard the iteration —
    # bash 3.2 trips on ``"${fix_blocks[@]}"`` when arr=() and the
    # ``+=`` only ran inside conditional branches that produced
    # error-counter increments without populating ``fix_blocks``
    # (defensive: in practice ``errors > 0`` implies fix_blocks is
    # populated, but the static check #4479 can't prove that).
    if [ "${#fix_blocks[@]}" -gt 0 ]; then
        for line in "${fix_blocks[@]}"; do
            echo "$line"
        done
    fi
    echo ""
    echo "Migration file validation: $errors error(s) found."
    exit 1
fi

last_num="${sorted_numbers[${#sorted_numbers[@]}-1]}"
echo "Migration file validation: all checks passed."
echo "  Migrations 1 through $last_num are present and sequential."
exit 0
