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
. "$(dirname "${BASH_SOURCE[0]}")/_gh-isolate.sh"  # no real GitHub from tests (#5071)

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

# ─── --pr mode: reads the live title/body over REST (#5133) ───────────
# A fake gh serves the PR. MOCK_TITLE / MOCK_BODY set the text;
# MOCK_FAIL=1 makes the read fail. Calls are logged to $TMPDIR_TEST/calls.
MOCK_BIN="$TMPDIR_TEST/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" << 'MOCKEOF'
#!/usr/bin/env bash
echo "$*" >> "$MOCK_CALLS"
if [[ "${MOCK_FAIL:-0}" == "1" ]]; then
    echo "HTTP 502: Bad Gateway" >&2
    exit 1
fi
if [[ "$1" == "api" && "$2" =~ ^/repos/[^/]+/[^/]+/pulls/[0-9]+$ && "$3" == "--jq" ]]; then
    jq -cn --arg t "${MOCK_TITLE:-}" --arg b "${MOCK_BODY:-}" '{title: $t, body: $b}' | jq -r "$4"
    exit 0
fi
echo "mock gh: unhandled: $*" >&2
exit 127
MOCKEOF
chmod +x "$MOCK_BIN/gh"
export MOCK_CALLS="$TMPDIR_TEST/calls"

run_pr_mode() {
    PATH="$MOCK_BIN:$PATH" PR_TITLE="" PR_BODY="" "$CHECK_SCRIPT" "$@" > /dev/null 2>&1
}

TESTS=$((TESTS + 1))
: > "$MOCK_CALLS"
rc=0
MOCK_TITLE="WIP: stub" MOCK_BODY="body" run_pr_mode --pr 990001 --repo o/r || rc=$?
if [[ "$rc" -eq 1 ]] && grep -q "^api /repos/o/r/pulls/990001 --jq" "$MOCK_CALLS"; then
    echo "PASS: --pr mode rejects a placeholder live title"
else
    echo "FAIL: --pr mode rejects a placeholder live title (rc=$rc calls: $(cat "$MOCK_CALLS"))"
    FAILURES=$((FAILURES + 1))
fi

TESTS=$((TESTS + 1))
rc=0
MOCK_TITLE="feat(x): y" MOCK_BODY="" run_pr_mode --pr 990001 || rc=$?
if [[ "$rc" -eq 1 ]]; then
    echo "PASS: --pr mode rejects an empty live body"
else
    echo "FAIL: --pr mode rejects an empty live body (rc=$rc)"
    FAILURES=$((FAILURES + 1))
fi

TESTS=$((TESTS + 1))
rc=0
MOCK_TITLE="feat(x): y" MOCK_BODY="real body" run_pr_mode --pr 990001 || rc=$?
if [[ "$rc" -eq 0 ]]; then
    echo "PASS: --pr mode accepts a good live title and body"
else
    echo "FAIL: --pr mode accepts a good live title and body (rc=$rc)"
    FAILURES=$((FAILURES + 1))
fi

TESTS=$((TESTS + 1))
rc=0
MOCK_FAIL=1 run_pr_mode --pr 990001 || rc=$?
if [[ "$rc" -eq 2 ]]; then
    echo "PASS: --pr mode exits 2 when the PR cannot be read"
else
    echo "FAIL: --pr mode exits 2 when the PR cannot be read (rc=$rc)"
    FAILURES=$((FAILURES + 1))
fi

for bad_args in "--pr abc" "--pr" "--bogus"; do
    TESTS=$((TESTS + 1))
    rc=0
    # shellcheck disable=SC2086  # split the arg list on purpose
    run_pr_mode $bad_args || rc=$?
    if [[ "$rc" -eq 2 ]]; then
        echo "PASS: usage error exits 2 ($bad_args)"
    else
        echo "FAIL: usage error exits 2 ($bad_args) (rc=$rc)"
        FAILURES=$((FAILURES + 1))
    fi
done

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
