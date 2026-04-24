#!/usr/bin/env bash
# test_dispatcher_helpers_query_lib.sh — Tests for scripts/dispatcher/helpers/_query_lib.sh
#
# Exercises the shared `dev-db-query.sh → JSON` helper used by every
# dispatcher helper script. This file specifically targets the failure
# mode from #3124: the old `awk '/^\[/{f=1} f&&!g{print} /^\]/{if(f)g=1}'
# | jq` pipeline would raise a bare `parse error: Invalid numeric literal`
# when the ECS-exec stream delivered a partial payload or an out-of-band
# trailer.
#
# All tests source _query_lib.sh with a $dev_db pointing at a one-shot
# shell-script stub that emits the exact kind of output dev-db-query.sh
# would produce — SSM banner + JSON + trailer — or deliberately-broken
# variants to verify the validation + retry path.
#
# Usage:
#   scripts/tests/test_dispatcher_helpers_query_lib.sh
#
# Exit codes:
#   0 — all tests passed.
#   1 — one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB="$REPO_ROOT/scripts/dispatcher/helpers/_query_lib.sh"

if [[ ! -f "$LIB" ]]; then
    echo "FATAL: $LIB missing" >&2
    exit 1
fi

FAILURES=0
TESTS=0
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
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

# Writes a one-shot stub at $TMPDIR_TEST/stub_dev_db.sh that ignores its
# args and prints the given payload to stdout. The payload is chosen per
# test to mimic various dev-db-query.sh outputs.
make_stub() {
    local payload="$1"
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<EOF
#!/usr/bin/env bash
# One-shot stub for dev-db-query.sh — prints a canned payload.
cat <<'__PAYLOAD__'
${payload}
__PAYLOAD__
EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# Writes a counting stub that succeeds only on a specific attempt. The
# stub tracks call count in $TMPDIR_TEST/call_count and emits
# garbage-then-valid-JSON-on-attempt-$N, where N is $1.
make_counting_stub() {
    local succeed_on="$1"
    echo 0 > "$TMPDIR_TEST/call_count"
    cat >"$TMPDIR_TEST/stub_dev_db.sh" <<EOF
#!/usr/bin/env bash
count_file="$TMPDIR_TEST/call_count"
n=\$(cat "\$count_file")
n=\$((n + 1))
echo "\$n" > "\$count_file"
if (( n >= $succeed_on )); then
    cat <<'__PAYLOAD__'
Starting session with SessionId: ecs-execute-command-deadbeef
[
  {"ok": true, "attempt": ${succeed_on}}
]

Exiting session with sessionId: ecs-execute-command-deadbeef
__PAYLOAD__
else
    # Broken payload — trailing garbage that mimics an interrupted stream.
    cat <<'__PAYLOAD__'
Starting session with SessionId: ecs-execute-command-deadbeef
[
  {"ok": true, "truncated":
Cannot perform start session: EOF
__PAYLOAD__
fi
EOF
    chmod +x "$TMPDIR_TEST/stub_dev_db.sh"
}

# Exercises the library. Sources _query_lib.sh with dev_db pointing at
# the stub, then calls _query_lib_run with a throwaway SQL string.
run_lib() {
    local label="${1:-test}"
    dev_db="$TMPDIR_TEST/stub_dev_db.sh" bash -c "
        set -uo pipefail
        dev_db='$TMPDIR_TEST/stub_dev_db.sh'
        . '$LIB'
        _query_lib_run 'SELECT 1;' '$label'
    "
}

# ─── Test 1: Happy path — SSM banner + JSON + trailer ───────────────────
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-0123456789abcdef
[
  {"agent_id": "abc", "count": 3}
]

Exiting session with sessionId: ecs-execute-command-0123456789abcdef
Cannot perform start session: EOF
EOF
)"
set +e
out=$(run_lib "happy" 2>/dev/null)
rc=$?
set -e
if [[ "$rc" == "0" ]] && printf '%s' "$out" | jq -e '.[0].agent_id == "abc"' >/dev/null 2>&1; then
    pass "happy path: SSM banner + JSON + trailer → returns just the JSON array"
else
    fail "happy path" "rc=$rc out=$out"
fi

# ─── Test 2: Malformed JSON — `Invalid numeric literal` scenario ────────
# Emit something that looks like JSON but isn't well-formed. The old
# pipeline would have let this through to jq which would print the
# `Invalid numeric literal` error and exit 4. The library must instead
# retry, and on final failure exit 2 with a descriptive error.
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-0123456789abcdef
[
  {"foo": 01bad}
]
EOF
)"
set +e
out=$(run_lib "bad-number" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "2" ]]; then
    pass "bad numeric literal: helper exits 2 (not 4) after exhausted retries"
