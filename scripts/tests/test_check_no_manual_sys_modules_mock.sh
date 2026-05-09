#!/usr/bin/env bash
# test_check_no_manual_sys_modules_mock.sh — Tests for
# scripts/check-no-manual-sys-modules-mock.sh (#4434).
#
# Asserts:
#   1. Exit 0 + clean output on the current repo (the post-#4430 tree
#      already uses the canonical mock_sys_modules helper).
#   2. Exit 1 + Fix block on a synthetic offending file.
#   3. Exit 0 on the spec_from_file_location allowlist case.
#   4. Exit 0 on the test_mock_helpers.py / test_inspect.py filename
#      allowlist case.
#   5. Exit 0 on a file using mock_sys_modules properly (canonical
#      replacement is not flagged).
#   6. Exit 0 on a file with an assignment inside a def/class.
#   7. Exit 0 on a file with the per-file opt-out marker.
#   8. Exit 0 on a file with the per-line noqa marker.
#   9. The error output emits the Fix block with the canonical
#      mock_sys_modules replacement.
#  10. file:line is named in the violation report.
#  11. Direct file-path argument detects violations.
#  12. No self-match on ci.yml step name (post-#2541/#2542 contract).
#
# Usage:
#   scripts/tests/test_check_no_manual_sys_modules_mock.sh
#
# Exit codes:
#   0 — All tests passed.
#   1 — One or more tests failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-no-manual-sys-modules-mock.sh"
FAILURES=0
TESTS=0

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
        out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
        echo "  output was:"
        printf '%s\n' "$out" | sed 's/^/    /'
        FAILURES=$((FAILURES + 1))
    fi
}

