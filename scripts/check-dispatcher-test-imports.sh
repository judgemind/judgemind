#!/usr/bin/env bash
# check-dispatcher-test-imports.sh — Forbid the absolute-package import
# shape (`from scripts.dispatcher import ...` / `import scripts.dispatcher`)
# in `scripts/dispatcher/tests/*.py`. Use the sibling-import pattern peer
# tests use instead (#4429).
#
# Background
# ----------
#
# `scripts/dispatcher/tests/*.py` files that need to import the daemon
# (or any other dispatcher sibling module) MUST use the pattern peer
# tests use:
#
#     _SCRIPTS = Path(__file__).resolve().parents[2]
#     if str(_SCRIPTS) not in sys.path:
#         sys.path.insert(0, str(_SCRIPTS))
#     from dispatcher import daemon  # noqa: E402
#
# NOT:
#
#     from scripts.dispatcher import daemon
#
# The latter shape works locally because pytest collection without an
# explicit `rootdir` makes `scripts/dispatcher/tests/test_*.py` run
# with `dispatcher.tests.*` as their module path, and
# `scripts.dispatcher.daemon` happens to resolve from cwd. But
# `pytest scripts/dispatcher/tests/` from CI runs from the repo root
# with no `scripts/__init__.py`, so `scripts.dispatcher` cannot resolve
# and pytest raises `ModuleNotFoundError: No module named 'scripts'`.
#
# Hit by #4417 / PR #4425 — the new `test_ci_classifier_consistency.py`
# initially used `from scripts.dispatcher import daemon` for its
# `_extract_failing_jobs` parity test. CI failed; locally the suite
# passed; one extra commit (`fix(test): match peer dispatcher-test
# import pattern`) was needed to align the test with the peer pattern.
# This guard catches the regression at pre-push / CI rather than
# burning ~10 minutes of CI iteration.
#
# Scope is intentionally confined to `scripts/dispatcher/tests/` —
# `packages/*/tests/` use a different import resolution (the package's
# own `pyproject.toml` makes `from packages.X` and `from .X` both
# work), so this rule does not apply there.
#
# Usage:
#   scripts/check-dispatcher-test-imports.sh          # scan repo's
#                                                       scripts/dispatcher/tests/
#   scripts/check-dispatcher-test-imports.sh [dir]    # scan a directory
#                                                       (used by tests)
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more files use the forbidden import shape outside the
#       allowlist. Prints a Fix block per the
#       docs/dx/check-script-fix-block-coverage.md contract showing the
#       canonical `parents[2]` sys.path push + `from dispatcher import X`
#       replacement.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_ROOT="${1:-$REPO_ROOT}"
TESTS_DIR="$SCAN_ROOT/scripts/dispatcher/tests"

if [ ! -d "$TESTS_DIR" ]; then
    # Nothing to scan — caller pointed us at a tree without dispatcher
    # tests. Treat as clean (the CI shard only runs this when scripts/
    # changed; an empty tree is a no-op).
    echo "All clean — no scripts/dispatcher/tests/ directory under $SCAN_ROOT."
    exit 0
fi

# ─── Patterns ────────────────────────────────────────────────────────────
#
# Pattern 1 — `from scripts.dispatcher ...` at the start of a line
# (allowing leading whitespace for indented imports inside try/except).
FROM_PATTERN='^[[:space:]]*from[[:space:]]+scripts\.dispatcher\b'

# Pattern 2 — `import scripts.dispatcher` at the start of a line.
IMPORT_PATTERN='^[[:space:]]*import[[:space:]]+scripts\.dispatcher\b'

# ─── Allowlist — files that legitimately use the absolute path ──────────
# None today. New entries here should require a CLAUDE.md or
# docs/agent/code-standards.md justification — this guard exists
# precisely because there is no good reason for the absolute shape in
# this directory.
ALLOWLIST=()

# ─── Scan ───────────────────────────────────────────────────────────────

is_allowlisted() {
    local path="$1"
    local rel="${path#"$SCAN_ROOT/"}"
    local entry
    for entry in "${ALLOWLIST[@]+"${ALLOWLIST[@]}"}"; do
        if [[ "$rel" == "$entry" ]]; then
            return 0
        fi
    done
    return 1
}

declare -a violation_lines=()
violations=0

PATTERNS=(
    "$FROM_PATTERN"
    "$IMPORT_PATTERN"
)

# Only look at *.py files directly under scripts/dispatcher/tests/.
# Subdirectories (fixtures/) hold non-Python data today; if a future
# fixture needs Python, the caller can extend the find depth.
#
# Use a while-read loop rather than mapfile/readarray — bash 3.2 (macOS
# default) does not have mapfile, and check-bash-compat.sh guards
# against new uses (see docs/agent/code-standards.md).
test_files=()
while IFS= read -r path; do
    [[ -n "$path" ]] && test_files+=("$path")
done < <(LC_ALL=C find "$TESTS_DIR" -maxdepth 1 -type f -name '*.py' | LC_ALL=C sort)

for path in "${test_files[@]+"${test_files[@]}"}"; do
    if is_allowlisted "$path"; then
        continue
    fi
    for pat in "${PATTERNS[@]}"; do
        # grep -nE prints "<lineno>:<content>"; prepend the path so the
        # output matches the "<path>:<lineno>:<content>" shape callers
        # expect from sibling guards.
        matches=$(grep -nE "$pat" "$path" 2>/dev/null || true)
        if [[ -z "$matches" ]]; then
            continue
        fi
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            violation_lines+=("$path:$line")
            violations=$((violations + 1))
        done <<< "$matches"
    done
done

if [[ $violations -gt 0 ]]; then
    echo "ERROR: scripts/dispatcher/tests/*.py files use the absolute"
    echo "'from scripts.dispatcher ...' / 'import scripts.dispatcher'"
    echo "import shape. This works under pytest's local default rootdir"
    echo "but fails in CI as 'ModuleNotFoundError: No module named"
    echo "'scripts'' (#4417 / PR #4425)."
    echo ""
    echo "Violations:"
    for line in "${violation_lines[@]}"; do
        echo "    $line"
    done
    echo ""
    echo "Fix:"
    echo "  Replace the absolute import with the sibling-package pattern"
    echo "  peer dispatcher tests use (e.g."
    echo "  scripts/dispatcher/tests/test_ci_classifier_consistency.py):"
    echo ""
    echo "      import sys"
    echo "      from pathlib import Path"
    echo ""
    echo "      _SCRIPTS = Path(__file__).resolve().parents[2]"
    echo "      if str(_SCRIPTS) not in sys.path:"
    echo "          sys.path.insert(0, str(_SCRIPTS))"
    echo ""
    echo "      from dispatcher import daemon  # noqa: E402"
    echo ""
    echo "  (Replace 'daemon' with the specific module you need —"
    echo "  phase_transitions, agent_runtime, etc.)"
    echo ""
    echo "  Why: pytest collection without an explicit rootdir makes the"
    echo "  test files run with 'dispatcher.tests.*' as their module path,"
    echo "  so 'from dispatcher import X' resolves once 'scripts/' is on"
    echo "  sys.path. The absolute 'scripts.dispatcher' shape relies on"
    echo "  cwd-based resolution that breaks the moment CI invokes"
    echo "  pytest from the repo root."
    echo ""
    echo "  If a new test legitimately needs the absolute path (very"
    echo "  unlikely), add the path to the ALLOWLIST array in this script."
    exit 1
fi

echo "All clean — scripts/dispatcher/tests/*.py use the sibling-import pattern."
exit 0
