#!/usr/bin/env bash
# test_check_schema_drift.sh — Tests for check-schema-drift.sh
#
# Creates temporary directory structures to verify that the checker correctly
# detects migration tables missing from schema.sql.
#
# Usage:
#   scripts/tests/test_check_schema_drift.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-schema-drift.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

# Set up the directory structure the script expects
setup_repo() {
    rm -rf "$TMPDIR_TEST"/*
    mkdir -p "$TMPDIR_TEST/packages/api/src/data-access"
    mkdir -p "$TMPDIR_TEST/packages/api/migrations"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: All tables present — should pass ─────────────────────────
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE IF NOT EXISTS foo (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE IF EXISTS foo;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_create-foo.sql"
assert_passes "All migration tables present in schema.sql"

# ─── Test 2: Migration table missing from schema.sql — should fail ────
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE IF NOT EXISTS bar (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE IF EXISTS bar;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_create-bar.sql"
assert_fails "Migration table missing from schema.sql is detected"

# ─── Test 3: Schema-qualified table (staging.captures) — should pass ──
setup_repo
printf '%s\n' "CREATE TABLE staging.captures (" "    id UUID PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE IF NOT EXISTS staging.captures (" "    id UUID PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE IF EXISTS staging.captures;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_staging.sql"
assert_passes "Schema-qualified table (staging.captures) matches"

# ─── Test 4: Migration that only ALTERs — no CREATE TABLE — should pass
setup_repo
printf '%s\n' "CREATE TABLE users (" "    id UUID PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "ALTER TABLE users ADD COLUMN google_id TEXT;" \
    "-- Down Migration" "ALTER TABLE users DROP COLUMN google_id;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_alter-users.sql"
assert_passes "ALTER-only migration (no CREATE TABLE) passes"

# ─── Test 5: Multiple migrations, one missing — should fail ───────────
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE foo;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_foo.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE bar (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE bar;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_bar.sql"
assert_fails "One of multiple migration tables missing is detected"

# ─── Test 6: Table in Down Migration only — should pass ───────────────
# A table referenced only in the Down section should not be required.
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "ALTER TABLE foo ADD COLUMN bar TEXT;" \
    "-- Down Migration" "CREATE TABLE IF NOT EXISTS backup_foo AS SELECT * FROM foo;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_alter.sql"
assert_passes "CREATE TABLE in Down section only is ignored"

# ─── Test 7: Case insensitivity — should pass ─────────────────────────
setup_repo
printf '%s\n' "CREATE TABLE MyTable (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "create table mytable (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE mytable;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_mytable.sql"
assert_passes "Case-insensitive table name matching works"

# ─── Test 8: No migration files — should pass ─────────────────────────
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
# Leave migrations dir empty — but we need at least one .sql file for glob
# Actually the glob will fail with no matches. Let's test with a no-table migration.
printf '%s\n' "-- Up Migration" "-- Just a comment" "-- Down Migration" \
    > "$TMPDIR_TEST/packages/api/migrations/0_noop.sql"
assert_passes "Migration with no CREATE TABLE passes"

# ─── Test 9: Output shows the migration source file ───────────────────
setup_repo
printf '%s\n' "CREATE TABLE foo (" "    id SERIAL PRIMARY KEY" ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' "-- Up Migration" "CREATE TABLE missing_table (" "    id SERIAL PRIMARY KEY" ");" \
    "-- Down Migration" "DROP TABLE missing_table;" \
    > "$TMPDIR_TEST/packages/api/migrations/5_missing.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "5_missing.sql"; then
    echo "PASS: Output includes source migration filename"
else
    echo "FAIL: Output does not include source migration filename"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# =========================================================================
# CONSTRAINT DRIFT TESTS
# =========================================================================

# ─── Test 10: Inline UNIQUE constraint covered by migration — should pass
setup_repo
printf '%s\n' \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL," \
    "    UNIQUE (case_id, party_id, role)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL," \
    "    UNIQUE (case_id, party_id, role)" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE case_parties;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
assert_passes "Inline UNIQUE constraint covered by inline in migration"

# ─── Test 11: Inline UNIQUE covered by ALTER TABLE in migration — pass
setup_repo
printf '%s\n' \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL," \
    "    UNIQUE (case_id, party_id, role)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE case_parties;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
printf '%s\n' \
    "-- Up Migration" \
    "ALTER TABLE case_parties" \
    "    ADD CONSTRAINT uq_cp UNIQUE (case_id, party_id, role);" \
    "-- Down Migration" \
    "ALTER TABLE case_parties DROP CONSTRAINT uq_cp;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_add-constraint.sql"
assert_passes "Inline UNIQUE covered by ALTER TABLE ADD CONSTRAINT in migration"

# ─── Test 12: UNIQUE constraint missing from migrations — should fail
setup_repo
printf '%s\n' \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL," \
    "    UNIQUE (case_id, party_id, role)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE case_parties;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
assert_fails "UNIQUE constraint in schema.sql but not in any migration is detected"

# ─── Test 13: Named CONSTRAINT in schema.sql — should pass when covered
setup_repo
printf '%s\n' \
    "CREATE TABLE judges (" \
    "    id UUID PRIMARY KEY," \
    "    canonical_name TEXT NOT NULL," \
    "    court_id UUID NOT NULL," \
    "    CONSTRAINT judges_canonical_name_court_id_key UNIQUE (canonical_name, court_id)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE judges (" \
    "    id UUID PRIMARY KEY," \
    "    canonical_name TEXT NOT NULL," \
    "    court_id UUID NOT NULL" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE judges;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
printf '%s\n' \
    "-- Up Migration" \
    "ALTER TABLE judges" \
    "    ADD CONSTRAINT judges_canonical_name_court_id_key UNIQUE (canonical_name, court_id);" \
    "-- Down Migration" \
    "ALTER TABLE judges DROP CONSTRAINT judges_canonical_name_court_id_key;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_add-unique.sql"
assert_passes "Named CONSTRAINT UNIQUE covered by ALTER TABLE migration"

# ─── Test 14: Single-column UNIQUE is ignored (not multi-column) ──────
setup_repo
printf '%s\n' \
    "CREATE TABLE users (" \
    "    id UUID PRIMARY KEY," \
    "    email TEXT NOT NULL UNIQUE" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE users (" \
    "    id UUID PRIMARY KEY," \
    "    email TEXT NOT NULL" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE users;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
assert_passes "Single-column UNIQUE on column definition is ignored"

# ─── Test 15: Constraint output shows table and columns ───────────────
setup_repo
printf '%s\n' \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY," \
    "    case_id UUID NOT NULL," \
    "    party_id UUID NOT NULL," \
    "    role TEXT NOT NULL," \
    "    UNIQUE (case_id, party_id, role)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE case_parties (" \
    "    id UUID PRIMARY KEY" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE case_parties;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "case_parties.*UNIQUE.*case_id.*party_id.*role"; then
    echo "PASS: Constraint drift output shows table and columns"
else
    echo "FAIL: Constraint drift output does not show table and columns"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 16: Multi-line ALTER TABLE / ADD CONSTRAINT / UNIQUE — pass
# Handles real-world migrations where ALTER, ADD CONSTRAINT, and UNIQUE
# are on three separate lines (e.g. migration 10).
setup_repo
printf '%s\n' \
    "CREATE TABLE judges (" \
    "    id UUID PRIMARY KEY," \
    "    canonical_name TEXT NOT NULL," \
    "    court_id UUID NOT NULL," \
    "    CONSTRAINT judges_name_court_key UNIQUE (canonical_name, court_id)" \
    ");" \
    > "$TMPDIR_TEST/packages/api/src/data-access/schema.sql"
printf '%s\n' \
    "-- Up Migration" \
    "CREATE TABLE judges (" \
    "    id UUID PRIMARY KEY," \
    "    canonical_name TEXT NOT NULL," \
    "    court_id UUID NOT NULL" \
    ");" \
    "-- Down Migration" \
    "DROP TABLE judges;" \
    > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
printf '%s\n' \
    "-- Up Migration" \
    "" \
    "ALTER TABLE judges" \
    "    ADD CONSTRAINT judges_name_court_key" \
    "    UNIQUE (canonical_name, court_id);" \
    "" \
    "-- Down Migration" \
    "ALTER TABLE judges DROP CONSTRAINT judges_name_court_key;" \
    > "$TMPDIR_TEST/packages/api/migrations/2_add-unique.sql"
assert_passes "Multi-line ALTER TABLE / ADD CONSTRAINT / UNIQUE (3 lines)"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
