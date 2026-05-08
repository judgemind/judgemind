#!/usr/bin/env bash
# test_progress_sh.sh — Regression test for issue #3884.
#
# Exercises scripts/dispatcher/progress.sh — the best-effort milestone
# helper invoked by /task at each natural step (planning, ralph, summary,
# push_and_pr, …) per dispatcher-v3 spec §4.3.
#
# What this verifies (acceptance criteria from #3884):
#
#   AC1 — Helper exists and is executable.
#   AC2 — Helper exits 0 on missing args, missing DATABASE_URL, missing
#         psql, and DB error. (Best-effort contract: never block /task.)
#   AC3 — Helper invokes psql with the expected `-v` flags so the SQL
#         body's `:'name'` placeholders interpolate to properly-quoted
#         literals. We stub `psql` and assert on the captured argv.
#   AC4 — SQL-injection safety: when invoked with a milestone string
#         containing `'; DROP TABLE …; --`, the value is passed via the
#         `-v` flag verbatim. The shell does NOT splice it into the SQL
#         body. (psql then quotes-and-escapes during `:'name'` expansion
#         — verified independently against a real Postgres before this
#         script was committed; see issue #3884 for the manual probe.)
#
# Stubbing strategy:
#
#   The test prepends a $TMP/bin directory to $PATH containing a fake
#   `psql` shell script. The fake records its argv to $PSQL_LOG, reads
#   stdin to $PSQL_STDIN, and exits with $PSQL_EXIT_CODE (default 0).
#   This lets us assert on the exact CLI invocation without a real DB.
#
# Usage:
#   scripts/dispatcher/tests/test_progress_sh.sh
#
# Exit codes:
#   0 — all assertions passed.
#   1 — one or more assertions failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$SCRIPT_DIR/dispatcher/progress.sh"

FAILURES=0
TESTS=0

# Cleanup of temp dirs + files via the shared helper (see #4343).
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

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

# Build a fresh tmp dir + stub `psql` for one test case. The stub:
#   * Captures argv to $TMP/psql.argv (one line per arg).
#   * Captures stdin to $TMP/psql.stdin.
#   * Exits with the value in $TMP/psql.exit_code (defaults to 0).
make_psql_stub() {
    local tmp
    tmp=$(mktemp -d -t progress_test.XXXXXX)
    register_temp_dir "$tmp"
    mkdir -p "$tmp/bin"
    cat > "$tmp/bin/psql" <<STUB
#!/usr/bin/env bash
exit_code=\$(cat "$tmp/psql.exit_code" 2>/dev/null || echo 0)
# Record argv one-per-line for easy grep assertions.
: > "$tmp/psql.argv"
for a in "\$@"; do
    printf '%s\n' "\$a" >> "$tmp/psql.argv"
done
# Drain stdin so the SQL body is captured.
cat > "$tmp/psql.stdin"
if [[ "\$exit_code" -ne 0 ]]; then
    echo "psql: simulated failure" >&2
fi
exit "\$exit_code"
STUB
    chmod +x "$tmp/bin/psql"
    echo "$tmp"
}

# ── Precondition: the helper exists and is executable ────────────────────────

if [[ ! -f "$HELPER" ]]; then
    fail "helper script exists" "expected $HELPER to be present"
    echo
    echo "Tests: $TESTS, Failures: $FAILURES"
    exit 1
fi
pass "helper script $HELPER exists"

if [[ ! -x "$HELPER" ]]; then
    fail "helper is executable" "expected -rwxr-xr-x at $HELPER"
else
    pass "helper is executable"
fi

# ── AC2: missing args → exit 0 ───────────────────────────────────────────────

run_no_args() {
    local err rc
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    "$HELPER" 2>"$err"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "no args → exit 0"
    else
        fail "no args → exit 0" "got exit code $rc"
    fi
    if grep -q "usage:" "$err"; then
        pass "no args prints usage to stderr"
    else
        fail "no args prints usage to stderr" "stderr was: $(cat "$err")"
    fi
    rm -f "$err"
}
run_no_args

run_one_arg() {
    local err rc
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    "$HELPER" only-one-arg 2>"$err"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "one arg → exit 0"
    else
        fail "one arg → exit 0" "got exit code $rc"
    fi
    if grep -q "usage:" "$err"; then
        pass "one arg prints usage to stderr"
    else
        fail "one arg prints usage to stderr" "stderr was: $(cat "$err")"
    fi
    rm -f "$err"
}
run_one_arg

# Empty-string second arg should also be rejected (treated as missing).
run_empty_milestone() {
    local err rc
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    "$HELPER" agent-1 "" 2>"$err"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "empty milestone → exit 0"
    else
        fail "empty milestone → exit 0" "got exit code $rc"
    fi
    if grep -q "usage:" "$err"; then
        pass "empty milestone prints usage"
    else
        fail "empty milestone prints usage" "stderr was: $(cat "$err")"
    fi
    rm -f "$err"
}
run_empty_milestone