reset_tmpdir() {
    rm -rf "$TMPDIR_TEST"/*
    rm -rf "$TMPDIR_TEST"/.[!.]* 2>/dev/null || true
}

if [[ ! -x "$CHECK_SCRIPT" ]]; then
    echo "FAIL: $CHECK_SCRIPT is not executable" >&2
    exit 1
fi

# ─── Test 1: Clean tree (production scope) — exit 0 ──────────────────
# Acceptance criteria verify line: the guard exits 0 against the
# post-#4430 worktree.  Run with no arguments (uses production scope
# scripts/tests/test_*.py).
TESTS=$((TESTS + 1))
if "$CHECK_SCRIPT" > /dev/null 2>&1; then
    echo "PASS: production scope (scripts/tests/test_*.py) passes on this worktree"
else
    echo "FAIL: production scope (scripts/tests/test_*.py) does NOT pass on this worktree"
    echo "  output was:"
    "$CHECK_SCRIPT" 2>&1 | sed 's/^/    /' || true
    FAILURES=$((FAILURES + 1))
fi

# ─── Test 2: Manual sys.modules at module level — exit 1 ─────────────
# The exact pre-#4430 anti-pattern: bare module-level
# ``sys.modules["x"] = MagicMock()`` with a save/replay loop that a
# future author might forget to write.
create_test_file "test_offender.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["structlog"] = MagicMock()
sys.modules["framework"] = MagicMock()

import my_script  # noqa: E402
'
assert_fails "Manual module-level sys.modules[X] = MagicMock() is caught"
reset_tmpdir

# ─── Test 3: Mock-helper-wrapped assignment is allowed — exit 0 ──────
# The canonical replacement.  ``mock_sys_modules`` restores
# automatically, so even though the body contains nominal
# ``sys.modules`` mutation (inside the helper's ``__enter__``), the
# test file itself contains no module-level sys.modules subscript
# assignment outside the helper.
create_test_file "test_canonical.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.path.insert(0, "..")

from tests._mock_helpers import mock_sys_modules  # noqa: E402

with mock_sys_modules({"structlog": MagicMock(), "framework": MagicMock()}):
    import my_script as _script  # noqa: E402
'
assert_passes "mock_sys_modules wrapped import is not flagged"
reset_tmpdir

# ─── Test 4: spec_from_file_location allowlist — exit 0 ──────────────
# Test files that load a hyphen-named script via importlib.util are
# structurally exempt (they register the loaded module in sys.modules
# for dataclass / pickle reconstruction, not to inject a MagicMock).
create_test_file "test_check_foo.py" 'from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "check-foo.py"
spec = importlib.util.spec_from_file_location("check_foo", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["check_foo"] = mod
spec.loader.exec_module(mod)
'
assert_passes "spec_from_file_location-loaded hyphen-named script is allowlisted"
reset_tmpdir

# ─── Test 5: Filename allowlist — test_mock_helpers.py — exit 0 ──────
# Even with a module-level sys.modules assignment, the helper's own
# self-tests are exempt by filename.
create_test_file "test_mock_helpers.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["_mock_helpers_probe_x"] = MagicMock()
'
assert_passes "test_mock_helpers.py filename is allowlisted"
reset_tmpdir

# ─── Test 6: Filename allowlist — test_inspect.py — exit 0 ───────────
# The stdlib-name-collision case is exempt by filename.
create_test_file "test_inspect.py" 'from __future__ import annotations
import sys

_HAD = sys.modules.pop("inspect", None)
import inspect as spotcheck_inspect  # noqa: E402
sys.modules["inspect"] = _HAD
sys.modules["spotcheck_inspect"] = spotcheck_inspect
'
assert_passes "test_inspect.py filename is allowlisted"
reset_tmpdir

# ─── Test 7: Assignment inside def is allowed — exit 0 ───────────────
# Module-level scope is the dangerous location.  Assignments inside
# function / class bodies are call-time, not import-time.
create_test_file "test_inside_def.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock


def _setup():
    sys.modules["foo"] = MagicMock()


def test_thing():
    sys.modules["bar"] = MagicMock()
'
assert_passes "Assignments inside def are not flagged"
reset_tmpdir

# ─── Test 8: Assignment inside class body — exit 0 ───────────────────
create_test_file "test_inside_class.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock


class TestSomething:
    def test_method(self):
        sys.modules["foo"] = MagicMock()
'
assert_passes "Assignments inside class methods are not flagged"
reset_tmpdir

# ─── Test 9: Per-file opt-out marker — exit 0 ────────────────────────
create_test_file "test_optout.py" '# manual-sys-modules-mock-allowed: stdlib-name-collision case, see #1234
from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["foo"] = MagicMock()
'
assert_passes "Per-file opt-out marker exempts the file"
reset_tmpdir

# ─── Test 10: Per-file opt-out without reason is not honored — exit 1
# An empty marker (``# manual-sys-modules-mock-allowed:`` with no text
# after) does NOT exempt the file — the reason is required to force a
# deliberate justification at the add site.
create_test_file "test_optout_empty.py" '# manual-sys-modules-mock-allowed:
from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["foo"] = MagicMock()
'
assert_fails "Per-file opt-out marker without reason is rejected"
reset_tmpdir

# ─── Test 11: Per-line noqa marker — exit 0 ──────────────────────────
create_test_file "test_noqa.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["foo"] = MagicMock()  # noqa: manual-sys-modules-mock
'
assert_passes "Per-line noqa marker exempts that single assignment"
reset_tmpdir

# ─── Test 12: Mixed file — flagged + noqa-exempt — exit 1 ────────────
# The noqa exempts only the marked line; an unmarked sibling is still
# flagged.
create_test_file "test_mixed.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["allowed"] = MagicMock()  # noqa: manual-sys-modules-mock
sys.modules["forbidden"] = MagicMock()
'
assert_fails "noqa-exempt + unmarked-violator combo is still caught"
reset_tmpdir

# ─── Test 13: try/except at module level — exit 1 ────────────────────
# A try/except at module level descends into both branches; an
# assignment inside is still module-level effectively.
create_test_file "test_try_except.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

try:
    sys.modules["foo"] = MagicMock()
except Exception:
    sys.modules["bar"] = MagicMock()
'
assert_fails "try/except at module level is descended into"
reset_tmpdir

# ─── Test 14: if-guard at module level — exit 1 ──────────────────────
# An assignment guarded by ``if`` at module level is still effectively
# module-level — it runs at import time if the condition holds.
create_test_file "test_if_guard.py" 'from __future__ import annotations
import os
import sys
from unittest.mock import MagicMock

if os.environ.get("CI"):
    sys.modules["foo"] = MagicMock()
'
assert_fails "if-guarded module-level assignment is caught"
reset_tmpdir

# ─── Test 15: Variable-key form is also flagged — exit 1 ─────────────
# ``sys.modules[varname] = mock`` (Name slice) is the same anti-pattern
# as the literal-string form, just spelled with a variable.
create_test_file "test_var_key.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

mod_name = "foo"
sys.modules[mod_name] = MagicMock()
'
assert_fails "Variable-key sys.modules[name] = MagicMock() is caught"
reset_tmpdir

# ─── Test 16: Empty file passes ──────────────────────────────────────
create_test_file "test_empty.py" ''
assert_passes "Empty test_*.py file passes"
reset_tmpdir

# ─── Test 17: Syntactically broken Python is skipped silently ────────
create_test_file "test_broken.py" 'import sys
def broken(:  # syntax error
    pass
'
assert_passes "Syntactically broken Python is skipped silently"
reset_tmpdir

# ─── Test 18: Read-only sys.modules access is not flagged — exit 0 ───
# ``x = sys.modules["foo"]`` reads from sys.modules but does not
# assign to it.  Only the assignment shape is the anti-pattern.
create_test_file "test_read_only.py" 'from __future__ import annotations
import sys

if "foo" in sys.modules:
    saved = sys.modules["foo"]
'
assert_passes "Read-only sys.modules access is not flagged"
reset_tmpdir

# ─── Test 19: del sys.modules[...] is not flagged — exit 0 ───────────
# ``del sys.modules["foo"]`` removes an entry but does not assign a
# mock — the anti-pattern is leaking a mock, not removing one.
create_test_file "test_del.py" 'from __future__ import annotations
import sys

if "foo" in sys.modules:
    del sys.modules["foo"]
'
assert_passes "del sys.modules[...] is not flagged"
reset_tmpdir

# ─── Test 20: Non-test_*.py files are out of scope — exit 0 ──────────
# The wrapper only scans test_*.py files at the top level.  A
# helper file (e.g. _mock_helpers.py) with module-level sys.modules
# assignments would NOT be picked up.  This protects the helper's own
# mutations of sys.modules from being incorrectly flagged.
create_test_file "_mock_helpers.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

# This file is NOT named test_*, so the wrapper skips it.
sys.modules["unrelated"] = MagicMock()
'
assert_passes "Non-test_*.py files are out of scope"
reset_tmpdir

# ─── Test 21: Fix block + violation report shape ─────────────────────
# Per the Fix-block contract (docs/agent/code-standards.md
# §Hygiene-check guards: Fix-block contract), the guard must emit a
# copy-pasteable Fix block on the violation path.  Build a violating
# fixture and assert the Fix block content + naming convention.
create_test_file "test_fixblock.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["foo"] = MagicMock()
'
TESTS=$((TESTS + 1))
err_out=$("$CHECK_SCRIPT" "$TMPDIR_TEST" 2>&1 || true)
if printf '%s' "$err_out" | grep -q '^[[:space:]]*Fix:' \
   && printf '%s' "$err_out" | grep -q 'mock_sys_modules' \
   && printf '%s' "$err_out" | grep -q 'scripts/tests/_mock_helpers.py' \
   && printf '%s' "$err_out" | grep -q 'scripts/tests/README.md'; then
    echo "PASS: error output emits a Fix: block naming mock_sys_modules and the README pattern"
else
    echo "FAIL: error output did not emit the expected Fix: block"
    echo "  output was:"
    printf '%s\n' "$err_out" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 22: file:lineno is named in the violation report ───────────
create_test_file "src/test_named.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

sys.modules["foo"] = MagicMock()
'
TESTS=$((TESTS + 1))
output=$("$CHECK_SCRIPT" "$TMPDIR_TEST/src" 2>&1 || true)
if printf '%s' "$output" | grep -qE 'src/test_named\.py:[0-9]+:'; then
    echo "PASS: error output names file:line of violation"
else
    echo "FAIL: error output did not include file:line for violation"
    echo "  output was:"
    printf '%s\n' "$output" | sed 's/^/    /'
    FAILURES=$((FAILURES + 1))
fi
reset_tmpdir

# ─── Test 23: Direct file-path argument detects violations ───────────
TESTS=$((TESTS + 1))
single_file="$TMPDIR_TEST/test_single.py"
mkdir -p "$TMPDIR_TEST"
printf 'import sys\nfrom unittest.mock import MagicMock\nsys.modules["foo"] = MagicMock()\n' \
    > "$single_file"
if "$CHECK_SCRIPT" "$single_file" > /dev/null 2>&1; then
    echo "FAIL: Direct test_*.py file path detects violations (expected failure, got success)"
    FAILURES=$((FAILURES + 1))
else
    echo "PASS: Direct test_*.py file path detects violations"
fi
reset_tmpdir

# ─── Test 24: Nested with mock_sys_modules inside def — exit 0 ───────
# A def body that itself contains ``with mock_sys_modules(...)`` is
# fine — the helper restores even from inside a function.
create_test_file "test_nested_helper.py" 'from __future__ import annotations
import sys
from unittest.mock import MagicMock

from tests._mock_helpers import mock_sys_modules


def test_thing():
    with mock_sys_modules({"foo": MagicMock()}):
        sys.modules["foo"] = MagicMock()  # noqa: manual-sys-modules-mock
'
assert_passes "with mock_sys_modules inside def is not flagged (def body is call-time)"
reset_tmpdir

# ─── Test 25: No self-match on ci.yml step name ──────────────────────
# The step name in .github/workflows/ci.yml that runs this guard must
# not itself contain the forbidden token.  See
# docs/agent/code-standards.md §Hygiene-check CI steps and #2541/#2542.
# shellcheck source=./_guard_self_match_helpers.sh
source "$SCRIPT_DIR/tests/_guard_self_match_helpers.sh"
assert_no_self_match_on_ci_step_name \
    "scripts/check-no-manual-sys-modules-mock.sh" "py"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "Results: $((TESTS - FAILURES))/$TESTS passed"

if [[ $FAILURES -gt 0 ]]; then
    exit 1
fi
exit 0
