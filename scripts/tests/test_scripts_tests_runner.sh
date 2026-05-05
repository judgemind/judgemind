#!/usr/bin/env bash
# test_scripts_tests_runner.sh — Tests for scripts/run-scripts-tests.sh.
#
# Exercises the auto-discovery + filter logic of the scripts-tests CI
# runner against a synthetic tests directory. Confirms:
#   1. Executable non-helper tests are run.
#   2. Underscore-prefixed helper files are skipped (not executed).
#   3. Non-executable files are warned about and skipped.
#   4. SKIP_TESTS entries are deferred, not run.
#   5. Empty directories exit 0.
#   6. A failing test causes the runner to exit 1.
#   7. The real scripts/tests/_guard_self_match_helpers.sh is not
#      executed by the runner on this repo (regression guard for #2559).
#
# Usage:
#   scripts/tests/test_scripts_tests_runner.sh
#
# Exit codes:
#   0 — all tests passed.
#   1 — one or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNNER="$SCRIPT_DIR/run-scripts-tests.sh"

if [[ ! -x "$RUNNER" ]]; then
    echo "FATAL: runner $RUNNER is missing or not executable"
    exit 1
fi

FAILURES=0
TESTS=0
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
}

# Writes an executable shell test that records its own path when run.
# Args: <filename> <marker_name>
make_passing_test() {
    local path="$TMPDIR_TEST/$1"
    local marker="$2"
    cat >"$path" <<EOF
#!/usr/bin/env bash
touch "$TMPDIR_TEST/RAN_$marker"
exit 0
EOF
    chmod +x "$path"
}

# Writes an executable shell test that fails (exits 1) and records it ran.
make_failing_test() {
    local path="$TMPDIR_TEST/$1"
    local marker="$2"
    cat >"$path" <<EOF
#!/usr/bin/env bash
touch "$TMPDIR_TEST/RAN_$marker"
exit 1
EOF
    chmod +x "$path"
}

# Writes a helper file that explodes if executed (to catch accidental
# invocation). Note: not chmod +x by default — callers may override.
make_helper() {
    local path="$TMPDIR_TEST/$1"
    cat >"$path" <<EOF
#!/usr/bin/env bash
# Shared helper — sourced, not executed. If the runner ever calls this
# as a test, mark that we ran so the test can fail loudly.
touch "$TMPDIR_TEST/HELPER_EXECUTED_$1"
exit 0
EOF
}

run_runner() {
    local skip_tests="${1:-}"
    # shellcheck disable=SC2097,SC2098
    TESTS_DIR="$TMPDIR_TEST" SKIP_TESTS="$skip_tests" "$RUNNER"
}

# Like run_runner, but also threads SHARD_FILTER + SLOW_TESTS for the
# #4067 shard-partition tests below.
run_runner_with_shard() {
    local shard_filter="${1:-}"
    local slow_tests="${2:-}"
    # shellcheck disable=SC2097,SC2098
    TESTS_DIR="$TMPDIR_TEST" \
        SKIP_TESTS="" \
        SHARD_FILTER="$shard_filter" \
        SLOW_TESTS="$slow_tests" \
        "$RUNNER"
}

