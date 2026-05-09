#!/usr/bin/env bash
# _test-helpers.sh — Shared assert harness for shell-test fixtures.
#
# Source this file (do NOT execute it) from a shell test to use the
# canonical pass / fail / assert_eq / assert_ge / assert_contains
# helpers. Each ``assert_*`` call optionally accepts a
# ``--err-file FILE`` flag — on assertion mismatch, the harness dumps
# the file's last 50 lines bracketed with the standard
# ``── #4540 stderr dump ──`` banner.
#
# Usage:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
#   . "$SCRIPT_DIR/tests/_test-helpers.sh"
#
#   # Initialise per-test counters in the consumer's scope. The helpers
#   # increment these via the shell's lexical scoping rules (the
#   # functions reference $TESTS / $FAILURES at runtime).
#   FAILURES=0
#   TESTS=0
#
#   pass "trivial truth"
#   fail "trivial untruth" "context: anything you want"
#
#   # No err-file:
#   assert_eq "exit code matches" "$exit_code" "0"
#   assert_ge "row count meets floor" "$rows" "30"
#   assert_contains "summary header present" "$out" "Top 5 longest sections"
#
#   # With err-file (one-line collapse of the #4528 6-line recipe):
#   err1="$TMPDIR_TEST/cmd.err"
#   out1=$("$WRAPPER" 1 2>"$err1") || exit_code=$?
#   assert_eq "exit code matches" "$exit_code" "1" --err-file "$err1"
#
# When the assertion fails AND ``--err-file FILE`` was passed, the
# harness emits:
#
#     ── #4540 stderr dump ──
#       (last 50 lines of FILE, indented by 4 spaces)
#     ── end #4540 dump ──
#
# The dump fires only on assertion failure — passing assertions are
# silent on the err-file path, mirroring the "dump-on-mismatch only"
# contract PR #4526 / #4538 established for the 6-line inline recipe.
#
# Why this helper exists
# ----------------------
# Issue #4528 patched 3 sites in test_profile_shell_test.sh with the
# canonical 6-line stderr-capture-and-dump-on-failure pattern. The
# audit found ~60+ adjacent sites in other shell tests that include
# stdout in their fail message but NOT stderr. Patching each
# individually is high-churn / low-benefit with the ad-hoc helpers —
# each needs a separate ``*.err`` file + a separate conditional dump
# block. The structural fix is to extend the shared assert harness so
# every assertion call optionally accepts a stderr-file argument that
# is dumped automatically on assertion mismatch. With the helper in
# place, the canonical recipe collapses from 6 lines to 1 line per
# site. See #4540.
#
# Argument-style choice
# ---------------------
# ``assert_eq desc actual expected`` — actual before expected.
#
# This matches the existing inline assert_eq in
# test_profile_shell_test.sh (the one #4528 patched) and the worked
# example in #4540's body. test_scripts_tests_runner.sh defines the
# inverse (``desc expected actual``) but uses ``==`` so the order is
# observationally identical for equality; it does not source this
# helper, so the two coexist without conflict.
#
# Compatibility
# -------------
# bash 3.2 compatible — no bash 4+ features (no ``mapfile``, no
# associative arrays, no namerefs). The ``${var:-}`` guard is the
# canonical bash-3.2-safe idiom for testing maybe-unset variables.
#
# Idempotence
# -----------
# Sourcing this file twice is safe — the helper functions are simply
# redefined the second time, which is a no-op.

# ── Internal: dump --err-file on assertion failure ────────────────────────
# _test_helpers__dump_err <err-file>
#   Print the last 50 lines of <err-file> bracketed with the standard
#   #4540 banner. Silent (no banner) when <err-file> is empty or does
#   not exist — matches the dump-on-mismatch contract that the file
#   may legitimately be empty/absent (e.g. the wrapped command did not
#   write to stderr at all). Indents file content by 4 spaces so the
#   dump nests cleanly under the FAIL line in test output.
# shellcheck disable=SC2329  # invoked indirectly via the assert_* helpers.
_test_helpers__dump_err() {
    local err_file="${1:-}"
    if [[ -z "$err_file" ]]; then
        return 0
    fi
    if [[ ! -e "$err_file" ]]; then
        return 0
    fi
    echo "  ── #4540 stderr dump ──"
    echo "  stderr capture (last 50 lines of $err_file):"
    tail -n 50 "$err_file" 2>/dev/null | sed 's/^/    /'
    echo "  ── end #4540 dump ──"
}