# ── AC2: missing DATABASE_URL → exit 0 ───────────────────────────────────────

run_no_db_url() {
    local err rc
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    # `env -u` removes DATABASE_URL even if the surrounding test env set one.
    env -u DATABASE_URL "$HELPER" agent-1 ralph "iter 3" 2>"$err"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "missing DATABASE_URL → exit 0"
    else
        fail "missing DATABASE_URL → exit 0" "got exit code $rc"
    fi
    if grep -q "DATABASE_URL not set" "$err"; then
        pass "missing DATABASE_URL prints note to stderr"
    else
        fail "missing DATABASE_URL prints note" "stderr was: $(cat "$err")"
    fi
    rm -f "$err"
}
run_no_db_url

# ── AC2: psql binary not on PATH → exit 0 ────────────────────────────────────

run_no_psql() {
    local err rc bin_only
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    bin_only=$(mktemp -d -t progress_nopath.XXXXXX)
    register_temp_dir "$bin_only"
    # Symlink only the binaries the script (and its shebang) need:
    # `env` and `bash`. This guarantees `psql` is unfindable while the
    # script's `#!/usr/bin/env bash` shebang still resolves. Falls back
    # to /bin if /usr/bin is absent on some image.
    for b in env bash sh head mktemp rm basename grep; do
        if [[ -x "/usr/bin/$b" ]]; then
            ln -sf "/usr/bin/$b" "$bin_only/$b"
        elif [[ -x "/bin/$b" ]]; then
            ln -sf "/bin/$b" "$bin_only/$b"
        fi
    done
    PATH="$bin_only" DATABASE_URL="postgresql://x@y:5/z" \
        "$HELPER" agent-1 ralph 2>"$err"
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "psql not on PATH → exit 0"
    else
        fail "psql not on PATH → exit 0" "got exit code $rc; stderr: $(cat "$err")"
    fi
    if grep -q "psql not on PATH" "$err"; then
        pass "psql-missing prints note to stderr"
    else
        fail "psql-missing prints note" "stderr was: $(cat "$err")"
    fi
}
run_no_psql

# ── AC2 & AC3: stubbed psql success → exit 0 + correct argv ──────────────────

run_stubbed_success() {
    local stub_dir rc err
    stub_dir=$(make_psql_stub)
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    echo 0 > "$stub_dir/psql.exit_code"

    PATH="$stub_dir/bin:$PATH" DATABASE_URL="postgresql://stub@localhost:0/x" \
        "$HELPER" "agent-uuid-1" "ralph" "iter 3" 2>"$err"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        pass "stubbed success → exit 0"
    else
        fail "stubbed success → exit 0" "got exit code $rc; stderr: $(cat "$err")"
    fi

    if [[ ! -f "$stub_dir/psql.argv" ]]; then
        fail "psql was invoked" "no argv log at $stub_dir/psql.argv"
    else
        pass "psql was invoked"
    fi

    # Each `-v name=value` becomes two argv entries: "-v" then "name=value".
    if grep -qFx "agent_id=agent-uuid-1" "$stub_dir/psql.argv"; then
        pass "psql received -v agent_id=agent-uuid-1"
    else
        fail "psql received -v agent_id=…" "argv was: $(cat "$stub_dir/psql.argv")"
    fi
    if grep -qFx "milestone=ralph" "$stub_dir/psql.argv"; then
        pass "psql received -v milestone=ralph"
    else
        fail "psql received -v milestone=…" "argv was: $(cat "$stub_dir/psql.argv")"
    fi
    if grep -qFx "detail=iter 3" "$stub_dir/psql.argv"; then
        pass "psql received -v detail=iter 3"
    else
        fail "psql received -v detail=…" "argv was: $(cat "$stub_dir/psql.argv")"
    fi
    # Read SQL from stdin: argv must contain `-f` immediately followed
    # by `-`. Use awk to scan for that adjacency.
    if awk 'BEGIN{f=0} /^-f$/{f=1; next} f==1 && /^-$/{print "ok"; exit 0} {f=0}' \
            "$stub_dir/psql.argv" | grep -q ok; then
        pass "psql received -f - (stdin SQL)"
    else
        fail "psql received -f -" "argv was: $(cat "$stub_dir/psql.argv")"
    fi

    # The DATABASE_URL must be passed positionally (one of the argv entries).
    if grep -qFx "postgresql://stub@localhost:0/x" "$stub_dir/psql.argv"; then
        pass "psql received DATABASE_URL positionally"
    else
        fail "psql received DATABASE_URL" "argv was: $(cat "$stub_dir/psql.argv")"
    fi
    rm -f "$err"
}
run_stubbed_success

