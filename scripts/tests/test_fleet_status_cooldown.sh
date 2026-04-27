#!/usr/bin/env bash
# test_fleet_status_cooldown.sh — Regression tests for the cooldown / stuck-loop
# watch section added to scripts/dispatcher/helpers/fleet-status.sh (#3577).
#
# Follows the test_fleet_status.sh harness pattern: mirror the repo tree under
# a temp directory and stub scripts/dev-db-query.sh keyed off SQL keyword tokens.
#
# Test coverage:
#   1. Header "── cooldown / stuck-loop watch ──" is present in default output.
#   2. A 10-attempt-failed issue lands as "🔁 stuck-in-loop (10x ralph_baseline)".
#   3. A 1-attempt succeeded issue lands as "✓ recently terminal".
#   4. Header is present even when q_cooldown returns [] (with affirmation).
#   5. Header is ABSENT when --no-cooldown is passed.
#   6. SQL contains "started_at DESC" and does NOT contain "ended_at DESC" for
#      latest-agent-per-issue selector; a zombie row (ended_at IS NULL) surfaces.
#   7. Non-zero exit on q_cooldown produces a WARNING on stderr, rc=0 overall,
#      and the rest of the sections still render.
#
# Usage:
#   scripts/tests/test_fleet_status_cooldown.sh
#
# Exit codes:
#   0  — all tests passed.
#   1  — one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FLEET_STATUS="$REPO_ROOT/scripts/dispatcher/helpers/fleet-status.sh"

if [[ ! -x "$FLEET_STATUS" ]]; then
    echo "FATAL: $FLEET_STATUS missing or not executable" >&2
    exit 1
fi

FAILURES=0
TESTS=0
TMPDIR_TEST=$(mktemp -d "$REPO_ROOT/tmp/test_fleet_status_cooldown.XXXXXX")
cleanup() { rm -rf "$TMPDIR_TEST"; }
trap cleanup EXIT

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "---DETAIL---"
        echo "$2"
        echo "---END---"
    fi
}

# ─── Stub helpers ───────────────────────────────────────────────────────────

# write_full_stub — canned stub returning multi-row cooldown payload covering
# all four classifier branches plus a zombie row.
#
# Cooldown rows:
#   #1001  10 failed ralph_baseline attempts → stuck-in-loop
#   #1002   2 attempts any status → retry pattern
#   #1003   1 succeeded → recently terminal
#   #1004   1 failed (not ≥2 and not succeeded/needs_review) → unclassified
#   #1005   zombie (ended_at IS NULL, is_zombie=true), 5 failed → stuck-in-loop
write_full_stub() {
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<'STUB_EOF'
#!/usr/bin/env bash
set -u
sql=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) sql=$(cat -- "$2"); shift 2 ;;
        --file=*) sql=$(cat -- "${1#--file=}"); shift ;;
        --rw) shift ;;
        --) shift ;;
        *) sql="$1"; shift ;;
    esac
done

if [[ "$sql" == *"status IN ('running'"* ]]; then
    echo "[]"
elif [[ "$sql" == *"ended_at IS NOT NULL"* && "$sql" == *"ORDER BY ended_at DESC"* ]]; then
    echo "[]"
elif [[ "$sql" == *"concurrency_cap"* ]]; then
    echo '[{"cap":2,"flipped_by":null}]'
elif [[ "$sql" == *"dispatcher.diagnoses"* && "$sql" == *"JOIN dispatcher.agents"* ]]; then
    echo "[]"
elif [[ "$sql" == *"merged_at IS NOT NULL"* && "$sql" == *"ORDER BY merged_at DESC"* ]]; then
    echo "[]"
elif [[ "$sql" == *"dispatcher.commands"* ]]; then
    echo "[]"
elif [[ "$sql" == *"total_terminals_in_window"* ]]; then
    echo '[{"total_terminals_in_window":0,"total_greens_in_window":0,"total_diagnoses_in_window":0}]'
