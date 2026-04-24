#!/usr/bin/env bash
# test_fleet_status.sh — Unit + smoke tests for scripts/dispatcher/helpers/fleet-status.sh
#
# Tests the refactored split-queries + jq-assembly pipeline introduced in
# #3195. Each test stubs $dev_db (the path to dev-db-query.sh) so we can
# assert that fleet-status.sh correctly assembles a snapshot from multiple
# small queries instead of one bundled CTE query — the latter was subject
# to SSM / ECS-exec mid-stream truncation.
#
# The dev-db-query.sh stub returns canned JSON arrays (matching the
# compact format emitted by dev_db_query_runner.py post-#3195).
#
# Usage:
#   scripts/tests/test_fleet_status.sh
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
TMPDIR_TEST=$(mktemp -d)
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

# Write a stub at $TMPDIR_TEST/stub_dev_db.sh that returns different
# canned payloads depending on which sub-query fleet-status.sh runs.  We
# key off the SQL keyword tokens (dispatcher.agents / dispatcher.config /
# dispatcher.diagnoses / dispatcher.commands) plus WHERE clauses to
# distinguish the seven sub-queries.
#
# The stub emits output in the same shape dev-db-query.sh produces post-
# #3195 — an SSM-banner-free stream ending in compact JSON.  That means a
# single line wrapped in a jq-parseable array.
write_happy_stub() {
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<'EOF'
#!/usr/bin/env bash
# Happy-path stub. The SQL is passed as $1 (positional) or via --file.
# We distinguish by keyword presence.
set -u

# Resolve SQL content from positional or --file
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

# active agents — matches only the active-agents query
if [[ "$sql" == *"status IN ('running'"* ]]; then
    cat <<'ACTIVE'
[{"agent_id":"abc12345-aaaa-bbbb-cccc-deadbeefdeed","issue_number":2797,"phase":"ralph","status":"running","priority":"p2","started_utc":"2026-04-24T11:00:00Z","started_pt":"04:00:00 PT","lifetime":"0:15:00"}]
ACTIVE
# terminals
elif [[ "$sql" == *"ended_at IS NOT NULL"* && "$sql" == *"ORDER BY ended_at DESC"* ]]; then
    cat <<'TERMINALS'
[{"agent_id":"def67890-aaaa-bbbb-cccc-deadbeefaa00","issue_number":3193,"phase":"cleanup_blocked","status":"succeeded","pr_number":3194,"failure_summary":null,"ended_utc":"2026-04-24T11:11:54Z","ended_pt":"04:11:54 PT","lifetime":"0:28:43"}]
TERMINALS
# config
elif [[ "$sql" == *"concurrency_cap"* ]]; then
    cat <<'CONFIG'
[{"cap":1,"flipped_by":null}]
CONFIG
# diagnoses
elif [[ "$sql" == *"dispatcher.diagnoses"* && "$sql" == *"JOIN dispatcher.agents"* ]]; then
    cat <<'DIAG'
[{"diagnosis_id":21,"agent_id":"def67890-aaaa-bbbb-cccc-deadbeefaa00","issue_number":3193,"status":"completed","action":"retry_with_hint","started_utc":"2026-04-24T11:11:56Z","started_pt":"04:11:56 PT"}]
DIAG
# greens
elif [[ "$sql" == *"merged_at IS NOT NULL"* && "$sql" == *"ORDER BY merged_at DESC"* ]]; then
    cat <<'GREENS'
[{"agent_id":"def67890-aaaa-bbbb-cccc-deadbeefaa00","issue_number":3193,"pr_number":3194,"merged_utc":"2026-04-24T11:11:54Z","merged_pt":"04:11:54 PT","lifetime":"0:28:43","issue_title":"dx(tests): test_agent_runner_entrypoint"}]
GREENS
# pending commands
elif [[ "$sql" == *"dispatcher.commands"* ]]; then
    echo "[]"
# totals
elif [[ "$sql" == *"total_terminals_in_window"* ]]; then
    cat <<'TOTALS'
[{"total_terminals_in_window":2,"total_greens_in_window":1,"total_diagnoses_in_window":1}]
TOTALS
else
    echo "Stub: unrecognised SQL shape" >&2
    echo "$sql" | head -5 >&2
    exit 1
fi
EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# Build a shim that overrides fleet-status.sh's $dev_db resolution. The
# real script resolves $dev_db as "$script_dir/../../dev-db-query.sh" —
# we replace that by prepending a wrapper on PATH *and* symlinking into a
# tree that mimics the repo layout.
run_fleet_status() {
    local args="${1:-}"
    # Prepare a mirrored tree: TMPDIR_TEST/scripts/dev-db-query.sh (stub)
    # and TMPDIR_TEST/scripts/dispatcher/helpers/fleet-status.sh (real).
    local mirror="$TMPDIR_TEST/mirror"
    mkdir -p "$mirror/scripts/dispatcher/helpers"
    cp "$TMPDIR_TEST/stub_dev_db.sh" "$mirror/scripts/dev-db-query.sh"
    chmod +x "$mirror/scripts/dev-db-query.sh"
    cp "$REPO_ROOT/scripts/dispatcher/helpers/_query_lib.sh" "$mirror/scripts/dispatcher/helpers/_query_lib.sh"
    cp "$FLEET_STATUS" "$mirror/scripts/dispatcher/helpers/fleet-status.sh"
    chmod +x "$mirror/scripts/dispatcher/helpers/fleet-status.sh"
    "$mirror/scripts/dispatcher/helpers/fleet-status.sh" $args
}

# ─── Test 1: Happy path snapshot assembly ───────────────────────────────
write_happy_stub
set +e
out=$(run_fleet_status "" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "0" ]]; then
    pass "happy path: fleet-status.sh exits 0"
