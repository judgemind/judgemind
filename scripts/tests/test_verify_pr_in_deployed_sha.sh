#!/usr/bin/env bash
# test_verify_pr_in_deployed_sha.sh — Tests for scripts/verify-pr-in-deployed-sha.sh
#
# Covers the three documented exit codes plus the common error paths:
#   0 — PR merge SHA is an ancestor of deployed SHA (PR landed)
#   1 — PR merge SHA is NOT an ancestor of deployed SHA (not yet deployed)
#   2 — Usage / environment / data error (missing arg, PR not merged,
#       missing tool on PATH, no startup log, git can't resolve SHA)
#
# Strategy: each test runs the script under an isolated temp PATH that
# masks the real `gh`, `aws`, and `git` binaries with tiny bash stubs.
# Each stub emits canned output matching the real tool's JSON shape,
# so the script sees exactly what it would see against the live APIs
# — without any network calls.
#
# Usage:
#   scripts/tests/test_verify_pr_in_deployed_sha.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT_UNDER_TEST="$REPO_ROOT/scripts/verify-pr-in-deployed-sha.sh"
FAILURES=0
TESTS=0

# ── Helpers ────────────────────────────────────────────────────────────────

# Cleanup of temp directories + PATH restore via the shared helper
# (see #4343). PATH is captured here at module-load time and the
# restore_path hook fires before the rm-phase of the trap.
. "$REPO_ROOT/scripts/tests/_temp_cleanup_helpers.sh"
ORIG_PATH_SAVE="$PATH"
restore_path() {
    export PATH="$ORIG_PATH_SAVE"
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

make_temp_dir() {
    local dir
    dir=$(mktemp -d)
    register_temp_dir "$dir"
    echo "$dir"
}

# Create a mock `gh` stub that echoes the canned JSON on `pr view ... --json`.
# Args: <mock-bin-dir> <canned-json>
install_gh_mock() {
    local bin_dir="$1"
    local json="$2"
    cat > "$bin_dir/gh" << MOCKGH
#!/usr/bin/env bash
# Mock gh: emit canned JSON on 'gh pr view <N> --json ...'.
if [[ "\${1:-}" == "pr" && "\${2:-}" == "view" ]]; then
    printf '%s' '$json'
    exit 0
fi
echo "mock gh: unexpected invocation: \$*" >&2
exit 2
MOCKGH
    chmod +x "$bin_dir/gh"
}

# Create a mock `gh` stub that fails (exit 1) on every invocation —
# simulates an unauthenticated / rate-limited / API-down scenario.
install_gh_mock_fail() {
    local bin_dir="$1"
    cat > "$bin_dir/gh" << 'MOCKGH'
#!/usr/bin/env bash
echo "mock gh: simulated API failure" >&2
exit 1
MOCKGH
    chmod +x "$bin_dir/gh"
}

# Create a mock `aws` stub that echoes canned events JSON on
# `aws logs filter-log-events ...`.
# Args: <mock-bin-dir> <events-json-array>  e.g. '[{"timestamp":1,"message":"..."}]'
install_aws_mock() {
    local bin_dir="$1"
    local events_json="$2"
    cat > "$bin_dir/aws" << MOCKAWS
#!/usr/bin/env bash
# Mock aws: emit canned filter-log-events response.
if [[ "\${1:-}" == "logs" && "\${2:-}" == "filter-log-events" ]]; then
    printf '{"events": %s}' '$events_json'
    exit 0
fi
echo "mock aws: unexpected invocation: \$*" >&2
exit 2
MOCKAWS
    chmod +x "$bin_dir/aws"
}

# Create a mock `git` stub that delegates to the real git binary EXCEPT
# for merge-base / rev-parse / rev-list, which are controlled by an
# environment variable to force a specific outcome.
# Args: <mock-bin-dir> <is-ancestor-exit:0|1> <rev-parse-deployed-ok:0|1> <rev-parse-merge-ok:0|1>
install_git_mock() {
    local bin_dir="$1"
    local is_ancestor_exit="$2"
    local rev_parse_deployed_ok="$3"
    local rev_parse_merge_ok="$4"
    cat > "$bin_dir/git" << MOCKGIT
#!/usr/bin/env bash
# Mock git: handle merge-base / rev-parse / rev-list deterministically.
# Any other subcommand defers to the real git.
case "\${1:-}" in
    merge-base)
        # '\$1=merge-base \$2=--is-ancestor \$3=<merge> \$4=<deployed>'
        if [[ "\${2:-}" == "--is-ancestor" ]]; then
            exit $is_ancestor_exit
        fi
        ;;
    rev-parse)
        # Called with '\$1=rev-parse \$2=--verify \$3=--quiet \$4=<sha>^{commit}'
        # The test uses 'DEPLOYED_SHA' as the deployed stand-in literal and
        # 'MERGE_SHA' for the merge SHA literal; we key on substring.
        sha_arg="\${4:-}"
        if [[ "\$sha_arg" == *DEPLOYED* ]]; then
            if [[ "$rev_parse_deployed_ok" == "0" ]]; then exit 0; else exit 1; fi
        fi
        if [[ "\$sha_arg" == *MERGE* ]]; then
            if [[ "$rev_parse_merge_ok" == "0" ]]; then exit 0; else exit 1; fi
        fi
        # Unknown SHA — fall back to failing safe.
        exit 1
        ;;
    rev-list)
        # --count returns a plausible distance count.
        echo "3"
        exit 0
        ;;