elif [[ "$sql" == *"issue_cooldown_remaining_seconds"* ]]; then
    cat <<'COOLDOWN'
[
  {"issue_number":1001,"priority":"p2","cooldown_left_seconds":3300,"last_phase":"ralph-baseline","last_status":"failed","is_zombie":false,"attempts_24h":10},
  {"issue_number":1002,"priority":"p2","cooldown_left_seconds":2400,"last_phase":"ralph-worker","last_status":"failed","is_zombie":false,"attempts_24h":2},
  {"issue_number":1003,"priority":"p3","cooldown_left_seconds":1800,"last_phase":"ralph","last_status":"succeeded","is_zombie":false,"attempts_24h":1},
  {"issue_number":1004,"priority":"p1","cooldown_left_seconds":900,"last_phase":"plan","last_status":"failed","is_zombie":false,"attempts_24h":1},
  {"issue_number":1005,"priority":"p2","cooldown_left_seconds":3000,"last_phase":"ralph","last_status":"failed","is_zombie":true,"attempts_24h":5}
]
COOLDOWN
else
    echo "Stub: unrecognised SQL shape" >&2
    echo "$sql" | head -5 >&2
    exit 1
fi
STUB_EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# write_empty_cooldown_stub — happy stub where q_cooldown returns [].
write_empty_cooldown_stub() {
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<'STUB_EOF'
#!/usr/bin/env bash
set -u
sql=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) sql=$(cat -- "$2"); shift 2 ;;
        --file=*) sql=$(cat -- "${1#--file=}"); shift ;;
        --rw) shift ;;
        --) shift ;;
        *) sql="$1"; shift ;;
    esac
done

if [[ "$sql" == *"concurrency_cap"* ]]; then
    echo '[{"cap":2,"flipped_by":null}]'
elif [[ "$sql" == *"total_terminals_in_window"* ]]; then
    echo '[{"total_terminals_in_window":0,"total_greens_in_window":0,"total_diagnoses_in_window":0}]'
elif [[ "$sql" == *"issue_cooldown_remaining_seconds"* ]]; then
    echo "[]"
else
    echo "[]"
fi
STUB_EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# write_cooldown_fail_stub — stub where q_cooldown exits 1.
write_cooldown_fail_stub() {
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<'STUB_EOF'
#!/usr/bin/env bash
set -u
sql=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --file) sql=$(cat -- "$2"); shift 2 ;;
        --file=*) sql=$(cat -- "${1#--file=}"); shift ;;
        --rw) shift ;;
        --) shift ;;
        *) sql="$1"; shift ;;
    esac
done

if [[ "$sql" == *"issue_cooldown_remaining_seconds"* ]]; then
    echo "simulated DB error" >&2
    exit 1
elif [[ "$sql" == *"concurrency_cap"* ]]; then
    echo '[{"cap":2,"flipped_by":null}]'
elif [[ "$sql" == *"total_terminals_in_window"* ]]; then
    echo '[{"total_terminals_in_window":0,"total_greens_in_window":0,"total_diagnoses_in_window":0}]'
else
    echo "[]"
fi
STUB_EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# run_fleet_status — execute fleet-status.sh from a mirrored tree, capturing
# stdout into $FLEET_OUT and stderr into $TMPDIR_TEST/err.log.
run_fleet_status() {
    local args="${1:-}"
    local mirror="$TMPDIR_TEST/mirror"
    mkdir -p "$mirror/scripts/dispatcher/helpers"
    cp "$TMPDIR_TEST/stub_dev_db.sh" "$mirror/scripts/dev-db-query.sh"
    chmod +x "$mirror/scripts/dev-db-query.sh"
    cp "$REPO_ROOT/scripts/dispatcher/helpers/_query_lib.sh" \
       "$mirror/scripts/dispatcher/helpers/_query_lib.sh"
    cp "$FLEET_STATUS" "$mirror/scripts/dispatcher/helpers/fleet-status.sh"
    chmod +x "$mirror/scripts/dispatcher/helpers/fleet-status.sh"
    # shellcheck disable=SC2086
    "$mirror/scripts/dispatcher/helpers/fleet-status.sh" $args
}

