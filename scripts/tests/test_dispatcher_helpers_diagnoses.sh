#!/usr/bin/env bash
# test_dispatcher_helpers_diagnoses.sh — Unit + smoke tests for
# scripts/dispatcher/helpers/diagnoses.sh
#
# Tests the payload-shrink refactor introduced in #3203:
#   - default SELECT drops full_recommendation, adds extras + truncated reasoning
#   - --full-reasoning flag disables the reasoning truncation
#   - --json mode fetches full_recommendation via per-row query and merges it back
#
# The dev-db-query.sh stub returns canned JSON arrays (matching the compact
# format emitted by dev_db_query_runner.py post-#3195).
#
# Usage:
#   scripts/tests/test_dispatcher_helpers_diagnoses.sh
#
# Exit codes:
#   0  — all tests passed.
#   1  — one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIAGNOSES="$REPO_ROOT/scripts/dispatcher/helpers/diagnoses.sh"

if [[ ! -x "$DIAGNOSES" ]]; then
    echo "FATAL: $DIAGNOSES missing or not executable" >&2
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

# ─── Canned data files ───────────────────────────────────────────────────────
# Write canned JSON to files; the stub reads from these paths.
# extras is pre-filtered server-side: recommendation minus action and reasoning.

# Main rows response (2 rows): diagnosis 42 with short reasoning + extras,
# diagnosis 41 with truncated long reasoning (ends in ellipsis).
printf '%s\n' '[{"rows":[{"diagnosis_id":42,"agent_id":"abc12345-aaaa-bbbb-cccc-deadbeefdeed","issue_number":3008,"status":"completed","started_utc":"2026-04-24T11:00:00Z","started_pt":"04:00:00 PT","completed_utc":"2026-04-24T11:05:00Z","duration":"00:05:00","action":"retry_with_hint","reasoning":"Agent failed due to a transient network error. Retry should succeed.","extras":{"hint":"skip_migration","confidence":"high"},"outcome":null,"failure_id":99,"failure_category":"timeout"},{"diagnosis_id":41,"agent_id":"def67890-aaaa-bbbb-cccc-deadbeefaa00","issue_number":3007,"status":"completed","started_utc":"2026-04-24T10:00:00Z","started_pt":"03:00:00 PT","completed_utc":"2026-04-24T10:02:00Z","duration":"00:02:00","action":"abandon","reasoning":"TRUNCATED_REASONING_PLACEHOLDER…","extras":{},"outcome":null,"failure_id":98,"failure_category":"ci_failure"}]}]' >"$TMPDIR_TEST/canned_main.json"

# Full-reasoning row: diagnosis 41 with untruncated long reasoning.
printf '%s\n' '[{"rows":[{"diagnosis_id":41,"agent_id":"def67890-aaaa-bbbb-cccc-deadbeefaa00","issue_number":3007,"status":"completed","started_utc":"2026-04-24T10:00:00Z","started_pt":"03:00:00 PT","completed_utc":"2026-04-24T10:02:00Z","duration":"00:02:00","action":"abandon","reasoning":"This is a very long reasoning string that exceeds eight hundred characters. The agent failed because the CI pipeline encountered a timeout in the test suite. After reviewing the logs it appears the database migration took longer than expected due to a table lock contention. The recommendation is to retry with a hint to skip the migration step and apply it separately. Additional context the lock was held by a background vacuum process that has since completed. The agent should be able to proceed normally on the next attempt. The root cause analysis points to a configuration issue in the migration runner that can be addressed in a follow-up task. END_OF_REASONING_MARKER","extras":{},"outcome":null,"failure_id":98,"failure_category":"ci_failure"}]}]' >"$TMPDIR_TEST/canned_full.json"

# Per-row recommendation fetch for diagnosis_id = 42.
printf '%s\n' '[{"recommendation":{"action":"retry_with_hint","reasoning":"Agent failed due to a transient network error. Retry should succeed.","hint":"skip_migration","confidence":"high"}}]' >"$TMPDIR_TEST/canned_rec42.json"

# Per-row recommendation fetch for diagnosis_id = 41.
printf '%s\n' '[{"recommendation":{"action":"abandon","reasoning":"FULL_UNTRUNCATED_REASONING_FOR_41"}}]' >"$TMPDIR_TEST/canned_rec41.json"