esac
echo "mock git: unexpected invocation: \$*" >&2
exit 2
MOCKGIT
    chmod +x "$bin_dir/git"
}

# ── Precondition: script under test exists and is executable ──────────────

if [[ ! -x "$SCRIPT_UNDER_TEST" ]]; then
    echo "FATAL: $SCRIPT_UNDER_TEST is not executable (or does not exist)" >&2
    exit 1
fi

# ── Test 1: exit 2 when called with no argument ────────────────────────────

exit_code=0
"$SCRIPT_UNDER_TEST" > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 with no argument"
else
    fail "exits 2 with no argument" "expected exit 2, got $exit_code"
fi

# ── Test 2: exit 2 on non-integer argument ─────────────────────────────────

exit_code=0
"$SCRIPT_UNDER_TEST" notanumber > /dev/null 2>&1 || exit_code=$?
if [[ "$exit_code" -eq 2 ]]; then
    pass "exits 2 on non-integer argument"
else
    fail "exits 2 on non-integer argument" "expected exit 2, got $exit_code"
fi

# ── Test 3: --help prints usage and exits 0 ────────────────────────────────

exit_code=0
output=$("$SCRIPT_UNDER_TEST" --help 2>&1) || exit_code=$?
if [[ "$exit_code" -eq 0 && "$output" == *"Usage:"* ]]; then
    pass "--help prints usage and exits 0"
else
    fail "--help prints usage and exits 0" "exit=$exit_code output='$output'"
fi

# ── Test 4: exit 0 when PR merge SHA is an ancestor of deployed SHA ────────

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":{"oid":"MERGESHAfull1234567890"},"number":3066,"state":"MERGED"}'
# Build a filter-log-events response with one startup event containing the deployed sha.
# The mock 'aws' printf-substitutes an events-array literal — build carefully.
EVENTS='[{"timestamp": 1700000000000, "message": "{\"event\":\"startup\",\"version_sha\":\"DEPLOYEDsha\",\"host\":\"h\",\"pid\":1}"}]'
install_aws_mock "$TDIR" "$EVENTS"
install_git_mock "$TDIR" 0 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3066 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 0 ]]; then
    pass "exits 0 when PR is ancestor of deployed SHA"
else
    fail "exits 0 when PR is ancestor of deployed SHA" "exit=$exit_code output='$output'"
fi

if [[ "$output" == *"landed"* && "$output" == *"DEPLOYEDsha"* && "$output" == *"MERGESH"* ]]; then
    pass "prints 'landed' + deployed SHA + merge SHA prefix on success"
