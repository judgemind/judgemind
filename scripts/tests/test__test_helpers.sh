#!/usr/bin/env bash
# test__test_helpers.sh — Regression test for scripts/tests/_test-helpers.sh.
#
# NOTE on the filename — the issue body for #4540 named this file
# `_test-helpers-test.sh`, but `scripts/run-scripts-tests.sh::is_helper`
# silently skips any file whose basename starts with `_` (treating
# them as sourceable helpers, not standalone tests). Naming the test
# `test__test_helpers.sh` keeps the AC's intent (a runnable test for
# the new shared helper) and makes the test discoverable by the runner.
#
# Exercises the shared assert harness extracted in #4540:
#
#   AC1 — Helper sources cleanly and exposes pass / fail / assert_eq /
#         assert_ge / assert_contains as functions.
#   AC2 — bash -n exits 0 on the helper file.
#   AC3 — pass / fail / assert_eq / assert_ge / assert_contains
#         increment $TESTS / $FAILURES correctly on the pass path
#         AND on the fail path.
#   AC4 — assert_eq accepts ``--err-file FILE`` and dumps the file's
#         last 50 lines on mismatch ONLY (not on pass).
#   AC5 — assert_ge accepts ``--err-file FILE`` and dumps on mismatch.
#   AC6 — assert_contains accepts ``--err-file FILE`` and dumps on
#         mismatch.
#   AC7 — When --err-file points to a missing file, the dump is
#         silent (no banner) — this is the "the wrapped command did
#         not write to stderr" case the contract permits.
#   AC8 — Bash 3.2 compatibility — scripts/check-bash-compat.sh exits 0
#         on the helper file.
#
# Test strategy
# -------------
# Each scenario invokes the helper from inside this fixture and
# captures stdout/stderr to verify both the assertion side-effects
# (counter increments) and the printed output (PASS/FAIL banners,
# dump bracket markers).
#
# The fail-path tests deliberately invoke the helper's fail path and
# capture its output into a string, so the parent fixture's own
# $TESTS / $FAILURES accounting reflects only the regression-test
# results, not the inner failure.
#
# Usage:
#   scripts/tests/_test-helpers-test.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$SCRIPT_DIR/tests/_test-helpers.sh"

TESTS=0
FAILURES=0

# ── Precondition: helper exists ───────────────────────────────────────────
if [[ ! -f "$HELPER" ]]; then
    echo "FAIL: $HELPER does not exist" >&2
    exit 1
fi

# Cleanup of temp directories via the shared #4343 helper.
. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

TMPDIR_TEST=$(mktemp -d)
register_temp_dir "$TMPDIR_TEST"

# Local pass/fail for THIS regression test — defined inline so the
# parent fixture's accounting is independent of the helper under test.
# (The helper's pass/fail are exercised via child shells below.)
parent_pass() {
    TESTS=$((TESTS + 1))
    echo "PASS: $1"
}

parent_fail() {
    TESTS=$((TESTS + 1))
    FAILURES=$((FAILURES + 1))
    echo "FAIL: $1"
    if [[ -n "${2:-}" ]]; then
        echo "  $2"
    fi
}

# ── AC1: helper sources cleanly and exposes the five functions ────────────
out=$(bash -c "set -uo pipefail; . '$HELPER'; \
declare -F pass > /dev/null && \
declare -F fail > /dev/null && \
declare -F assert_eq > /dev/null && \
declare -F assert_ge > /dev/null && \
declare -F assert_contains > /dev/null && \
echo OK" 2>&1) || true
if [[ "$out" == "OK" ]]; then
    parent_pass "AC1: helper sources and exposes pass/fail/assert_{eq,ge,contains}"
else
    parent_fail "AC1: helper does not expose all five functions" "got: $out"
fi

# ── AC2: bash -n exits 0 on the helper ────────────────────────────────────
if bash -n "$HELPER" 2>/dev/null; then
    parent_pass "AC2: bash -n on helper exits 0"
else
    parent_fail "AC2: bash -n on helper does not exit 0" ""
fi

# ── AC3a: pass increments TESTS by 1 and prints PASS banner ───────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
pass 'foo'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"PASS: foo"* && "$out" == *"TESTS=1 FAILURES=0"* ]]; then
    parent_pass "AC3a: pass prints PASS banner and increments TESTS"