else
    fail "happy path: exit 0" "rc=$rc err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 2: Output contains all expected section headers ───────────────
for header in "── active agents ──" "── cap / breaker ──" "── terminals in window" "── daemon-shipped greens" "── diagnoses in window" "── pending commands ──"; do
    if printf '%s' "$out" | grep -qF "$header"; then
        pass "happy path: renders section '$header'"
    else
        fail "happy path: missing section '$header'" "out: $out"
    fi
done

# ─── Test 3: Active agent row is rendered correctly ─────────────────────
if printf '%s' "$out" | grep -q "abc12345.*#2797.*ralph.*running"; then
    pass "happy path: active agent row renders"
else
    fail "happy path: active agent row" "out: $out"
fi

# ─── Test 4: Terminal row with PR rendered ──────────────────────────────
if printf '%s' "$out" | grep -q "#3193.*cleanup_blocked/succeeded.*PR #3194"; then
    pass "happy path: terminal row renders with PR number"
else
    fail "happy path: terminal row" "out: $out"
fi

# ─── Test 5: Cap/breaker section renders config ─────────────────────────
# The formatter substitutes `(null)` for a null flipped_by (see the
# `$cfg.flipped_by // "(null)"` expression in fleet-status.sh).
if printf '%s' "$out" | grep -qE "cap: 1 .*flipped_by: \(null\)"; then
    pass "happy path: cap/breaker renders from config query"
else
    fail "happy path: cap/breaker" "out: $out"
fi

# ─── Test 6: No SSM trailer/banner leakage ──────────────────────────────
# The stub does not emit banners, so the output should be clean.
if printf '%s' "$out" | grep -q "Cannot perform start session\|Exiting session with\|Starting session with\|The Session Manager plugin"; then
    fail "happy path: SSM chatter leaked into output" "out: $out"
else
    pass "happy path: no SSM chatter in output"
fi

# ─── Test 7: Empty collections handled gracefully ───────────────────────
# Stub that returns [] for every query (all CTEs empty).
cat >"$TMPDIR_TEST/stub_dev_db.sh" <<'EOF'
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
    echo '[{"cap":null,"flipped_by":null}]'
elif [[ "$sql" == *"total_terminals_in_window"* ]]; then
    echo '[{"total_terminals_in_window":0,"total_greens_in_window":0,"total_diagnoses_in_window":0}]'
else
    echo "[]"
fi
EOF
chmod +x "$TMPDIR_TEST/stub_dev_db.sh"

set +e
out2=$(run_fleet_status "" 2>"$TMPDIR_TEST/err.log")
rc2=$?
set -e
if [[ "$rc2" == "0" ]]; then
    pass "empty fleet: fleet-status.sh exits 0"
else
    fail "empty fleet: exit 0" "rc=$rc2 err=$(cat "$TMPDIR_TEST/err.log")"
fi
if printf '%s' "$out2" | grep -q "── active agents ──"; then
    # The "(none)" placeholder should appear under each empty collection.
    if printf '%s' "$out2" | grep -A1 "── active agents ──" | grep -q "(none)"; then
        pass "empty fleet: shows '(none)' for active agents"
    else
        fail "empty fleet: missing '(none)' marker under active" "out: $out2"
    fi
else
    fail "empty fleet: missing active section" "out: $out2"
fi

# ─── Test 8: --since argument is validated ──────────────────────────────
set +e
out3=$(run_fleet_status "--since invalid" 2>"$TMPDIR_TEST/err.log")
rc3=$?
set -e
if [[ "$rc3" == "1" ]] && grep -q "value like" "$TMPDIR_TEST/err.log"; then
    pass "arg validation: --since rejects invalid value"
else
    fail "arg validation: --since invalid" "rc=$rc3 err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 9: 20 consecutive stubbed runs all succeed ────────────────────
# Acceptance criterion #1 from #3195 — once the SSM-truncation-prone
# bundled CTE was split into small queries, 20 consecutive runs should
# all exit 0. The stub removes the SSM nondeterminism so this measures
# the code path itself.
write_happy_stub
stress_failures=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    set +e
    out_n=$(run_fleet_status "" 2>/dev/null)
    rc_n=$?
    set -e
    if [[ "$rc_n" != "0" ]]; then
        stress_failures=$((stress_failures + 1))
    fi
done
if [[ "$stress_failures" == "0" ]]; then
    pass "stress: 20 consecutive stubbed runs all exited 0"
else
    fail "stress: $stress_failures/20 iterations failed"
fi

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
