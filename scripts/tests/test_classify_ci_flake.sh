#!/usr/bin/env bash
# test_classify_ci_flake.sh — Unit tests for scripts/classify-ci-flake.sh.
#
# Exercises every entry in the FLAKE_PATTERNS table plus a real-failure
# control case, the file-input path, and the no-input usage error.
#
# Usage:
#   scripts/tests/test_classify_ci_flake.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/classify-ci-flake.sh"

if [ ! -x "$SCRIPT_UNDER_TEST" ]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable." >&2
    exit 1
fi

FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [ -n "${2:-}" ]; then
        echo "  $2"
    fi
}

# Run the classifier with a stdin string, assert exact stdout.
# Usage: assert_stdin_yields <name> <stdin> <expected>
assert_stdin_yields() {
    local name="$1" stdin="$2" expected="$3" actual
    actual=$(printf '%s' "$stdin" | "$SCRIPT_UNDER_TEST" 2>/dev/null || true)
    if [ "$actual" = "$expected" ]; then
        pass "$name"
    else
        fail "$name" "expected='$expected' got='$actual'"
    fi
}

# ── Tests ──────────────────────────────────────────────────────────────────

# postgres-startup: the canonical schema-drift-check flake (#4148 motivation).
test_postgres_startup_match() {
    assert_stdin_yields "postgres_startup_match" \
        "some setup output
ERROR: postgres failed to start within 30 seconds
more output" \
        "flake/postgres-startup"
}

# postgres-startup: regex tolerates a different timeout value.
test_postgres_startup_arbitrary_seconds() {
    assert_stdin_yields "postgres_startup_arbitrary_seconds" \
        "ERROR: postgres failed to start within 60 seconds" \
        "flake/postgres-startup"
}

# docker-daemon: Docker socket race on GitHub-hosted runners.
test_docker_daemon_match() {
    assert_stdin_yields "docker_daemon_match" \
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" \
        "flake/docker-daemon"
}

# dns-resolution: transient DNS hiccup (apt-get / npm / git clone).
test_dns_resolution_match() {
    assert_stdin_yields "dns_resolution_match" \
        "fatal: unable to access 'https://github.com/judgemind/judgemind/': Could not resolve host: github.com" \
        "flake/dns-resolution"
}

# github-network: connectivity loss to github.com mid-job.
test_github_network_match() {
    assert_stdin_yields "github_network_match" \
        "curl: (7) Failed to connect to github.com port 443: Connection refused" \
        "flake/github-network"
}

# Real failure: AssertionError from a unit test must classify as real.
# This is the regression test for AC#1 — the classifier must NOT mask real
# failures, even ones that mention `error` or `failed`.
test_real_unit_test_failure() {
    assert_stdin_yields "real_unit_test_failure" \
        "test_foo (TestBar) ... FAIL
AssertionError: expected 1 got 2
ran 17 tests in 0.234s
FAILED (failures=1)" \
        "real"
}

# Real failure: a generic `error` line that is not in the flake table.
test_real_generic_error() {
    assert_stdin_yields "real_generic_error" \
        "ERROR: ruff found 3 violations" \
        "real"
}

# First-match-wins: when multiple flake patterns appear, the first listed in
# the table wins. The script processes the table in order and exits on the
# first match. We confirm postgres-startup wins over docker-daemon.
test_first_match_wins() {
    assert_stdin_yields "first_match_wins" \
        "ERROR: postgres failed to start within 30 seconds
Cannot connect to the Docker daemon" \
        "flake/postgres-startup"
}

# File-input path: the classifier accepts a file path as positional arg.
test_file_input() {
    local tmp
    tmp=$(mktemp)
    # shellcheck disable=SC2064  # we want $tmp expanded now
    trap "rm -f $tmp" RETURN
    printf 'ERROR: postgres failed to start within 30 seconds\n' > "$tmp"

    local actual
    actual=$("$SCRIPT_UNDER_TEST" "$tmp" 2>/dev/null || true)
    if [ "$actual" = "flake/postgres-startup" ]; then
        pass "file_input: classifies file content"
    else
        fail "file_input: classifies file content" "got='$actual'"
    fi
}

# Missing-file error: nonexistent file must exit non-zero.
test_missing_file_errors() {
    local exit_code
    exit_code=0
    "$SCRIPT_UNDER_TEST" /nonexistent/path/zzz 2>/dev/null || exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        pass "missing_file_errors: exits non-zero on missing input file"
    else
        fail "missing_file_errors: exits non-zero on missing input file" "exit=$exit_code"
    fi
}

# Help flag.
test_help_flag() {
    local output exit_code
    exit_code=0
    output=$("$SCRIPT_UNDER_TEST" --help 2>&1) || exit_code=$?
    if [ "$exit_code" -eq 0 ] && echo "$output" | grep -qi "usage"; then
        pass "help_flag: --help exits 0 and mentions Usage"
    else
        fail "help_flag: --help exits 0 and mentions Usage" "exit=$exit_code output=$output"
    fi
}

# Empty input: blank stdin must classify as real.
test_empty_input_is_real() {
    assert_stdin_yields "empty_input_is_real" "" "real"
}

# ── Run all tests ──────────────────────────────────────────────────────────

test_postgres_startup_match
test_postgres_startup_arbitrary_seconds
test_docker_daemon_match
test_dns_resolution_match
test_github_network_match
test_real_unit_test_failure
test_real_generic_error
test_first_match_wins
test_file_input
test_missing_file_errors
test_help_flag
test_empty_input_is_real

echo ""
echo "────────────────────────────────────────────"
echo "Results: $((TESTS - FAILURES))/$TESTS passed"
if [ "$FAILURES" -gt 0 ]; then
    echo "$FAILURES test(s) FAILED"
    exit 1
fi
echo "All tests passed."
exit 0
