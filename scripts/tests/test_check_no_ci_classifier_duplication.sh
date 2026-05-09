#!/usr/bin/env bash
# test_check_no_ci_classifier_duplication.sh — Tests for
# scripts/check-no-ci-classifier-duplication.sh (#4417).
#
# Asserts:
#   1. Exit 0 + "All clean" on a tree containing only the canonical
#      sources (allowlisted files).
#   2. Exit 1 + Fix-block on a tree that introduces a duplicate
#      Python frozenset literal in a non-allowlisted file.
#   3. Exit 1 on a tree that introduces a duplicate jq pattern in a
#      non-allowlisted shell script.
#
# Usage:
#   scripts/tests/test_check_no_ci_classifier_duplication.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$SCRIPT_DIR/check-no-ci-classifier-duplication.sh"
FAILURES=0
TESTS=0

. "$SCRIPT_DIR/tests/_temp_cleanup_helpers.sh"

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

if [[ ! -x "$GUARD" ]]; then
    echo "FAIL: $GUARD is not executable" >&2
    exit 1
fi

# ── Test 1: clean repo (current tree) — exit 0 ─────────────────────────

if "$GUARD" >/dev/null 2>&1; then
    pass "clean tree exits 0"
else
    fail "clean tree exits 0" "guard exited non-zero against current repo"
fi

# ── Test 2: introduce a Python frozenset duplicate — exit 1 ───────────

WORK=$(mktemp -d)
register_temp_dir "$WORK"

# Build a minimal scannable tree: scripts/ with the guard + preflight
# infra, plus a violating file.
mkdir -p "$WORK/scripts"
cp "$GUARD" "$WORK/scripts/check-no-ci-classifier-duplication.sh"
cp "$REPO_ROOT/scripts/preflight.sh" "$WORK/scripts/preflight.sh"

mkdir -p "$WORK/scripts/dispatcher"
# A violating file that spells out the conclusion vocabulary as a
# Python frozenset — exactly the shape the canonical sources use, but
# in a non-allowlisted location.
cat > "$WORK/scripts/dispatcher/some_other_module.py" <<'PYEOF'
"""Hypothetical new module that re-spells the CI vocabulary."""

_FAILURES = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED"})
PYEOF

if "$WORK/scripts/check-no-ci-classifier-duplication.sh" "$WORK" >"$WORK/out" 2>&1; then
    fail "python_frozenset_duplicate flagged" \
        "guard exited 0 but should have exited 1"
else
    if grep -q "scripts/dispatcher/some_other_module.py" "$WORK/out" \
        && grep -q "Fix:" "$WORK/out"; then
        pass "python_frozenset_duplicate flagged with Fix block"
    else
        fail "python_frozenset_duplicate flagged with Fix block" \
            "guard exited non-zero but output missing path/Fix block: $(cat "$WORK/out")"
    fi
fi

# ── Test 3: introduce a jq duplicate — exit 1 ─────────────────────────

WORK2=$(mktemp -d)
register_temp_dir "$WORK2"
mkdir -p "$WORK2/scripts"
cp "$GUARD" "$WORK2/scripts/check-no-ci-classifier-duplication.sh"
cp "$REPO_ROOT/scripts/preflight.sh" "$WORK2/scripts/preflight.sh"

# A violating shell script that re-spells the jq conclusion vocabulary.
cat > "$WORK2/scripts/some_dashboard.sh" <<'SHEOF'
#!/usr/bin/env bash
# Hypothetical operator dashboard that re-implements the jq classifier.
classify() {
    jq -r '
        if (.conclusion == "FAILURE" or .conclusion == "TIMED_OUT") then "red"
        else "ok" end
    '
}
SHEOF
chmod +x "$WORK2/scripts/some_dashboard.sh"

if "$WORK2/scripts/check-no-ci-classifier-duplication.sh" "$WORK2" >"$WORK2/out" 2>&1; then
    fail "jq_duplicate flagged" "guard exited 0 but should have exited 1"
else
    if grep -q "scripts/some_dashboard.sh" "$WORK2/out"; then
        pass "jq_duplicate flagged"
    else
        fail "jq_duplicate flagged" \
            "guard exited non-zero but output missing path: $(cat "$WORK2/out")"
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────

echo ""
echo "Ran $TESTS tests, $FAILURES failed."
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