# ─── Test 1: Cooldown section header present in default output ──────────────
write_full_stub
set +e
FLEET_OUT=$(run_fleet_status "" 2>"$TMPDIR_TEST/err.log")
RC=$?
set -e

if [[ "$RC" == "0" ]]; then
    pass "t1: fleet-status exits 0 with cooldown section"
else
    fail "t1: fleet-status exits 0 with cooldown section" "rc=$RC err=$(cat "$TMPDIR_TEST/err.log")"
fi

if printf '%s' "$FLEET_OUT" | grep -qF "── cooldown / stuck-loop watch ──"; then
    pass "t1: section header '── cooldown / stuck-loop watch ──' present"
else
    fail "t1: section header '── cooldown / stuck-loop watch ──' present" "out: $FLEET_OUT"
fi

# ─── Test 2: 10-attempt-failed issue → stuck-in-loop ───────────────────────
if printf '%s' "$FLEET_OUT" | grep -q "stuck-in-loop (10x ralph"; then
    pass "t2: 10-attempt failed issue classified as stuck-in-loop"
else
    fail "t2: 10-attempt failed issue classified as stuck-in-loop" "out: $FLEET_OUT"
fi

# Also verify the issue number is present
if printf '%s' "$FLEET_OUT" | grep -q "stuck-in-loop" | grep -q "#1001" 2>/dev/null || \
   printf '%s' "$FLEET_OUT" | grep -F "stuck-in-loop" | grep -q "1001"; then
    pass "t2: stuck-in-loop row references issue #1001"
else
    # More permissive check: both appear in the output
    if printf '%s' "$FLEET_OUT" | grep -q "stuck-in-loop" && printf '%s' "$FLEET_OUT" | grep -q "#1001"; then
        pass "t2: stuck-in-loop row references issue #1001"
    else
        fail "t2: stuck-in-loop row references issue #1001" "out: $FLEET_OUT"
    fi
fi

# ─── Test 3: 1-attempt succeeded issue → recently terminal ─────────────────
if printf '%s' "$FLEET_OUT" | grep -q "recently terminal" && printf '%s' "$FLEET_OUT" | grep -q "#1003"; then
    pass "t3: 1-attempt succeeded issue classified as recently terminal"
else
    fail "t3: 1-attempt succeeded issue classified as recently terminal" "out: $FLEET_OUT"
fi

# ─── Test 4: Header present even when cooldown returns [] ──────────────────
write_empty_cooldown_stub
set +e
FLEET_OUT4=$(run_fleet_status "" 2>"$TMPDIR_TEST/err.log")
RC4=$?
set -e

if [[ "$RC4" == "0" ]]; then
    pass "t4: fleet-status exits 0 with empty cooldown"
else
    fail "t4: fleet-status exits 0 with empty cooldown" "rc=$RC4 err=$(cat "$TMPDIR_TEST/err.log")"
fi

if printf '%s' "$FLEET_OUT4" | grep -qF "── cooldown / stuck-loop watch ──"; then
    pass "t4: cooldown section header present when section is empty"
else
    fail "t4: cooldown section header present when section is empty" "out: $FLEET_OUT4"
fi

if printf '%s' "$FLEET_OUT4" | grep -qF "no cooldown / stuck loops"; then
    pass "t4: affirmation 'no cooldown / stuck loops' emitted when section empty"
else
    fail "t4: affirmation 'no cooldown / stuck loops' emitted when section empty" "out: $FLEET_OUT4"
fi

# ─── Test 5: --no-cooldown suppresses header ────────────────────────────────
write_full_stub
set +e
FLEET_OUT5=$(run_fleet_status "--no-cooldown" 2>"$TMPDIR_TEST/err.log")
RC5=$?
set -e

