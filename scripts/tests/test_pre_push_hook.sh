#!/usr/bin/env bash
# Regression test for #2487: the pre-push hook reads git's four pre-push
# stdin fields in the correct order.
#
# Git's pre-push stdin format is (see `man githooks`):
#   <local ref> <local sha> <remote ref> <remote sha>
#
# Before #2487 the hook read these as `local_ref local_sha remote_sha _rest`,
# i.e. field 3 (a ref name) was misidentified as a SHA. Two consequences:
#
#   1. The "new branch" detection (`remote_sha == 0000...`) never fired,
#      because field 3 is always a ref name.
#   2. On a first push of a brand-new branch, `git diff <remote-ref> <local-sha>`
#      resolved the remote ref locally (if at all) and produced an empty
#      changed-files list, causing all downstream checks to be silently
#      skipped.
#
# This test asserts both behaviours are now correct by driving the hook
# through a minimal temp git repository.
#
# Run:
#   scripts/tests/test_pre_push_hook.sh
#
# Exits non-zero if any assertion fails.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO_ROOT/.githooks/pre-push"

if [ ! -x "$HOOK" ]; then
    echo "FAIL: hook not found or not executable at $HOOK" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Build a minimal "remote" git repo with an initial commit on main.
# The hook calls `git fetch "$remote" main`, so we need a working origin.
REMOTE="$TMPDIR/remote.git"
WORK="$TMPDIR/work"
git init --quiet --bare "$REMOTE"

# Build a local repo that tracks the remote.
git init --quiet "$WORK"
git -C "$WORK" config user.email "test@example.com"
git -C "$WORK" config user.name "Test"
git -C "$WORK" config commit.gpgsign false
git -C "$WORK" remote add origin "$REMOTE"

# Seed an initial commit on main so `git fetch origin main` succeeds.
echo "# test" > "$WORK/README.md"
git -C "$WORK" add README.md
git -C "$WORK" commit --quiet -m "initial"
git -C "$WORK" branch -M main
git -C "$WORK" push --quiet origin main

# Create a feature branch with a Python file that exercises the hook's
# per-package detection logic. The file is ruff-clean so the test is not
# sensitive to ruff installation state — the hook's "checking Python
# package 'testpkg'..." log line is emitted as soon as the package is
# detected, before any ruff invocation. That log line is the assertion
# target for tests 1 and 2.
git -C "$WORK" checkout --quiet -b feature-new-branch
mkdir -p "$WORK/packages/testpkg/src" "$WORK/packages/testpkg/tests"
cat > "$WORK/packages/testpkg/pyproject.toml" <<'PYPROJ'
[project]
name = "testpkg"
version = "0.0.1"
PYPROJ
cat > "$WORK/packages/testpkg/src/good.py" <<'PY'
"""A minimal, ruff-clean module for pre-push hook testing."""


def greet() -> str:
    """Return a greeting."""
    return "hello"
PY
git -C "$WORK" add packages/testpkg
git -C "$WORK" commit --quiet -m "feat: add testpkg"

FEATURE_SHA="$(git -C "$WORK" rev-parse HEAD)"
ZERO_SHA="0000000000000000000000000000000000000000"

# ---------------------------------------------------------------
# Test 1: new-branch push detects changed files
# ---------------------------------------------------------------
# Stdin for a new-branch push has remote_sha = all zeros.
#   local_ref           local_sha      remote_ref          remote_sha(=zeros)
NEW_BRANCH_STDIN="refs/heads/feature-new-branch $FEATURE_SHA refs/heads/feature-new-branch $ZERO_SHA"

# Run the hook. We expect it to log "pre-push: checking Python package 'testpkg'..."
# because the new-branch fallback must resolve the merge-base with main and
# diff against it, finding the new src/bad.py.
echo "[test 1] new-branch push — hook should see changed files and run ruff"
new_out="$(cd "$WORK" && echo "$NEW_BRANCH_STDIN" | "$HOOK" origin "$REMOTE" 2>&1 || true)"

if echo "$new_out" | grep -q "checking Python package 'testpkg'"; then
    echo "  PASS: hook detected testpkg on new-branch push"
else
    echo "  FAIL: hook did NOT detect testpkg on new-branch push" >&2
    echo "--- hook output ---" >&2
    echo "$new_out" >&2
    echo "--- end ---" >&2
    exit 1
fi

# ---------------------------------------------------------------
# Test 2: existing-branch push still detects changed files
# ---------------------------------------------------------------
# Simulate an existing-branch push where remote_sha points to the previous
# commit on main (before testpkg was added).
PREV_SHA="$(git -C "$WORK" rev-parse main)"
EXISTING_BRANCH_STDIN="refs/heads/feature-new-branch $FEATURE_SHA refs/heads/feature-new-branch $PREV_SHA"

echo "[test 2] existing-branch push — hook should still see changed files"
existing_out="$(cd "$WORK" && echo "$EXISTING_BRANCH_STDIN" | "$HOOK" origin "$REMOTE" 2>&1 || true)"

if echo "$existing_out" | grep -q "checking Python package 'testpkg'"; then
    echo "  PASS: hook detected testpkg on existing-branch push"
else
    echo "  FAIL: hook did NOT detect testpkg on existing-branch push" >&2
    echo "--- hook output ---" >&2
    echo "$existing_out" >&2
    echo "--- end ---" >&2
    exit 1
fi

# ---------------------------------------------------------------
# Test 3: delete push (local_sha = zeros) is skipped cleanly
# ---------------------------------------------------------------
DELETE_STDIN="refs/heads/feature-new-branch $ZERO_SHA refs/heads/feature-new-branch $PREV_SHA"
echo "[test 3] delete push — hook should skip without error"
delete_out="$(cd "$WORK" && echo "$DELETE_STDIN" | "$HOOK" origin "$REMOTE" 2>&1 || true)"

# Delete pushes should not run any per-package checks. The hook exits 0.
if echo "$delete_out" | grep -q "checking Python package"; then
    echo "  FAIL: hook ran checks on a delete push" >&2
    echo "--- hook output ---" >&2
    echo "$delete_out" >&2
    echo "--- end ---" >&2
    exit 1
fi
echo "  PASS: hook skipped delete push"

echo ""
echo "All pre-push field-order tests passed."