# ── AC2: stubbed psql DB error → exit 0 (best-effort) ────────────────────────

run_stubbed_db_error() {
    local stub_dir rc err
    stub_dir=$(make_psql_stub)
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    echo 2 > "$stub_dir/psql.exit_code"

    PATH="$stub_dir/bin:$PATH" DATABASE_URL="postgresql://stub@localhost:0/x" \
        "$HELPER" "agent-uuid-1" "ralph" "iter 3" 2>"$err"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        pass "DB error (psql exit 2) → script exit 0"
    else
        fail "DB error → script exit 0" "got exit code $rc; stderr: $(cat "$err")"
    fi

    if grep -q "psql failed (swallowed)" "$err"; then
        pass "DB error logs swallowed-failure note to stderr"
    else
        fail "DB error logs swallowed-failure note" "stderr was: $(cat "$err")"
    fi
    rm -f "$err"
}
run_stubbed_db_error

# ── AC3 & AC4: SQL injection input is passed via -v, not spliced into SQL ─────
#
# The malicious milestone string `'; DROP TABLE dispatcher.agents; --` must
# appear verbatim in the argv as `milestone=…` AND must NOT appear inside
# the SQL body sent on stdin. (The SQL body uses `:'milestone'` — a psql
# substitution token — not the literal value.)

run_injection_safe() {
    local stub_dir rc err evil
    stub_dir=$(make_psql_stub)
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    echo 0 > "$stub_dir/psql.exit_code"

    evil="'; DROP TABLE dispatcher.agents; --"

    PATH="$stub_dir/bin:$PATH" DATABASE_URL="postgresql://stub@localhost:0/x" \
        "$HELPER" "agent-uuid-1" "$evil" "still here" 2>"$err"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        pass "injection input → exit 0"
    else
        fail "injection input → exit 0" "got exit code $rc; stderr: $(cat "$err")"
    fi

    # The full payload (after `milestone=`) must appear in argv unaltered.
    if grep -qFx "milestone=$evil" "$stub_dir/psql.argv"; then
        pass "injection payload passed verbatim via -v milestone="
    else
        fail "injection payload passed verbatim via -v milestone=" \
             "argv was: $(cat "$stub_dir/psql.argv")"
    fi

    # The SQL body sent on stdin must NOT contain a literal `DROP TABLE`
    # — the helper must use the `:'milestone'` placeholder token instead.
    if grep -q "DROP TABLE" "$stub_dir/psql.stdin"; then
        fail "SQL body free of injected DROP TABLE" \
             "stdin SQL was: $(cat "$stub_dir/psql.stdin")"
    else
        pass "SQL body free of injected DROP TABLE"
    fi

    # The SQL body MUST use the :'milestone' placeholder.
    if grep -q ":'milestone'" "$stub_dir/psql.stdin"; then
        pass "SQL body uses :'milestone' placeholder"
    else
        fail "SQL body uses :'milestone' placeholder" \
             "stdin SQL was: $(cat "$stub_dir/psql.stdin")"
    fi
    if grep -q ":'agent_id'" "$stub_dir/psql.stdin"; then
        pass "SQL body uses :'agent_id' placeholder"
    else
        fail "SQL body uses :'agent_id' placeholder" \
             "stdin SQL was: $(cat "$stub_dir/psql.stdin")"
    fi
    if grep -q ":'detail'" "$stub_dir/psql.stdin"; then
        pass "SQL body uses :'detail' placeholder"
    else
        fail "SQL body uses :'detail' placeholder" \
             "stdin SQL was: $(cat "$stub_dir/psql.stdin")"
    fi

    rm -f "$err"
}
run_injection_safe

# ── AC2: detail is optional ──────────────────────────────────────────────────

run_optional_detail() {
    local stub_dir rc err
    stub_dir=$(make_psql_stub)
    err=$(mktemp -t progress_test_err.XXXXXX); register_temp_file "$err"
    echo 0 > "$stub_dir/psql.exit_code"

    PATH="$stub_dir/bin:$PATH" DATABASE_URL="postgresql://stub@localhost:0/x" \
        "$HELPER" "agent-uuid-1" "ralph" 2>"$err"
    rc=$?

    if [[ $rc -eq 0 ]]; then
        pass "detail omitted → exit 0"
    else
        fail "detail omitted → exit 0" "got exit code $rc; stderr: $(cat "$err")"
    fi

    # detail should be an empty string in argv.
    if grep -qFx "detail=" "$stub_dir/psql.argv"; then
        pass "detail omitted → -v detail= (empty value)"
    else
        fail "detail omitted → -v detail=" \
             "argv was: $(cat "$stub_dir/psql.argv")"
    fi
    rm -f "$err"
}
run_optional_detail

# ── Summary ──────────────────────────────────────────────────────────────────

echo
echo "Tests: $TESTS, Failures: $FAILURES"

if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