# ─── Stub builder ────────────────────────────────────────────────────────────
# write_stub MAIN_JSON_FILE
# The stub reads canned data from files to avoid heredoc-in-heredoc quoting
# complications. MAIN_FILE is used for the main diagnoses query response.
write_stub() {
    local main_file="$1"
    local stub="$TMPDIR_TEST/stub_dev_db.sh"
    printf '#!/usr/bin/env bash\nset -u\n' >"$stub"
    printf 'sql=""\n' >>"$stub"
    printf 'while [[ $# -gt 0 ]]; do\n' >>"$stub"
    printf '    case "$1" in\n' >>"$stub"
    printf '        --file) sql=$(cat -- "$2"); shift 2 ;;\n' >>"$stub"
    printf '        --file=*) sql=$(cat -- "${1#--file=}"); shift ;;\n' >>"$stub"
    printf '        --rw) shift ;;\n' >>"$stub"
    printf '        --) shift ;;\n' >>"$stub"
    printf '        *) sql="$1"; shift ;;\n' >>"$stub"
    printf '    esac\n' >>"$stub"
    printf 'done\n' >>"$stub"
    # Agent prefix resolve query — returns empty array (no prefix filter in tests)
    printf 'if [[ "$sql" == *"dispatcher.agents"* && "$sql" == *"LIKE"* ]]; then\n' >>"$stub"
    printf '    printf '"'"'[]\\n'"'"'\n' >>"$stub"
    # Per-row recommendation fetch for diagnosis_id = 41
    printf 'elif [[ "$sql" == *"WHERE diagnosis_id = 41"* ]]; then\n' >>"$stub"
    printf '    cat %s\n' "$TMPDIR_TEST/canned_rec41.json" >>"$stub"
    # Per-row recommendation fetch for diagnosis_id = 42
    printf 'elif [[ "$sql" == *"WHERE diagnosis_id = 42"* ]]; then\n' >>"$stub"
    printf '    cat %s\n' "$TMPDIR_TEST/canned_rec42.json" >>"$stub"
    # Main diagnoses query
    printf 'elif [[ "$sql" == *"dispatcher.diagnoses d"* && "$sql" == *"jsonb_agg"* ]]; then\n' >>"$stub"
    printf '    cat %s\n' "$main_file" >>"$stub"
    printf 'else\n' >>"$stub"
    printf '    printf "Stub: unrecognised SQL shape\\n" >&2\n' >>"$stub"
    printf '    printf "%%s\\n" "$sql" | head -5 >&2\n' >>"$stub"
    printf '    exit 1\n' >>"$stub"
    printf 'fi\n' >>"$stub"
    chmod +x "$stub"
}

# Mirror the helper into a temp tree so $dev_db resolves to the stub.
# diagnoses.sh resolves dev_db as "$script_dir/../../dev-db-query.sh",
# so: mirror/scripts/dev-db-query.sh (stub)
#     mirror/scripts/dispatcher/helpers/diagnoses.sh (real copy)
#     mirror/scripts/dispatcher/helpers/_query_lib.sh (real copy)
run_diagnoses() {
    local args="${1:-}"
    local mirror="$TMPDIR_TEST/mirror"
    mkdir -p "$mirror/scripts/dispatcher/helpers"
    cp "$TMPDIR_TEST/stub_dev_db.sh" "$mirror/scripts/dev-db-query.sh"
    chmod +x "$mirror/scripts/dev-db-query.sh"
    cp "$REPO_ROOT/scripts/dispatcher/helpers/_query_lib.sh" "$mirror/scripts/dispatcher/helpers/_query_lib.sh"
    cp "$DIAGNOSES" "$mirror/scripts/dispatcher/helpers/diagnoses.sh"
    chmod +x "$mirror/scripts/dispatcher/helpers/diagnoses.sh"
    # shellcheck disable=SC2086
    "$mirror/scripts/dispatcher/helpers/diagnoses.sh" $args
}

# ─── Test 1: Happy path exits 0 ─────────────────────────────────────────────
write_stub "$TMPDIR_TEST/canned_main.json"
set +e
out=$(run_diagnoses "--recent 5" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "0" ]]; then
    pass "happy path: diagnoses.sh exits 0"
else
    fail "happy path: exit 0" "rc=$rc err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 2: Section headers present ────────────────────────────────────────
for header in "── diagnosis #42 ──" "── diagnosis #41 ──"; do
    if printf '%s' "$out" | grep -qF "$header"; then
        pass "happy path: renders section '$header'"
    else
        fail "happy path: missing section '$header'" "out: $out"
    fi
done

# ─── Test 3: extras bullets rendered ────────────────────────────────────────
if printf '%s' "$out" | grep -qF "hint:"; then
    pass "happy path: extras bullet 'hint:' rendered"
else
    fail "happy path: missing extras bullet" "out: $out"
fi

# ─── Test 4: reasoning lines rendered ───────────────────────────────────────
if printf '%s' "$out" | grep -q "reasoning:"; then
    pass "happy path: reasoning section rendered"
else
    fail "happy path: missing reasoning section" "out: $out"
fi

# ─── Test 5: action rendered ─────────────────────────────────────────────────
if printf '%s' "$out" | grep -q "action:.*retry_with_hint"; then
    pass "happy path: action field rendered"
else
    fail "happy path: missing action field" "out: $out"
fi

