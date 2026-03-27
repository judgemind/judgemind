#!/usr/bin/env bash
# test_check_removed_exports.sh — Regression tests for check-removed-exports.sh
#
# Creates a temporary git scenario with removed exports and stale imports to
# verify that the checker correctly detects broken imports and passes on clean
# branches.
#
# Usage:
#   scripts/tests/test_check_removed_exports.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

FAILURES=0
TESTS=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-removed-exports.sh"

# We need a temp directory for a fake git repo
TMPDIR_BASE="${REPO_ROOT}/tmp"
mkdir -p "$TMPDIR_BASE"
TEST_REPO="$TMPDIR_BASE/_test_removed_exports_repo"

cleanup() {
    rm -rf "$TEST_REPO"
}
trap cleanup EXIT

assert_fails() {
    local desc="$1"
    shift
    TESTS=$((TESTS + 1))
    if "$@" > /dev/null 2>&1; then
        echo "FAIL: $desc (expected failure, got success)"
        FAILURES=$((FAILURES + 1))
    else
        echo "PASS: $desc"
    fi
}

assert_passes() {
    local desc="$1"
    shift
    TESTS=$((TESTS + 1))
    if "$@" > /dev/null 2>&1; then
        echo "PASS: $desc"
    else
        echo "FAIL: $desc (expected success, got failure)"
        FAILURES=$((FAILURES + 1))
    fi
}

# ─── Setup: create a test git repo with a base commit ─────────────────────
rm -rf "$TEST_REPO"
mkdir -p "$TEST_REPO/packages/mylib/src" "$TEST_REPO/packages/mylib/tests" "$TEST_REPO/scripts"

cd "$TEST_REPO"
git init --quiet
git config user.email "test@test.com"
git config user.name "Test Runner"
git checkout -b main --quiet

# Copy the check script into the test repo so it resolves REPO_ROOT correctly
mkdir -p scripts
cp "$CHECK_SCRIPT" scripts/check-removed-exports.sh
chmod +x scripts/check-removed-exports.sh

# Create a Python module with some exports
cat > packages/mylib/src/utils.py << 'PYEOF'
def helper_function():
    return "help"

def another_function():
    return "another"

class MyClass:
    pass

MY_CONSTANT = 42
PYEOF

# Create a file that imports from the module
cat > packages/mylib/tests/test_utils.py << 'PYEOF'
from mylib.src.utils import helper_function, another_function, MyClass
PYEOF

# Create another file that also imports
cat > packages/mylib/src/consumer.py << 'PYEOF'
from mylib.src.utils import helper_function
PYEOF

git add -A
git commit -m "initial commit" --quiet

# ─── Test 1: No changes — should pass ─────────────────────────────────────
git checkout -b test-branch-1 --quiet
assert_passes "No changes passes cleanly" scripts/check-removed-exports.sh main

# ─── Test 2: Remove a function, stale import exists — should fail ──────────
git checkout main --quiet
git branch -D test-branch-1 --quiet 2>/dev/null || true
git checkout -b test-branch-2 --quiet

# Remove helper_function from utils.py
cat > packages/mylib/src/utils.py << 'PYEOF'
def another_function():
    return "another"

class MyClass:
    pass

MY_CONSTANT = 42
PYEOF

git add packages/mylib/src/utils.py
git commit -m "remove helper_function" --quiet

assert_fails "Detects stale import of removed function" scripts/check-removed-exports.sh main

# ─── Test 3: Remove a function AND update all import sites — should pass ───
git checkout main --quiet
git branch -D test-branch-2 --quiet 2>/dev/null || true
git checkout -b test-branch-3 --quiet

# Remove helper_function from utils.py
cat > packages/mylib/src/utils.py << 'PYEOF'
def another_function():
    return "another"

class MyClass:
    pass

MY_CONSTANT = 42
PYEOF

# Update all import sites
cat > packages/mylib/tests/test_utils.py << 'PYEOF'
from mylib.src.utils import another_function, MyClass
PYEOF

cat > packages/mylib/src/consumer.py << 'PYEOF'
from mylib.src.utils import another_function
PYEOF

git add -A
git commit -m "remove helper_function and update imports" --quiet

assert_passes "Passes when all import sites are updated" scripts/check-removed-exports.sh main

# ─── Test 4: Remove a class, stale import exists — should fail ─────────────
git checkout main --quiet
git branch -D test-branch-3 --quiet 2>/dev/null || true
git checkout -b test-branch-4 --quiet

# Remove MyClass from utils.py
cat > packages/mylib/src/utils.py << 'PYEOF'
def helper_function():
    return "help"

def another_function():
    return "another"

MY_CONSTANT = 42
PYEOF

git add packages/mylib/src/utils.py
git commit -m "remove MyClass" --quiet

assert_fails "Detects stale import of removed class" scripts/check-removed-exports.sh main

# ─── Test 5: Rename (remove + re-add) — should pass ───────────────────────
git checkout main --quiet
git branch -D test-branch-4 --quiet 2>/dev/null || true
git checkout -b test-branch-5 --quiet

# Rename helper_function to helper_func (remove old, add new)
cat > packages/mylib/src/utils.py << 'PYEOF'
def helper_func():
    return "help"

def another_function():
    return "another"

class MyClass:
    pass

MY_CONSTANT = 42
PYEOF

git add packages/mylib/src/utils.py
git commit -m "rename helper_function to helper_func" --quiet

# Note: this still has the old import, but the script should detect it
# because helper_function was removed and NOT re-added with the same name.
# Actually helper_func != helper_function, so helper_function IS truly removed.
# The stale import of helper_function should be caught.
assert_fails "Detects stale import when function is renamed (old name removed)" scripts/check-removed-exports.sh main

# ─── Test 6: Remove a constant, stale import — should fail ────────────────
git checkout main --quiet
git branch -D test-branch-5 --quiet 2>/dev/null || true
git checkout -b test-branch-6 --quiet

# Remove MY_CONSTANT and add a file that imports it
cat > packages/mylib/src/other.py << 'PYEOF'
from mylib.src.utils import MY_CONSTANT
PYEOF
git add packages/mylib/src/other.py
git commit -m "add other.py that imports MY_CONSTANT" --quiet

# Now go back to main and re-branch so other.py exists in both
git checkout main --quiet
git merge test-branch-6 --quiet
git branch -D test-branch-6 --quiet 2>/dev/null || true
git checkout -b test-branch-6b --quiet

# Remove MY_CONSTANT
cat > packages/mylib/src/utils.py << 'PYEOF'
def helper_function():
    return "help"

def another_function():
    return "another"

class MyClass:
    pass
PYEOF

git add packages/mylib/src/utils.py
git commit -m "remove MY_CONSTANT" --quiet

assert_fails "Detects stale import of removed constant" scripts/check-removed-exports.sh main

# ─── Summary ──────────────────────────────────────────────────────────────
cd "$REPO_ROOT"
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
