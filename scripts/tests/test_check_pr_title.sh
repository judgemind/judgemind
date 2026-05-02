#!/usr/bin/env bash
# test_check_pr_title.sh — Tests for check-pr-title.sh
#
# Verifies that the checker correctly rejects placeholder PR titles and
# empty/whitespace-only PR bodies, while allowing legitimate title+body pairs.
#
# Usage:
#   scripts/tests/test_check_pr_title.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-pr-title.sh"
FAILURES=0
TESTS=0

TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

assert_fails() {
    local desc="$1"
    local title="$2"
    local body="$3"
    TESTS=$((TESTS + 1))
    if PR_TITLE="$title" PR_BODY="$body" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    local title="$2"
    local body="$3"
    TESTS=$((TESTS + 1))
    if PR_TITLE="$title" PR_BODY="$body" "$CHECK_SCRIPT" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Placeholder title tests ───────────────────────────────────────────

assert_fails "WIP: prefix is rejected" \
    "WIP: foo" \
    "Some body text"

assert_fails "WIP: bare title is rejected" \
    "WIP:" \
    "Some body text"

assert_fails "ralph output title is rejected" \
    "ralph output" \
    "Some body text"

assert_fails "Placeholder: prefix is rejected" \
    "Placeholder: stub" \
    "Some body text"

assert_fails "wip: lowercase is rejected" \
    "wip: foo" \
    "Some body text"

assert_fails "Wip: mixed case is rejected" \
    "Wip: foo" \
    "Some body text"

# ─── Empty/whitespace body tests ──────────────────────────────────────

assert_fails "Valid title with empty body is rejected" \
    "feat(api): add foo" \
    ""

assert_fails "Valid title with whitespace-only body is rejected" \
    "feat(api): add foo" \
    "   "

# ─── Good title + non-empty body passes ───────────────────────────────

assert_passes "Conventional commits title with body is accepted" \
    "feat(api): add foo" \
    "Some body text"

assert_passes "WIP mid-string (not at start) with non-empty body is accepted" \
    "add WIP detection helper" \
    "Implements the detection logic described in #3994"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