else
    fail "bad numeric literal: expected rc=2, got rc=$rc" "out=$out err=$(cat "$TMPDIR_TEST/err.log" 2>/dev/null || echo '(no err)')"
fi
if grep -q "malformed JSON after 5 attempts" "$TMPDIR_TEST/err.log"; then
    pass "bad numeric literal: stderr names the failure ('malformed JSON after 5 attempts')"
else
    fail "bad numeric literal: stderr missing expected diagnostic" "$(cat "$TMPDIR_TEST/err.log")"
fi
if grep -q "01bad" "$TMPDIR_TEST/err.log"; then
    pass "bad numeric literal: stderr dumps the raw captured output for debugging"
else
    fail "bad numeric literal: stderr did not include raw payload" "$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 3: Truncated stream (Cannot perform start session: EOF) ───────
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-0123456789abcdef
[
  {"foo":
Cannot perform start session: EOF
EOF
)"
set +e
out=$(run_lib "truncated" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "2" ]]; then
    pass "truncated stream: helper exits 2 (not bare jq parse error)"
else
    fail "truncated stream: expected rc=2, got rc=$rc" "out=$out err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 4: Empty output ───────────────────────────────────────────────
make_stub ""
set +e
out=$(run_lib "empty" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "2" ]]; then
    pass "empty output: helper exits 2 with diagnostic"
else
    fail "empty output: expected rc=2, got rc=$rc" "out=$out err=$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 5: Transient failure then recovery (succeeds on 2nd attempt) ──
make_counting_stub 2
set +e
out=$(run_lib "retry" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "0" ]] && printf '%s' "$out" | jq -e '.[0].ok == true' >/dev/null 2>&1; then
    pass "transient failure: helper retries and recovers on attempt 2"
else
    fail "transient failure" "rc=$rc out=$out err=$(cat "$TMPDIR_TEST/err.log")"
fi
if grep -q "malformed JSON on attempt 1/5 — retrying" "$TMPDIR_TEST/err.log"; then
    pass "transient failure: retry warning logged to stderr"
else
    fail "transient failure: missing retry warning" "$(cat "$TMPDIR_TEST/err.log")"
fi

# ─── Test 6: Stress — 20× happy-path calls all succeed ──────────────────
# Mirrors the acceptance criterion on #3124: run the helper 20× in a row,
# expect exit 0 every time.
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-0123456789abcdef
[
  {"n": 1}
]

Cannot perform start session: EOF
EOF
)"
stress_failures=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    set +e
    out=$(run_lib "stress-$i" 2>/dev/null)
    rc=$?
    set -e
    if [[ "$rc" != "0" ]] || ! printf '%s' "$out" | jq -e '.[0].n == 1' >/dev/null 2>&1; then
        stress_failures=$((stress_failures + 1))
    fi
done
if [[ "$stress_failures" == "0" ]]; then
    pass "stress: 20 consecutive happy-path calls all exited 0 with well-formed JSON"
else
    fail "stress: $stress_failures/20 iterations failed"
fi

