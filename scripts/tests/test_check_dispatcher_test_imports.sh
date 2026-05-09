#!/usr/bin/env bash
# test_check_dispatcher_test_imports.sh — Tests for
# scripts/check-dispatcher-test-imports.sh (#4429).
#
# Asserts:
#   1. Exit 0 + "All clean" on the current repo (the canonical sources
#      already use the sibling-import pattern post-#4417).
#   2. Exit 1 + Fix-block on a tree that introduces a
#      `from scripts.dispatcher import daemon` line in a test file.
#   3. Exit 1 on a tree that introduces an
#      `import scripts.dispatcher.daemon` line.
#   4. Exit 0 on a tree that uses the canonical sibling-import pattern
#      (`from dispatcher import daemon`) — false-positive guard.
#
# Usage:
#   scripts/tests/test_check_dispatcher_test_imports.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GUARD="$SCRIPT_DIR/check-dispatcher-test-imports.sh"
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

# ── Test 2: introduce `from scripts.dispatcher import …` — exit 1 ──────

WORK=$(mktemp -d)
register_temp_dir "$WORK"

mkdir -p "$WORK/scripts/dispatcher/tests"
cat > "$WORK/scripts/dispatcher/tests/test_offender.py" <<'PYEOF'
"""Hypothetical new test that uses the absolute import shape."""

from scripts.dispatcher import daemon

assert daemon is not None
PYEOF

if "$GUARD" "$WORK" >"$WORK/out" 2>&1; then
    fail "from_scripts_dispatcher flagged" \
        "guard exited 0 but should have exited 1"
else
    if grep -q "scripts/dispatcher/tests/test_offender.py" "$WORK/out" \
        && grep -q "Fix:" "$WORK/out" \
        && grep -q "from dispatcher import" "$WORK/out"; then
        pass "from_scripts_dispatcher flagged with Fix block"
    else
        fail "from_scripts_dispatcher flagged with Fix block" \
            "guard exited non-zero but output missing path/Fix block: $(cat "$WORK/out")"
    fi
fi

# ── Test 3: introduce `import scripts.dispatcher.daemon` — exit 1 ──────

WORK2=$(mktemp -d)
register_temp_dir "$WORK2"

mkdir -p "$WORK2/scripts/dispatcher/tests"
cat > "$WORK2/scripts/dispatcher/tests/test_other_offender.py" <<'PYEOF'
"""Hypothetical new test using the bare ``import scripts.dispatcher.X`` shape."""

import scripts.dispatcher.daemon
PYEOF

if "$GUARD" "$WORK2" >"$WORK2/out" 2>&1; then
    fail "import_scripts_dispatcher flagged" \
        "guard exited 0 but should have exited 1"
else
    if grep -q "scripts/dispatcher/tests/test_other_offender.py" "$WORK2/out"; then
        pass "import_scripts_dispatcher flagged"
    else
        fail "import_scripts_dispatcher flagged" \
            "guard exited non-zero but output missing path: $(cat "$WORK2/out")"
    fi
fi

# ── Test 4: canonical sibling-import pattern — exit 0 ──────────────────
# The canonical replacement (`from dispatcher import daemon` after a
# parents[2] sys.path push) MUST NOT trip the guard. This catches
# regex-too-broad regressions that would forbid the very shape the
# Fix block recommends.

WORK3=$(mktemp -d)
register_temp_dir "$WORK3"

mkdir -p "$WORK3/scripts/dispatcher/tests"
cat > "$WORK3/scripts/dispatcher/tests/test_canonical.py" <<'PYEOF'
"""Hypothetical new test using the canonical sibling-import pattern."""

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from dispatcher import daemon  # noqa: E402

assert daemon is not None
PYEOF

if "$GUARD" "$WORK3" >"$WORK3/out" 2>&1; then
    pass "canonical sibling-import pattern not flagged"
else
    fail "canonical sibling-import pattern not flagged" \
        "guard exited non-zero on canonical pattern: $(cat "$WORK3/out")"
fi

# ── Summary ────────────────────────────────────────────────────────────

echo ""
echo "Ran $TESTS tests, $FAILURES failed."
if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
