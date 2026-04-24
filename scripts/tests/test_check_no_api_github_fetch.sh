#!/usr/bin/env bash
# test_check_no_api_github_fetch.sh — Tests for
# check-no-api-github-fetch.sh.
#
# Creates temporary files to verify that the checker correctly detects
# forbidden GitHub API references while allowing comment-only references,
# unrelated OAuth calls, JSDoc annotations, empty directories, and
# excluded directories (.git, node_modules).
#
# Transitive-import note (test d): the guard catches GitHub API calls via
# direct scan of every file under SCAN_DIR.  Since all relative imports
# from packages/api/src/ resolve within packages/api/src/, a direct scan
# catches any fetch anywhere in the API container code.  No AST walk is
# performed — if a future refactor introduces packages/api/lib/ or similar,
# extend the scan path then.
#
# Usage:
#   scripts/tests/test_check_no_api_github_fetch.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-api-github-fetch.sh"
FAILURES=0
TESTS=0

# Use a temp directory so we don't pollute the repo.
TMPDIR_TEST=$(mktemp -d)
cleanup() {
    rm -rf "$TMPDIR_TEST"
}
trap cleanup EXIT

create_test_file() {
    local name="$1"
    local content="$2"
    local dir
    dir="$(dirname "$TMPDIR_TEST/$name")"
    mkdir -p "$dir"
    local path="$TMPDIR_TEST/$name"
    printf '%s\n' "$content" > "$path"
    echo "$path"
}

assert_fails() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    TESTS=$((TESTS + 1))
    if "$CHECK_SCRIPT" "$TMPDIR_TEST" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

# ─── Test a: api.github.com literal in a .ts file should fail ────────────
create_test_file "bad.ts" 'const url = "https://api.github.com/repos/judgemind/judgemind/issues";'
assert_fails "api.github.com literal in a .ts file is detected"
reset_tmpdir

# ─── Test b: github.com/repos/ literal should fail ───────────────────────
create_test_file "bad.ts" 'const endpoint = "https://github.com/repos/foo/bar/issues";'
assert_fails "github.com/repos/ literal is detected"
reset_tmpdir

# ─── Test c: fetch('https://api.github.com/...') should fail ─────────────
create_test_file "bad.ts" "const res = await fetch('https://api.github.com/repos/foo/bar/issues');"
assert_fails "fetch('https://api.github.com/...') call is detected"
reset_tmpdir

# ─── Test d: Transitive — a.ts imports b.ts which contains api.github.com ─
# The guard does a direct scan of every file under SCAN_DIR. Since b.ts
# contains the forbidden literal, the scan catches it regardless of which
# file imported it. No AST walk is needed — direct scan is sufficient.
create_test_file "a.ts" "import { fetchIssues } from './b';"
create_test_file "b.ts" 'const url = "https://api.github.com/repos/foo/bar/issues";'
assert_fails "Transitive: b.ts with api.github.com is caught by direct scan"
reset_tmpdir

# ─── Test e: Comment-only reference should pass ───────────────────────────
create_test_file "clean.ts" '// Do not call api.github.com from the API container.'
assert_passes "// comment line with api.github.com is not flagged"
reset_tmpdir

# ─── Test f: JSDoc comment should pass ───────────────────────────────────
create_test_file "clean.ts" ' * See github.ts for context on the removed fetch path.'
assert_passes "JSDoc * comment line is not flagged"
reset_tmpdir

# ─── Test g: Unrelated oauth2.googleapis.com fetch call should pass ───────
# This mirrors packages/api/src/graphql/auth-resolvers.ts:308 which uses
# fetch for Google OAuth token exchange — not GitHub. The fetch(.*github)
# regex only matches when the URL contains 'github', so googleapis.com
# must not be flagged.
create_test_file "clean.ts" "const tokenRes = await fetch('https://oauth2.googleapis.com/token', { method: 'POST' });"
assert_passes "fetch to oauth2.googleapis.com is not flagged"
reset_tmpdir

# ─── Test h: Empty directory should pass ─────────────────────────────────
assert_passes "Empty directory passes"

# ─── Test i: .git and node_modules should be excluded ────────────────────
mkdir -p "$TMPDIR_TEST/.git/objects"
printf 'const url = "https://api.github.com/repos/foo/bar";\n' > "$TMPDIR_TEST/.git/objects/badfile"
mkdir -p "$TMPDIR_TEST/node_modules/some-pkg"
printf 'const url = "https://api.github.com/repos/foo/bar";\n' > "$TMPDIR_TEST/node_modules/some-pkg/index.js"
assert_passes ".git and node_modules are excluded"
reset_tmpdir

# ─── Test j: No self-match on ci.yml step name ───────────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden pattern.  If it does, the guard fails
# on its first CI run (see issues #2541/#2542 for the class of bug).
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-api-github-fetch.sh" "yml"

# ─── Summary ──────────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