else
    fail "prints 'landed' + deployed SHA + merge SHA prefix on success" "got '$output'"
fi

# ── Test 5: exit 1 when PR merge SHA is NOT an ancestor of deployed SHA ───

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":{"oid":"MERGESHAfull1234567890"},"number":3099,"state":"MERGED"}'
install_aws_mock "$TDIR" "$EVENTS"
install_git_mock "$TDIR" 1 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3099 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 1 ]]; then
    pass "exits 1 when PR is NOT ancestor of deployed SHA"
else
    fail "exits 1 when PR is NOT ancestor of deployed SHA" "exit=$exit_code output='$output'"
fi

if [[ "$output" == *"NOT in deployed"* ]]; then
    pass "prints 'NOT in deployed' on exit-1"
else
    fail "prints 'NOT in deployed' on exit-1" "got '$output'"
fi

# ── Test 6: exit 2 when PR is OPEN (not merged) ────────────────────────────

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":null,"number":3044,"state":"OPEN"}'
# Even though we don't expect aws/git to be called, install stubs so a
# bug that lets the script fall through yields a clear failure, not a
# mysterious "aws not found" from the stripped PATH.
install_aws_mock "$TDIR" "[]"
install_git_mock "$TDIR" 0 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3044 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 2 && "$output" == *"not merged"* ]]; then
    pass "exits 2 when PR is OPEN (not merged)"
else
    fail "exits 2 when PR is OPEN (not merged)" "exit=$exit_code output='$output'"
fi

# ── Test 7: exit 2 when CloudWatch returns no startup events ──────────────

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":{"oid":"MERGESHAfull1234567890"},"number":3066,"state":"MERGED"}'
install_aws_mock "$TDIR" "[]"
install_git_mock "$TDIR" 0 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3066 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 2 && "$output" == *"no recent"* ]]; then
    pass "exits 2 when CloudWatch returns no startup events"
else
    fail "exits 2 when CloudWatch returns no startup events" "exit=$exit_code output='$output'"
fi

# ── Test 8: exit 2 when gh pr view fails (API error) ──────────────────────

TDIR=$(make_temp_dir)
install_gh_mock_fail "$TDIR"
install_aws_mock "$TDIR" "$EVENTS"
install_git_mock "$TDIR" 0 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3066 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 2 && "$output" == *"gh pr view"* ]]; then
    pass "exits 2 when gh pr view fails"
else
    fail "exits 2 when gh pr view fails" "exit=$exit_code output='$output'"
fi

# ── Test 9: exit 2 when deployed SHA not resolvable in local git ──────────

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":{"oid":"MERGESHAfull1234567890"},"number":3066,"state":"MERGED"}'
install_aws_mock "$TDIR" "$EVENTS"
# rev_parse_deployed_ok=1 → simulate the local clone not having the deployed SHA.
install_git_mock "$TDIR" 0 1 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" 3066 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 2 && "$output" == *"deployed SHA"* && "$output" == *"not resolvable"* ]]; then
    pass "exits 2 when deployed SHA not resolvable locally"
else
    fail "exits 2 when deployed SHA not resolvable locally" "exit=$exit_code output='$output'"
fi

# ── Test 10: strips leading '#' from the pr-number argument ────────────────

TDIR=$(make_temp_dir)
install_gh_mock "$TDIR" '{"mergeCommit":{"oid":"MERGESHAfull1234567890"},"number":3066,"state":"MERGED"}'
install_aws_mock "$TDIR" "$EVENTS"
install_git_mock "$TDIR" 0 0 0

export PATH="$TDIR:$ORIG_PATH_SAVE"
exit_code=0
output=$("$SCRIPT_UNDER_TEST" "#3066" 2>&1) || exit_code=$?
export PATH="$ORIG_PATH_SAVE"

if [[ "$exit_code" -eq 0 && "$output" == *"PR #3066 landed"* ]]; then
    pass "strips leading '#' from the pr-number argument"
else
    fail "strips leading '#' from the pr-number argument" "exit=$exit_code output='$output'"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