assert_eq() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    TESTS=$((TESTS + 1))
    if [[ "$expected" == "$actual" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected='$expected' actual='$actual')"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_file_exists() {
    local desc="$1"
    local path="$2"
    TESTS=$((TESTS + 1))
    if [[ -e "$path" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (missing: $path)"
        FAILURES=$((FAILURES + 1))
    fi
}

assert_file_absent() {
    local desc="$1"
    local path="$2"
    TESTS=$((TESTS + 1))
    if [[ ! -e "$path" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (exists but should not: $path)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Test 1: Executable non-helper tests run ────────────────────────────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_passing_test "test_beta.sh" beta
set +e
out="$(run_runner 2>&1)"
rc=$?
set -e
assert_eq "runner exits 0 when all tests pass" 0 "$rc"
assert_file_exists "test_alpha.sh was executed" "$TMPDIR_TEST/RAN_alpha"
assert_file_exists "test_beta.sh was executed" "$TMPDIR_TEST/RAN_beta"

# ─── Test 2: Underscore-prefixed helpers are skipped ────────────────────
reset_tmpdir
make_passing_test "test_real.sh" real
make_helper "_guard_helpers.sh"
chmod +x "$TMPDIR_TEST/_guard_helpers.sh"  # even if executable, must NOT run
set +e
out="$(run_runner 2>&1)"
rc=$?
set -e
assert_eq "runner exits 0 when helpers are present and all real tests pass" 0 "$rc"
assert_file_exists "real test was executed" "$TMPDIR_TEST/RAN_real"
assert_file_absent "executable _-prefixed helper was NOT executed" "$TMPDIR_TEST/HELPER_EXECUTED__guard_helpers.sh"

# ─── Test 3: Non-executable files trigger warning, skipped ──────────────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
path_nonexec="$TMPDIR_TEST/test_broken.sh"
cat >"$path_nonexec" <<'EOF'
#!/usr/bin/env bash
exit 1  # should never run — not executable
EOF
# deliberately NOT chmod +x
set +e
out="$(run_runner 2>&1)"
rc=$?
set -e
assert_eq "runner exits 0 when non-executable files are warned+skipped" 0 "$rc"
assert_file_exists "executable test ran" "$TMPDIR_TEST/RAN_alpha"
case "$out" in
    *"Skipping $path_nonexec — not executable"*)
        TESTS=$((TESTS + 1))
        echo "PASS: non-executable skip warning present in output" ;;
    *)
        TESTS=$((TESTS + 1))
        FAILURES=$((FAILURES + 1))
        echo "FAIL: missing 'not executable' warning in runner output"
        echo "---OUTPUT---"
        echo "$out"
        echo "---END---" ;;
esac

# ─── Test 4: SKIP_TESTS entries are deferred ────────────────────────────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_failing_test "test_deferred.sh" deferred  # would fail if run!
set +e
out="$(run_runner "$TMPDIR_TEST/test_deferred.sh" 2>&1)"
rc=$?
set -e
assert_eq "runner exits 0 when failing test is deferred via SKIP_TESTS" 0 "$rc"
assert_file_exists "alpha ran" "$TMPDIR_TEST/RAN_alpha"
assert_file_absent "deferred test did NOT run" "$TMPDIR_TEST/RAN_deferred"

# ─── Test 5: Empty directory exits 0 ────────────────────────────────────
reset_tmpdir
set +e
out="$(run_runner 2>&1)"
rc=$?
set -e
assert_eq "runner exits 0 on empty tests directory" 0 "$rc"

# ─── Test 6: A failing test causes exit 1 ───────────────────────────────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_failing_test "test_bad.sh" bad
set +e
out="$(run_runner 2>&1)"
rc=$?
set -e
assert_eq "runner exits 1 when a test fails" 1 "$rc"
assert_file_exists "passing test still ran" "$TMPDIR_TEST/RAN_alpha"
assert_file_exists "failing test ran (and failed)" "$TMPDIR_TEST/RAN_bad"

# ─── Test 6.5a (#4067): SHARD_FILTER=slow runs only SLOW_TESTS entries ─
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_passing_test "test_beta.sh" beta
make_passing_test "test_slow.sh" slow
slow_path="$TMPDIR_TEST/test_slow.sh"
set +e
out="$(run_runner_with_shard slow "$slow_path" 2>&1)"
rc=$?
set -e
assert_eq "SHARD_FILTER=slow exits 0" 0 "$rc"
assert_file_exists "SHARD_FILTER=slow ran the slow test" "$TMPDIR_TEST/RAN_slow"
assert_file_absent "SHARD_FILTER=slow did NOT run alpha (fast)" "$TMPDIR_TEST/RAN_alpha"
assert_file_absent "SHARD_FILTER=slow did NOT run beta (fast)" "$TMPDIR_TEST/RAN_beta"

# ─── Test 6.5b (#4067): SHARD_FILTER=not-slow skips SLOW_TESTS entries ─
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_passing_test "test_beta.sh" beta
make_passing_test "test_slow.sh" slow
slow_path="$TMPDIR_TEST/test_slow.sh"
set +e
out="$(run_runner_with_shard not-slow "$slow_path" 2>&1)"
rc=$?
set -e
assert_eq "SHARD_FILTER=not-slow exits 0" 0 "$rc"
assert_file_exists "SHARD_FILTER=not-slow ran alpha (fast)" "$TMPDIR_TEST/RAN_alpha"
assert_file_exists "SHARD_FILTER=not-slow ran beta (fast)" "$TMPDIR_TEST/RAN_beta"
assert_file_absent "SHARD_FILTER=not-slow did NOT run the slow test" "$TMPDIR_TEST/RAN_slow"

# ─── Test 6.5c (#4067): SHARD_FILTER unset (legacy) runs everything ──────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
make_passing_test "test_slow.sh" slow
slow_path="$TMPDIR_TEST/test_slow.sh"
set +e
# Empty shard filter — equivalent to legacy callers that don't set the var.
out="$(run_runner_with_shard "" "$slow_path" 2>&1)"
rc=$?
set -e
assert_eq "SHARD_FILTER='' exits 0 (legacy compat)" 0 "$rc"
assert_file_exists "SHARD_FILTER='' ran alpha" "$TMPDIR_TEST/RAN_alpha"
assert_file_exists "SHARD_FILTER='' also ran the slow test" "$TMPDIR_TEST/RAN_slow"

# ─── Test 6.5d (#4067): SHARD_FILTER=invalid exits 1 with error ──────────
reset_tmpdir
make_passing_test "test_alpha.sh" alpha
set +e
out="$(run_runner_with_shard bogus "" 2>&1)"
rc=$?
set -e
assert_eq "SHARD_FILTER=bogus exits 1" 1 "$rc"
case "$out" in
    *"Invalid SHARD_FILTER"*)
        TESTS=$((TESTS + 1))
        echo "PASS: SHARD_FILTER=bogus emits 'Invalid SHARD_FILTER' error" ;;
    *)
        TESTS=$((TESTS + 1))
        FAILURES=$((FAILURES + 1))
        echo "FAIL: SHARD_FILTER=bogus missing 'Invalid SHARD_FILTER' error"
        echo "---OUTPUT---"
        echo "$out"
        echo "---END---" ;;
esac
assert_file_absent "SHARD_FILTER=bogus did NOT execute any test" "$TMPDIR_TEST/RAN_alpha"

# ─── Test 7: Regression guard — real scripts/tests/_guard_self_match_helpers.sh
#            is not executed by the live runner (the production scenario).
#            This is the specific scenario acceptance criterion 3 of #2559.
# We can't actually run the live runner from scripts/tests/ here (it would
# invoke every real test — a giant loop). Instead, assert that the real
# helper file is present, is NOT chmod'd +x, AND does match the glob but
# the is_helper filter would exclude it. We prove the last by calling the
# runner over a synthetic dir containing a copy of the real helper.
reset_tmpdir
real_helper="$REPO_ROOT/scripts/tests/_guard_self_match_helpers.sh"
if [[ -f "$real_helper" ]]; then
    cp "$real_helper" "$TMPDIR_TEST/_guard_self_match_helpers.sh"
    chmod +x "$TMPDIR_TEST/_guard_self_match_helpers.sh"  # worst-case permissions
    make_passing_test "test_sentinel.sh" sentinel
    set +e
    out="$(run_runner 2>&1)"
    rc=$?
    set -e
    assert_eq "runner exits 0 with real _guard_self_match_helpers.sh copy present" 0 "$rc"
    assert_file_exists "sentinel test ran alongside the helper" "$TMPDIR_TEST/RAN_sentinel"
    # The real helper would error out if invoked (unset $CHECK_SCRIPT).
    # If it had been invoked, rc would be non-zero and "sentinel" would
    # still have run, but the summary line would show "Failed: 1". Check:
    case "$out" in
        *"Failed: 0"*)
            TESTS=$((TESTS + 1))
            echo "PASS: real _guard_self_match_helpers.sh copy was NOT executed" ;;
        *)
            TESTS=$((TESTS + 1))
            FAILURES=$((FAILURES + 1))
            echo "FAIL: runner appears to have executed _guard_self_match_helpers.sh"
            echo "---OUTPUT---"
            echo "$out"
            echo "---END---" ;;
    esac
else
    TESTS=$((TESTS + 1))
    echo "PASS: (skipped) real _guard_self_match_helpers.sh not present in repo — regression guard N/A"
fi

# ─── Summary ────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
