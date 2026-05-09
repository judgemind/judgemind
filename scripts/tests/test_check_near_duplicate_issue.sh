#!/usr/bin/env bash
# test_check_near_duplicate_issue.sh — Tests for scripts/check-near-duplicate-issue.sh.
#
# Mocks the gh CLI on PATH and asserts the documented exit codes:
#   0 — near-duplicate match (informational signal)
#   1 — no near-duplicate (the common case)
#   2 — error (missing argument, gh CLI unavailable, etc.)
#
# Usage:
#   scripts/tests/test_check_near_duplicate_issue.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$SCRIPT_DIR/check-near-duplicate-issue.sh"
FAILURES=0
TESTS=0

# ─── Helpers ──────────────────────────────────────────────────────────────

. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"
ORIG_PATH_SAVE=""
restore_path() {
    if [[ -n "$ORIG_PATH_SAVE" ]]; then
        export PATH="$ORIG_PATH_SAVE"
    fi
}
register_cleanup_hook restore_path

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

ORIG_PATH_SAVE="$PATH"
MOCK_BIN_DIR=$(mktemp -d)
register_temp_dir "$MOCK_BIN_DIR"
export PATH="$MOCK_BIN_DIR:$ORIG_PATH_SAVE"

MOCK_GH="$MOCK_BIN_DIR/gh"

if [[ ! -x "$WRAPPER" ]]; then
    echo "FAIL: $WRAPPER is not executable (or does not exist)" >&2
    exit 1
fi

# ─── Test 1: exit 2 when called with no argument ──────────────────────────

exit_code=0
"$WRAPPER" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with no argument"
else
    fail "exits 2 with no argument" "expected exit 2, got $exit_code"
fi

# ─── Test 2: exit 2 with non-numeric argument ─────────────────────────────

exit_code=0
"$WRAPPER" not-a-number > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with non-numeric argument"
else
    fail "exits 2 with non-numeric argument" "expected exit 2, got $exit_code"
fi

# ─── Test 3: leading-# stripped from argument ─────────────────────────────
#
# Provide a mock gh that fails fast on the issue-view stage so the wrapper
# exits 2; the load-bearing assertion is that '#4520' parses successfully
# (no "not a valid issue number" error) and reaches the gh call.

cat > "$MOCK_GH" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$MOCK_GH"

stderr=$("$WRAPPER" '#4520' 2>&1 >/dev/null)
exit_code=0
"$WRAPPER" '#4520' > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 && "$stderr" != *"not a valid issue number"* ]]; then
    pass "leading-# stripped from argument"
else
    fail "leading-# stripped from argument" \
        "expected exit 2 + no parse error, got exit=$exit_code stderr=$stderr"
fi

# ─── Test 4: CHECK_NEAR_DUP_DISABLE=1 short-circuits to exit 1 ────────────

cat > "$MOCK_GH" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    issue)
        case "$2" in
            view)
                echo '{"body": "test", "title": "test", "createdAt": "2026-05-08T19:49:25Z"}'
                exit 0
                ;;
        esac
        ;;
esac
exit 1
EOF
chmod +x "$MOCK_GH"

exit_code=0
output=$(CHECK_NEAR_DUP_DISABLE=1 "$WRAPPER" 4520 2>&1) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"disabled via CHECK_NEAR_DUP_DISABLE"* ]]; then
    pass "CHECK_NEAR_DUP_DISABLE=1 short-circuits to exit 1"
else
    fail "CHECK_NEAR_DUP_DISABLE=1 short-circuits to exit 1" \
        "exit=$exit_code output=$output"
fi

# ─── Test 5: exit 1 on no-near-duplicate ──────────────────────────────────

cat > "$MOCK_GH" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
    "issue view")
        # Initial fetch of current issue
        echo '{"body": "isolated change to scripts/widget_loader.py", "title": "fix(scrapers): widget loader", "createdAt": "2026-05-08T19:49:25Z"}'
        exit 0
        ;;
    "issue list")
        # Recently-closed issues — none in window
        echo '[]'
        exit 0
        ;;
esac
exit 1
EOF
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4520 2>&1) || exit_code=$?
if [[ "$exit_code" -eq 1 && "$output" == *"no near-duplicate"* ]]; then
    pass "exits 1 on no near-duplicate"
else
    fail "exits 1 on no near-duplicate" \
        "exit=$exit_code output=$output"
