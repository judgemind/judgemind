#!/usr/bin/env bash
# test_check_migration_files.sh — Tests for check-migration-files.sh
#
# Creates temporary directory structures to verify that the checker correctly
# validates migration file naming and sequencing.
#
# Usage:
#   scripts/tests/test_check_migration_files.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-migration-files.sh"
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

# ─── Test 1: Sequential migrations 1-3 — should pass ─────────────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
echo "ALTER TABLE foo ADD COLUMN bar TEXT;" > "$TMPDIR_TEST/packages/api/migrations/2_add-bar.sql"
echo "CREATE INDEX idx ON foo(bar);" > "$TMPDIR_TEST/packages/api/migrations/3_add-index.sql"
assert_passes "Sequential migrations 1-3"

# ─── Test 2: Gap in sequence — should fail ────────────────────────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
echo "CREATE INDEX idx ON foo(id);" > "$TMPDIR_TEST/packages/api/migrations/3_add-index.sql"
assert_fails "Gap in migration sequence (missing 2)"

# ─── Test 3: Duplicate migration number — should fail ─────────────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
echo "CREATE TABLE bar (id INT);" > "$TMPDIR_TEST/packages/api/migrations/1_also-initial.sql"
echo "ALTER TABLE foo ADD COLUMN x TEXT;" > "$TMPDIR_TEST/packages/api/migrations/2_alter.sql"
assert_fails "Duplicate migration number"

# ─── Test 4: Invalid filename — should fail ───────────────────────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/initial.sql"
assert_fails "Invalid migration filename (no number prefix)"

# ─── Test 5: Single migration — should pass ───────────────────────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
assert_passes "Single migration file"

# ─── Test 6: No migration files — should pass ─────────────────────────
setup_repo
assert_passes "Empty migrations directory"

# ─── Test 7: Real-world naming — should pass ──────────────────────────
setup_repo
echo "-- schema" > "$TMPDIR_TEST/packages/api/migrations/1_initial-schema.sql"
echo "-- auth" > "$TMPDIR_TEST/packages/api/migrations/2_auth-google-id-refresh-tokens.sql"
echo "-- unique" > "$TMPDIR_TEST/packages/api/migrations/3_rulings-document-id-unique.sql"
assert_passes "Real-world migration naming convention"

# ─── Test 8: Output shows expected range ──────────────────────────────
setup_repo
echo "-- m1" > "$TMPDIR_TEST/packages/api/migrations/1_first.sql"
echo "-- m2" > "$TMPDIR_TEST/packages/api/migrations/2_second.sql"
echo "-- m3" > "$TMPDIR_TEST/packages/api/migrations/3_third.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "1 through 3"; then
    echo "PASS: Output shows migration range"
else
    echo "FAIL: Output does not show migration range"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 9: Naming-pattern violation emits Fix block (#4346) ────────
setup_repo
echo "CREATE TABLE foo (id INT);" > "$TMPDIR_TEST/packages/api/migrations/initial.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "^Fix for naming-pattern violation 'initial.sql':" \
    && echo "$output" | grep -q "git mv 'initial.sql'"; then
    echo "PASS: Test 9 — naming-pattern Fix block emitted"
else
    echo "FAIL: Test 9 — Fix block missing for naming-pattern violation"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 10: Duplicate-number violation emits Fix block (#4346) ─────
setup_repo
echo "-- a" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
echo "-- b" > "$TMPDIR_TEST/packages/api/migrations/1_also-initial.sql"
echo "-- c" > "$TMPDIR_TEST/packages/api/migrations/2_alter.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "^Fix for duplicate migration number 1:" \
    && echo "$output" | grep -q "1_initial.sql" \
    && echo "$output" | grep -q "1_also-initial.sql" \
    && echo "$output" | grep -q "3_<topic>.sql"; then
    echo "PASS: Test 10 — duplicate-number Fix block emitted with concrete files + next free slot"
else
    echo "FAIL: Test 10 — Fix block missing for duplicate-number violation"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 11: Gap violation emits Fix block (#4346) ──────────────────
setup_repo
echo "-- a" > "$TMPDIR_TEST/packages/api/migrations/1_initial.sql"
echo "-- c" > "$TMPDIR_TEST/packages/api/migrations/3_third.sql"
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if echo "$output" | grep -q "^Fix for migration number gap" \
    && echo "$output" | grep -q "3_third.sql"; then
    echo "PASS: Test 11 — gap Fix block emitted with concrete renumber suggestion"
else
    echo "FAIL: Test 11 — Fix block missing for gap violation"
    echo "  Got: $output"
    FAILURES=$((FAILURES + 1))
fi

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