else
    parent_fail "AC3a: pass output unexpected" "got: $out"
fi

# ── AC3b: fail increments TESTS+FAILURES and prints FAIL banner ──────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
fail 'foo' 'context'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"FAIL: foo"* && "$out" == *"context"* && "$out" == *"TESTS=1 FAILURES=1"* ]]; then
    parent_pass "AC3b: fail prints FAIL banner+context and increments TESTS+FAILURES"
else
    parent_fail "AC3b: fail output unexpected" "got: $out"
fi

# ── AC3c: assert_eq pass-path ────────────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-pass' 'a' 'a'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"PASS: eq-pass"* && "$out" == *"TESTS=1 FAILURES=0"* ]]; then
    parent_pass "AC3c: assert_eq pass-path increments TESTS only"
else
    parent_fail "AC3c: assert_eq pass-path unexpected" "got: $out"
fi

# ── AC3d: assert_eq fail-path ────────────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-fail' 'a' 'b'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"FAIL: eq-fail"* && "$out" == *"expected: b"* && "$out" == *"actual:   a"* && "$out" == *"TESTS=1 FAILURES=1"* ]]; then
    parent_pass "AC3d: assert_eq fail-path emits FAIL+expected/actual and increments FAILURES"
else
    parent_fail "AC3d: assert_eq fail-path unexpected" "got: $out"
fi

# ── AC3e: assert_ge pass-path ────────────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_ge 'ge-pass' '5' '3'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"PASS: ge-pass (got 5, floor 3)"* && "$out" == *"TESTS=1 FAILURES=0"* ]]; then
    parent_pass "AC3e: assert_ge pass-path increments TESTS only"
else
    parent_fail "AC3e: assert_ge pass-path unexpected" "got: $out"
fi

# ── AC3f: assert_ge fail-path ────────────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_ge 'ge-fail' '2' '5'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"FAIL: ge-fail"* && "$out" == *"expected ≥ 5"* && "$out" == *"actual:    2"* && "$out" == *"TESTS=1 FAILURES=1"* ]]; then
    parent_pass "AC3f: assert_ge fail-path emits FAIL+floor/actual and increments FAILURES"
else
    parent_fail "AC3f: assert_ge fail-path unexpected" "got: $out"
fi

# ── AC3g: assert_contains pass-path ──────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_contains 'cn-pass' 'hello world' 'world'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"PASS: cn-pass"* && "$out" == *"TESTS=1 FAILURES=0"* ]]; then
    parent_pass "AC3g: assert_contains pass-path increments TESTS only"
else
    parent_fail "AC3g: assert_contains pass-path unexpected" "got: $out"
fi

# ── AC3h: assert_contains fail-path ──────────────────────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_contains 'cn-fail' 'hello world' 'goodbye'; echo \"TESTS=\$TESTS FAILURES=\$FAILURES\"" 2>&1) || true
if [[ "$out" == *"FAIL: cn-fail"* && "$out" == *"expected to contain: goodbye"* && "$out" == *"haystack: hello world"* && "$out" == *"TESTS=1 FAILURES=1"* ]]; then
    parent_pass "AC3h: assert_contains fail-path emits FAIL+needle/haystack and increments FAILURES"
else
    parent_fail "AC3h: assert_contains fail-path unexpected" "got: $out"
fi

# ── AC4a: assert_eq with --err-file PASS — no dump emitted ───────────────
err_file="$TMPDIR_TEST/ac4a.err"
echo "captured-stderr-line" > "$err_file"
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-ok' 'x' 'x' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"PASS: eq-ok"* && "$out" != *"#4540 stderr dump"* && "$out" != *"captured-stderr-line"* ]]; then
    parent_pass "AC4a: assert_eq pass-path is silent on --err-file"
else
    parent_fail "AC4a: assert_eq pass-path leaked --err-file dump" "got: $out"
fi

# ── AC4b: assert_eq with --err-file FAIL — dump emitted ──────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-bad' 'x' 'y' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"FAIL: eq-bad"* \
    && "$out" == *"── #4540 stderr dump ──"* \
    && "$out" == *"captured-stderr-line"* \
    && "$out" == *"── end #4540 dump ──"* ]]; then
    parent_pass "AC4b: assert_eq fail-path with --err-file emits #4540 dump banner+content"