fi

# ─── Test 6: exit 0 on near-duplicate match (canonical 4321 ↔ 4355) ───────

cat > "$MOCK_GH" <<'EOF'
#!/usr/bin/env bash
# argv: $1 = subcommand, $2 = action
case "$1 $2" in
    "issue view")
        # When the wrapper fetches the current issue (#4355), return its
        # body; when the python helper fetches candidate #4321, return
        # 4321's title + body + closedByPullRequestsReferences.
        # Distinguish by issue number — argv[3] is the issue number.
        case "$3" in
            4355)
                cat <<'JSON'
{"body": "## Proposal\n\nCreate `scripts/drain_splitter_carry_forward_clusters.py` (one-off, scraper-framework venv).", "title": "tooling: drain_splitter_carry_forward_clusters.py canonical drain helper", "createdAt": "2026-05-08T19:49:25Z"}
JSON
                exit 0
                ;;
            4321)
                cat <<'JSON'
{"title": "feat(dx): generic splitter-carry-forward drain helper", "body": "## Proposal\n\nAdd a generic helper script `scripts/drain_splitter_carry_forward_clusters.py`.", "closedAt": "2026-05-08T17:07:58Z", "closedByPullRequestsReferences": [{"number": 4325, "state": "MERGED"}]}
JSON
                exit 0
                ;;
        esac
        ;;
    "issue list")
        echo '[{"number": 4321}]'
        exit 0
        ;;
esac
exit 1
EOF
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4355 2>&1) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"near-duplicate: #4321"* && "$output" == *"PR #4325"* ]]; then
    pass "exits 0 on near-duplicate match (#4321 ↔ #4355 canonical AC1)"
else
    fail "exits 0 on near-duplicate match (#4321 ↔ #4355 canonical AC1)" \
        "exit=$exit_code output=$output"
fi

# ─── Test 7: machine-readable second line (TAB-separated payload) ─────────

# Reuse the same mock from Test 6 — output already captured.
# Second line should be `<closed_issue>\t<closing_pr>\t<channel>\t<overlap>`.
second_line=$(printf '%s\n' "$output" | sed -n '2p')
expected_prefix='4321	4325	'
if [[ "$second_line" == "$expected_prefix"* ]]; then
    pass "machine-readable second line shape (4321\\t4325\\t...)"
else
    fail "machine-readable second line shape (4321\\t4325\\t...)" \
        "got: '$second_line'"
fi

# ─── Test 8: exit 0 on near-duplicate match, closed issue with no closing PR ──

cat > "$MOCK_GH" <<'EOF'
#!/usr/bin/env bash
case "$1 $2" in
    "issue view")
        case "$3" in
            4355)
                cat <<'JSON'
{"body": "## Proposal\n\nCreate `scripts/drain_splitter_carry_forward_clusters.py`.", "title": "tooling: drain_splitter_carry_forward_clusters.py canonical drain helper", "createdAt": "2026-05-08T19:49:25Z"}
JSON
                exit 0
                ;;
            4321)
                # Closed issue with no closedByPullRequestsReferences (AC scenario d).
                cat <<'JSON'
{"title": "feat(dx): generic splitter-carry-forward drain helper", "body": "Add `scripts/drain_splitter_carry_forward_clusters.py`.", "closedAt": "2026-05-08T17:07:58Z", "closedByPullRequestsReferences": []}
JSON
                exit 0
                ;;
        esac
        ;;
    "issue list")
        echo '[{"number": 4321}]'
        exit 0
        ;;
esac
exit 1
EOF
chmod +x "$MOCK_GH"

exit_code=0
output=$("$WRAPPER" 4355 2>&1) || exit_code=$?
# When closing_pr is absent, the human-readable line drops the PR clause.
if [[ "$exit_code" -eq 0 && "$output" == *"near-duplicate: #4321"* && "$output" != *"PR #"* ]]; then
    pass "exits 0 on closed-with-no-PR (AC scenario d)"
else
    fail "exits 0 on closed-with-no-PR (AC scenario d)" \
        "exit=$exit_code output=$output"
fi

# ─── Summary ──────────────────────────────────────────────────────────────

echo ""
echo "Tests: $TESTS, Failures: $FAILURES"
if [[ "$FAILURES" -gt 0 ]]; then
    exit 1
fi
exit 0
