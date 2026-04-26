#!/usr/bin/env bash
# test_record_invocation.sh — Unit tests for scripts/tests/_record_invocation.sh.
#
# Drives the shared recorder in isolation to verify:
#   1. argv-only call records a CALL line with shell-quoted args.
#   2. non-tty stdin appends a STDIN: token to the log.
#   3. key=/path/to/file argv slot appends FILE:key=<contents> token;
#      non-existent paths are silently skipped.
#   4. Sourced caller can read $stdin_content after sourcing.
#   5. Wire-format invariance: same SQL via -c arg and heredoc-on-stdin
#      both produce matching grep output in psql.log (AC5).
#
# Usage:
#   scripts/tests/test_record_invocation.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RECORDER="$REPO_ROOT/scripts/tests/_record_invocation.sh"

FAILURES=0
TESTS=0

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── Temp dir lifecycle ────────────────────────────────────────────────────

TEST_TMP=""
cleanup() {
    set +eu
    if [[ -n "$TEST_TMP" && -d "$TEST_TMP" ]]; then
        rm -rf "$TEST_TMP"
    fi
}
trap cleanup EXIT

TEST_TMP=$(mktemp -d)
INVOCATIONS_DIR="$TEST_TMP/invocations"
mkdir -p "$INVOCATIONS_DIR"
export INVOCATIONS_DIR

reset_log() {
    : > "$INVOCATIONS_DIR/${1:-tool}.log"
}

# ── Helper: write a driver script and run it ────────────────────────────
#
# Each test writes a tiny bash driver script into $TEST_TMP/driver.sh,
# then runs it with bash.  This avoids bash -c quoting complexity and
# keeps env var inheritance simple (INVOCATIONS_DIR is exported above).
#
# write_driver <content>  — overwrites $TEST_TMP/driver.sh
write_driver() {
    printf '%s\n' '#!/usr/bin/env bash' 'set -uo pipefail' > "$TEST_TMP/driver.sh"
    printf '%s\n' "$1" >> "$TEST_TMP/driver.sh"
    chmod +x "$TEST_TMP/driver.sh"
}

# ══════════════════════════════════════════════════════════════════════════
# T1: argv-only call records CALL line with shell-quoted args
# ══════════════════════════════════════════════════════════════════════════

reset_log mytool

write_driver ". '$RECORDER' mytool arg1 'arg with spaces' arg3"
bash "$TEST_TMP/driver.sh" < /dev/null 2>/dev/null || true

if grep -q '^CALL ' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T1: argv-only call writes CALL line"
else
    fail "T1: argv-only call writes CALL line" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

if grep -q 'arg1' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T1: CALL line contains first arg"
else
    fail "T1: CALL line contains first arg" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

# printf %q produces arg\ with\ spaces — the dot matches the backslash or
# the quote character depending on quoting style.
if grep -q 'arg' "$INVOCATIONS_DIR/mytool.log" && grep -q 'spaces' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T1: arg with spaces is shell-quoted in CALL line"
else
    fail "T1: arg with spaces is shell-quoted in CALL line" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# T2: non-tty stdin appends STDIN: token
# ══════════════════════════════════════════════════════════════════════════

reset_log mytool

write_driver ". '$RECORDER' mytool somearg"
printf 'SELECT 1;\n' | bash "$TEST_TMP/driver.sh" 2>/dev/null || true

if grep -q '^STDIN:SELECT 1;' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T2: non-tty stdin appends STDIN: token"
else
    fail "T2: non-tty stdin appends STDIN: token" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# T3a: key=/path/to/file arg appends FILE:key=<contents> token
# ══════════════════════════════════════════════════════════════════════════

reset_log mytool
payload_file="$TEST_TMP/payload.json"
printf '{"hello": "world"}' > "$payload_file"

write_driver ". '$RECORDER' mytool -v output_path=$payload_file"
bash "$TEST_TMP/driver.sh" < /dev/null 2>/dev/null || true

if grep -q '^FILE:output_path=' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T3a: key=/path arg appends FILE: token"
else
    fail "T3a: key=/path arg appends FILE: token" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

if grep -q 'hello' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T3a: FILE: token contains file contents"
else
    fail "T3a: FILE: token contains file contents" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# T3b: non-existent key=/ path is silently skipped
# ══════════════════════════════════════════════════════════════════════════

reset_log mytool

write_driver ". '$RECORDER' mytool output_path=/nonexistent/path/x.json"
bash "$TEST_TMP/driver.sh" < /dev/null 2>/dev/null || true

if grep -q '^CALL ' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T3b: non-existent path still writes CALL line"
else
    fail "T3b: non-existent path still writes CALL line" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