else
    parent_fail "AC4b: assert_eq fail-path dump unexpected" "got: $out"
fi

# ── AC5a: assert_ge with --err-file PASS — no dump ───────────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_ge 'ge-ok' '7' '5' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"PASS: ge-ok"* && "$out" != *"#4540 stderr dump"* ]]; then
    parent_pass "AC5a: assert_ge pass-path is silent on --err-file"
else
    parent_fail "AC5a: assert_ge pass-path leaked --err-file dump" "got: $out"
fi

# ── AC5b: assert_ge with --err-file FAIL — dump emitted ──────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_ge 'ge-bad' '2' '5' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"FAIL: ge-bad"* \
    && "$out" == *"── #4540 stderr dump ──"* \
    && "$out" == *"captured-stderr-line"* \
    && "$out" == *"── end #4540 dump ──"* ]]; then
    parent_pass "AC5b: assert_ge fail-path with --err-file emits #4540 dump banner+content"
else
    parent_fail "AC5b: assert_ge fail-path dump unexpected" "got: $out"
fi

# ── AC6a: assert_contains with --err-file PASS — no dump ─────────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_contains 'cn-ok' 'hello world' 'hello' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"PASS: cn-ok"* && "$out" != *"#4540 stderr dump"* ]]; then
    parent_pass "AC6a: assert_contains pass-path is silent on --err-file"
else
    parent_fail "AC6a: assert_contains pass-path leaked --err-file dump" "got: $out"
fi

# ── AC6b: assert_contains with --err-file FAIL — dump emitted ────────────
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_contains 'cn-bad' 'hello world' 'goodbye' --err-file '$err_file'" 2>&1) || true
if [[ "$out" == *"FAIL: cn-bad"* \
    && "$out" == *"── #4540 stderr dump ──"* \
    && "$out" == *"captured-stderr-line"* \
    && "$out" == *"── end #4540 dump ──"* ]]; then
    parent_pass "AC6b: assert_contains fail-path with --err-file emits #4540 dump banner+content"
else
    parent_fail "AC6b: assert_contains fail-path dump unexpected" "got: $out"
fi

# ── AC7: --err-file pointing at a missing file is silent ──────────────────
missing_err="$TMPDIR_TEST/does-not-exist.err"
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-missing-err' 'x' 'y' --err-file '$missing_err'" 2>&1) || true
if [[ "$out" == *"FAIL: eq-missing-err"* && "$out" != *"#4540 stderr dump"* ]]; then
    parent_pass "AC7: --err-file pointing at a missing file does not emit a dump banner"
else
    parent_fail "AC7: missing --err-file path leaked dump banner" "got: $out"
fi

# ── AC8: bash 3.2 compatibility ──────────────────────────────────────────
if "$SCRIPT_DIR/check-bash-compat.sh" "$HELPER" >/dev/null 2>&1; then
    parent_pass "AC8: helper passes scripts/check-bash-compat.sh"
else
    parent_fail "AC8: helper has bash 4+ constructs" ""
fi

# ── Tail: empty --err-file argument is silent (defensive) ────────────────
empty_err_arg="$TMPDIR_TEST/empty.err"
: > "$empty_err_arg"  # zero-byte file
out=$(bash -c "set -uo pipefail; TESTS=0; FAILURES=0; . '$HELPER'; \
assert_eq 'eq-empty-err' 'x' 'y' --err-file '$empty_err_arg'" 2>&1) || true
# A zero-byte file should still emit the banner (file exists, has 0
# lines to dump — the banner is informative, the empty body is fine).
# This documents the behavior: existence is the gate, not non-emptiness.
if [[ "$out" == *"FAIL: eq-empty-err"* && "$out" == *"── #4540 stderr dump ──"* && "$out" == *"── end #4540 dump ──"* ]]; then
    parent_pass "AC9: --err-file pointing at a zero-byte file still emits the banner (existence is the gate)"
else
    parent_fail "AC9: zero-byte --err-file behavior unexpected" "got: $out"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