# ── Internal: extract --err-file FILE from a varargs tail ─────────────────
# _test_helpers__parse_err_file <args...>
#   Echo the value of any ``--err-file FILE`` pair found in the
#   trailing positional args. Emits empty string if absent. The flag
#   may appear anywhere in the optional-args portion (we only inspect
#   the args the assert helpers pass us, not the leading desc/value
#   args). Nothing fancy — just walk the args looking for the literal
#   ``--err-file``.
# shellcheck disable=SC2329  # invoked indirectly via the assert_* helpers.
_test_helpers__parse_err_file() {
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--err-file" ]]; then
            shift
            echo "${1:-}"
            return 0
        fi
        shift
    done
    echo ""
}

# ── Public: pass <desc> ───────────────────────────────────────────────────
# Increments $TESTS in the caller's scope and prints "PASS: <desc>".
# The caller MUST have $TESTS initialised before the first call.
# shellcheck disable=SC2329  # invoked indirectly via consumer test scripts.
pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

# ── Public: fail <desc> [<context>] ───────────────────────────────────────
# Increments $TESTS and $FAILURES in the caller's scope and prints
# "FAIL: <desc>" plus an optional indented context line. The caller
# MUST have $TESTS and $FAILURES initialised before the first call.
# shellcheck disable=SC2329  # invoked indirectly via consumer test scripts.
fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── Public: assert_eq <desc> <actual> <expected> [--err-file FILE] ───────
# Asserts ``actual == expected`` (string equality, ``[[ a = b ]]``).
# On mismatch, emits FAIL plus expected/actual lines AND, if
# ``--err-file FILE`` was passed, dumps FILE's last 50 lines.
# shellcheck disable=SC2329  # invoked indirectly via consumer test scripts.
assert_eq() {
    local desc="$1"
    local actual="$2"
    local expected="$3"
    shift 3
    local err_file
    err_file=$(_test_helpers__parse_err_file "$@")
    TESTS=$((TESTS + 1))
    if [[ "$actual" = "$expected" ]]; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc"
        echo "  expected: $expected"
        echo "  actual:   $actual"
        FAILURES=$((FAILURES + 1))
        _test_helpers__dump_err "$err_file"
    fi
}

# ── Public: assert_ge <desc> <actual> <floor> [--err-file FILE] ──────────
# Asserts ``actual >= floor`` (integer comparison, ``[[ a -ge b ]]``).
# On mismatch, emits FAIL plus expected/actual lines AND, if
# ``--err-file FILE`` was passed, dumps FILE's last 50 lines.
# shellcheck disable=SC2329  # invoked indirectly via consumer test scripts.
assert_ge() {
    local desc="$1"
    local actual="$2"
    local floor="$3"
    shift 3
    local err_file
    err_file=$(_test_helpers__parse_err_file "$@")
    TESTS=$((TESTS + 1))
    if [[ "$actual" -ge "$floor" ]]; then
        echo "PASS: $desc (got $actual, floor $floor)"
    else
        echo "FAIL: $desc"
        echo "  expected ≥ $floor"
        echo "  actual:    $actual"
        FAILURES=$((FAILURES + 1))
        _test_helpers__dump_err "$err_file"
    fi
}

# ── Public: assert_contains <desc> <haystack> <needle> [--err-file FILE] ─
# Asserts ``haystack`` contains ``needle`` (literal-string match via
# ``grep -qF --``). On mismatch, emits FAIL plus expected/haystack
# lines AND, if ``--err-file FILE`` was passed, dumps FILE's last 50
# lines.
# shellcheck disable=SC2329  # invoked indirectly via consumer test scripts.
assert_contains() {
    local desc="$1"
    local haystack="$2"
    local needle="$3"
    shift 3
    local err_file
    err_file=$(_test_helpers__parse_err_file "$@")
    TESTS=$((TESTS + 1))
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc"
        echo "  expected to contain: $needle"
        echo "  haystack: $haystack"
        FAILURES=$((FAILURES + 1))
        _test_helpers__dump_err "$err_file"
    fi
}