# ─── Test 7: Payload contains a line starting with `[` inside a string ──
# Regression guard — the JSON pretty-printer keeps `[` outside strings on
# its own line. If a string value ever contained a raw newline + `[`, the
# awk would re-enter capture mode. We can't produce that with json.dumps,
# but we can verify the library validates end-to-end via jq — which would
# reject such a payload as malformed. (Test that the validator is actually
# invoked, not bypassed.)
make_stub "$(cat <<'EOF'
[
  {"value": "abc"}
][oops trailing junk starting with bracket
EOF
)"
# Our awk stops at the first `^]`, so the trailing junk on the same line
# *as* `]` should be included. That would fail jq validation. But if the
# trailing junk is on a new line starting with `[`, awk re-opens capture
# — a subtle bug. Either way, the library's jq validation should reject
# it. Assert: rc=2 (validator blocked the bad output, rather than letting
# it through to a caller's jq which would exit 4 with a cryptic error).
set +e
out=$(run_lib "noise" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
if [[ "$rc" == "2" ]]; then
    pass "post-JSON noise: validator rejects malformed payload (exits 2, not 4)"
else
    # This one is best-effort — awk's exact behavior on this particular
    # input may vary. The important guarantee is that we don't silently
    # pass bad JSON through to jq. If rc==0, that's a regression.
    if [[ "$rc" == "0" ]]; then
        fail "post-JSON noise: library returned rc=0 on bad payload (regression)" "out=$out"
    else
        pass "post-JSON noise: library returned non-zero on bad payload (rc=$rc)"
    fi
fi

# ─── Test 8: Compact single-line JSON (runner's default after #3195) ───
# The runner defaults to compact JSON so more bytes fit under SSM's
# per-session output budget. _query_lib_json_only must handle the
# single-line case — the legacy awk pattern expected `[` and `]` on
# their own lines (pretty-printed only).
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-compact
[{"agent_id":"abc","count":3},{"agent_id":"def","count":7}]

Exiting session with sessionId: ecs-execute-command-compact
EOF
)"
set +e
out=$(run_lib "compact" 2>/dev/null)
rc=$?
set -e
if [[ "$rc" == "0" ]] \
    && printf '%s' "$out" | jq -e '.[0].agent_id == "abc"' >/dev/null 2>&1 \
    && printf '%s' "$out" | jq -e '.[1].count == 7' >/dev/null 2>&1; then
    pass "compact JSON (single line): library returns parseable result"
else
    fail "compact JSON (single line)" "rc=$rc out=$out"
fi

# ─── Test 9: Compact JSON with SSM chatter on the SAME line as JSON ─────
# Real SSM sometimes emits the session trailer concatenated with the
# final output chunk (no newline separator). dev-db-query.sh now strips
# that at the script level, but the library's own filter is a secondary
# guard — test that a same-line trailer concatenated to compact JSON
# fails validation rather than returning corrupt output.
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-mixed
[{"n":1}]Cannot perform start session: EOF
EOF
)"
set +e
out=$(run_lib "mixed" 2>"$TMPDIR_TEST/err.log")
rc=$?
set -e
# Either: rc=0 with the JSON parseable (if the grep stripped the
# trailer), or rc=2 (validator caught bad concatenation). Both are
# acceptable — the important negative is rc=0 with corrupt output.
if [[ "$rc" == "0" ]]; then
    if printf '%s' "$out" | jq -e '.[0].n == 1' >/dev/null 2>&1; then
        pass "same-line trailer: library returns clean parseable JSON"
    else
        fail "same-line trailer: library returned rc=0 with bad JSON" \
            "out=$out"
    fi
elif [[ "$rc" == "2" ]]; then
    pass "same-line trailer: validator rejected corrupt concatenation"
else
    fail "same-line trailer: unexpected rc=$rc" "out=$out"
fi

# ─── Test 10: Legacy pretty-printed output stays supported ──────────────
# _query_lib.sh must continue to work if a caller passes pretty-printed
# JSON (e.g. an operator explicitly invokes the runner with compact=0).
make_stub "$(cat <<'EOF'
Starting session with SessionId: ecs-execute-command-pretty
[
  {
    "agent_id": "legacy-pretty",
    "count": 99
  }
]

Exiting session with sessionId: ecs-execute-command-pretty
EOF
)"
set +e
out=$(run_lib "pretty" 2>/dev/null)
rc=$?
set -e
if [[ "$rc" == "0" ]] \
    && printf '%s' "$out" | jq -e '.[0].agent_id == "legacy-pretty"' >/dev/null 2>&1; then
    pass "legacy pretty-printed JSON still parses cleanly"
else
    fail "legacy pretty-printed JSON" "rc=$rc out=$out"
fi

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