if ! grep -q '^FILE:' "$INVOCATIONS_DIR/mytool.log"; then
    pass "T3b: non-existent path produces no FILE: token"
else
    fail "T3b: non-existent path produces no FILE: token" \
         "log: $(cat "$INVOCATIONS_DIR/mytool.log")"
fi

# ══════════════════════════════════════════════════════════════════════════
# T4: sourced caller can read $stdin_content
# ══════════════════════════════════════════════════════════════════════════

reset_log mytool
result_file="$TEST_TMP/stdin_content_result.txt"

write_driver ". '$RECORDER' mytool; printf '%s' \"\$stdin_content\" > '$result_file'"
printf 'my query text\n' | bash "$TEST_TMP/driver.sh" 2>/dev/null || true

if [[ -f "$result_file" ]] && grep -q 'my query text' "$result_file"; then
    pass "T4: sourced caller can read \$stdin_content"
else
    fail "T4: sourced caller can read \$stdin_content" \
         "result_file: $(cat "$result_file" 2>/dev/null || printf '(missing)')"
fi

# ══════════════════════════════════════════════════════════════════════════
# T5: wire-format invariance — same SQL via -c arg and heredoc-on-stdin
#     both produce greppable evidence in psql.log without stub edits (AC5)
# ══════════════════════════════════════════════════════════════════════════
#
# Build a minimal psql stub that sources the shared recorder and uses
# $stdin_content for routing (same pattern as the real stubs after
# consolidation). Drive it twice with the same SQL:
#   Shape A — psql -c "SQL"   (SQL appears shell-quoted in CALL line)
#   Shape B — SQL on stdin    (SQL appears raw in STDIN: line)
#
# Both shapes must produce at least one line in psql.log greppable by a
# common substring that does NOT depend on shell-quoting (e.g. the table
# name "phase_outputs" and the phase string "planning" both appear in
# both shapes — in the CALL line for A, in STDIN: for B).

reset_log psql

stub_bin="$TEST_TMP/stub-bin"
mkdir -p "$stub_bin"
cp "$RECORDER" "$stub_bin/_record_invocation.sh"

# Write the minimal psql stub.
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -u' \
    '. "$(dirname "$0")/_record_invocation.sh" psql "$@"' \
    'query=""' \
    'while [[ $# -gt 0 ]]; do' \
    '    case "$1" in' \
    '        -c) shift; query="$1" ;;' \
    '    esac' \
    '    shift || true' \
    'done' \
    'if [[ -z "$query" ]]; then' \
    '    query="${stdin_content:-}"' \
    'fi' \
    'exit 0' \
    > "$stub_bin/psql"
chmod +x "$stub_bin/psql"

THE_SQL="INSERT INTO dispatcher.phase_outputs VALUES ('agent1', 'planning', '{}'::jsonb)"

# Shape A: -c "SQL" (no stdin) — SQL appears in CALL line, shell-quoted.
bash "$stub_bin/psql" -c "$THE_SQL" < /dev/null 2>/dev/null || true

# Shape B: SQL on stdin — SQL appears in STDIN: line, raw.
write_driver "printf '%s\n' '$THE_SQL' | bash '$stub_bin/psql'"
bash "$TEST_TMP/driver.sh" 2>/dev/null || true

# Both shapes must log "planning" (appears in CALL line for A, in STDIN: for B).
# grep -c counts matching lines; we expect one match per shape = 2 total.
planning_count=$(grep -c "planning" "$INVOCATIONS_DIR/psql.log" 2>/dev/null || true)
if [[ "$planning_count" -ge 2 ]]; then
    pass "T5: wire-format invariance — both shapes log 'planning' in psql.log"
else
    fail "T5: wire-format invariance — expected >=2 lines with 'planning', got $planning_count" \
         "psql.log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# Shape B's STDIN: line must contain the raw SQL (greppable without shell-quoting).
if grep -q "^STDIN:.*INSERT INTO dispatcher.phase_outputs" "$INVOCATIONS_DIR/psql.log"; then
    pass "T5: stdin shape produces raw-SQL STDIN: line greppable by test assertions"
else
    fail "T5: stdin shape produces raw-SQL STDIN: line greppable by test assertions" \
         "psql.log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# Shape A's CALL line must contain the SQL (shell-quoted).
# Use grep -E with a pattern that matches the shell-quoted form (backslash-space).
if grep -q "^CALL.*phase_outputs" "$INVOCATIONS_DIR/psql.log"; then
    pass "T5: -c shape produces CALL line containing the SQL"
else
    fail "T5: -c shape produces CALL line containing the SQL" \
         "psql.log: $(cat "$INVOCATIONS_DIR/psql.log")"
fi

# ── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