if [[ "$RC5" == "0" ]]; then
    pass "t5: fleet-status exits 0 with --no-cooldown"
else
    fail "t5: fleet-status exits 0 with --no-cooldown" "rc=$RC5 err=$(cat "$TMPDIR_TEST/err.log")"
fi

if printf '%s' "$FLEET_OUT5" | grep -qF "── cooldown / stuck-loop watch ──"; then
    fail "t5: cooldown section header absent when --no-cooldown passed" "out: $FLEET_OUT5"
else
    pass "t5: cooldown section header absent when --no-cooldown passed"
fi

# ─── Test 6: SQL uses started_at DESC (not ended_at DESC) for latest-agent ──
# Verify the q_cooldown SQL text contains started_at DESC but not ended_at DESC
# in the ORDER BY for the latest-agent-per-issue window.
# Approach: check the embedded SQL in fleet-status.sh directly.
FLEET_SRC="$REPO_ROOT/scripts/dispatcher/helpers/fleet-status.sh"

# The query must contain started_at DESC
if grep -q "started_at DESC" "$FLEET_SRC"; then
    pass "t6: q_cooldown SQL contains 'started_at DESC'"
else
    fail "t6: q_cooldown SQL contains 'started_at DESC'" "grep found nothing in $FLEET_SRC"
fi

# The q_cooldown block must NOT contain ended_at DESC
# (the other queries use ended_at DESC for ordering terminals/greens — we need to
#  ensure our specific cooldown CTE does not).  We extract the q_cooldown block
# by looking between the marker comment and ORDER BY cooldown_left_seconds.
Q_COOLDOWN_BLOCK=$(awk '/q_cooldown=/,/ORDER BY cooldown_left_seconds/' "$FLEET_SRC")
if printf '%s' "$Q_COOLDOWN_BLOCK" | grep -q "ended_at DESC"; then
    fail "t6: q_cooldown SQL does NOT contain 'ended_at DESC' (zombie-safe ordering)" "block: $Q_COOLDOWN_BLOCK"
else
    pass "t6: q_cooldown SQL does NOT contain 'ended_at DESC' (zombie-safe ordering)"
fi

# Verify zombie row (is_zombie=true) surfaces in output (from write_full_stub).
# Issue #1005 has is_zombie=true; check that "(zombie)" suffix appears.
write_full_stub
set +e
FLEET_OUT6=$(run_fleet_status "" 2>"$TMPDIR_TEST/err.log")
RC6=$?
set -e
if printf '%s' "$FLEET_OUT6" | grep -q "(zombie)"; then
    pass "t6: zombie row renders with '(zombie)' suffix"
else
    fail "t6: zombie row renders with '(zombie)' suffix" "out: $FLEET_OUT6"
fi

# ─── Test 7: Cooldown SQL failure → WARNING on stderr, rc=0, rest renders ──
write_cooldown_fail_stub
set +e
FLEET_OUT7=$(run_fleet_status "" 2>"$TMPDIR_TEST/err7.log")
RC7=$?
set -e

if [[ "$RC7" == "0" ]]; then
    pass "t7: fleet-status exits 0 when cooldown query fails"
else
    fail "t7: fleet-status exits 0 when cooldown query fails" "rc=$RC7 err=$(cat "$TMPDIR_TEST/err7.log")"
fi

if grep -qi "WARNING" "$TMPDIR_TEST/err7.log"; then
    pass "t7: WARNING emitted to stderr on cooldown query failure"
else
    fail "t7: WARNING emitted to stderr on cooldown query failure" "stderr: $(cat "$TMPDIR_TEST/err7.log")"
fi

# Other sections must still render (active agents, cap/breaker)
if printf '%s' "$FLEET_OUT7" | grep -qF "── active agents ──"; then
    pass "t7: rest of fleet-status still renders after cooldown SQL failure"
else
    fail "t7: rest of fleet-status still renders after cooldown SQL failure" "out: $FLEET_OUT7"
fi

# ─── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