# ─── Test 6: failure category rendered ───────────────────────────────────────
if printf '%s' "$out" | grep -q "cat=timeout"; then
    pass "happy path: failure_category rendered"
else
    fail "happy path: missing failure_category" "out: $out"
fi

# ─── Test 7: full_recommendation NOT in default output ──────────────────────
# The default SELECT replaces full_recommendation with extras, so
# "full_recommendation" key must NOT appear in the human-readable output.
if printf '%s' "$out" | grep -q "full_recommendation"; then
    fail "happy path: full_recommendation leaked into default human output" "out: $out"
else
    pass "happy path: full_recommendation absent from default human output"
fi

# ─── Test 8: --full-reasoning returns untruncated text ──────────────────────
write_stub "$TMPDIR_TEST/canned_full.json"
set +e
out_full=$(run_diagnoses "--full-reasoning" 2>"$TMPDIR_TEST/err.log")
rc_full=$?
set -e
if [[ "$rc_full" == "0" ]]; then
    pass "--full-reasoning: exits 0"
else
    fail "--full-reasoning: exit 0" "rc=$rc_full err=$(cat "$TMPDIR_TEST/err.log")"
fi
# Verify the unique end-of-reasoning marker from the full canned data appears.
if printf '%s' "$out_full" | grep -q "END_OF_REASONING_MARKER"; then
    pass "--full-reasoning: untruncated reasoning rendered (END_OF_REASONING_MARKER found)"
else
    fail "--full-reasoning: untruncated reasoning not found" "out: $out_full"
fi

# ─── Test 9: --json mode emits array with full_recommendation non-null ───────
write_stub "$TMPDIR_TEST/canned_main.json"
set +e
out_json=$(run_diagnoses "--recent 5 --json" 2>"$TMPDIR_TEST/err.log")
rc_json=$?
set -e
if [[ "$rc_json" == "0" ]]; then
    pass "--json: exits 0"
else
    fail "--json: exit 0" "rc=$rc_json err=$(cat "$TMPDIR_TEST/err.log")"
fi
# Verify it parses as a JSON array with elements.
set +e
json_len=$(printf '%s' "$out_json" | jq 'length' 2>/dev/null)
set -e
if [[ -n "$json_len" ]] && (( json_len > 0 )); then
    pass "--json: emits non-empty JSON array (length=$json_len)"
else
    fail "--json: did not emit a non-empty JSON array" "out: $out_json err: $(cat "$TMPDIR_TEST/err.log")"
fi
# Verify .[0].full_recommendation is non-null (per-row fetch wired up).
set +e
fr=$(printf '%s' "$out_json" | jq '.[0].full_recommendation // "NULL_SENTINEL"' 2>/dev/null)
set -e
if [[ "$fr" != '"NULL_SENTINEL"' && -n "$fr" ]]; then
    pass "--json: .[0].full_recommendation is non-null"
else
    fail "--json: .[0].full_recommendation is null or missing" "json_out: $out_json"
fi

# ─── Test 10: argument validation — bad --recent ─────────────────────────────
write_stub "$TMPDIR_TEST/canned_main.json"
set +e
run_diagnoses "--recent abc" >"$TMPDIR_TEST/out_bad.txt" 2>"$TMPDIR_TEST/err.log"
rc_bad=$?
set -e
if [[ "$rc_bad" == "1" ]] && grep -q "positive integer" "$TMPDIR_TEST/err.log"; then
    pass "arg validation: --recent rejects non-integer value"
else
    fail "arg validation: --recent abc" "rc=$rc_bad err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 11: argument validation — bad --issue ──────────────────────────────
set +e
run_diagnoses "--issue notanumber" >"$TMPDIR_TEST/out_bad2.txt" 2>"$TMPDIR_TEST/err.log"
rc_bad2=$?
set -e
if [[ "$rc_bad2" == "1" ]] && grep -q "positive integer" "$TMPDIR_TEST/err.log"; then
    pass "arg validation: --issue rejects non-integer"
else
    fail "arg validation: --issue notanumber" "rc=$rc_bad2 err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 12: argument validation — unknown arg ──────────────────────────────
set +e
run_diagnoses "--unknown-flag" >"$TMPDIR_TEST/out_bad3.txt" 2>"$TMPDIR_TEST/err.log"
rc_bad3=$?
set -e
if [[ "$rc_bad3" == "1" ]] && grep -q "unknown arg" "$TMPDIR_TEST/err.log"; then
    pass "arg validation: unknown arg rejected"
else
    fail "arg validation: --unknown-flag" "rc=$rc_bad3 err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 13: 20-iteration stress loop ───────────────────────────────────────
# Mirrors AC #1 from #3203: 20 consecutive stubbed runs must all exit 0.
write_stub "$TMPDIR_TEST/canned_main.json"
stress_failures=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    set +e
    run_diagnoses "--recent 5" >/dev/null 2>/dev/null
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

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
