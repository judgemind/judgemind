#!/usr/bin/env bash
# check-no-manual-sys-modules-mock.sh — Forbid the manual
# ``sys.modules["<name>"] = <mock>`` mocking anti-pattern at module
# level in scripts/tests/test_*.py (#4434).
#
# Background — the manual save/replay footgun
# ─────────────────────────────────────────────
# Pre-#4430, every scripts/tests/test_*.py file that needed to mock
# unavailable modules (psycopg, structlog, framework.*, ingestion.*)
# maintained its own ~15-line save/restore boilerplate around the
# import.  A single forgotten restore loop pollutes sys.modules for
# every test collected later in the same pytest process — the bug
# class #4426 caught and pinned with test_scripts_tests_isolation.py.
#
# PR #4430 introduced scripts/tests/_mock_helpers.py::mock_sys_modules
# as the canonical context-manager replacement.  The README now leads
# with the new pattern and marks the manual save/replay form as legacy.
# But nothing previously stopped a future test author from writing a
# new ``_modules_to_mock = {...}`` + bare ``sys.modules[name] = mock``
# block that forgets the restore loop — test_scripts_tests_isolation.py
# catches the failure mode (a leaked MagicMock in sys.modules), not the
# anti-pattern (manual mocking that omits restore).
#
# This guard catches the anti-pattern at PR time, before the failure
# mode reaches CI.
#
# What this guard scans
# ─────────────────────
# Every ``scripts/tests/test_*.py`` file (no recursion).  A file is
# flagged when it contains a module-level assignment of the shape
# ``sys.modules["<name>"] = <expr>`` that is NOT inside:
#
#   - a ``with mock_sys_modules(...)`` block (the canonical replacement)
#   - a ``def`` function body
#   - a ``class`` body
#
# Allowlist
# ─────────
# Two structural carve-outs:
#
#   1. Files calling ``importlib.util.spec_from_file_location(...)``
#      anywhere in the module — these load hyphen-named scripts as
#      Python modules and register them in sys.modules so dataclass /
#      pickle reconstruction works.  Examples:
#      test_audit_shipped_zombies.py, test_check_shipped_pr_*.py,
#      test_check_ci_job_skipped.py, test_check_script_headers.py,
#      test_check_ci_guards_skip_list_coverage.py,
#      test_check_nullable_column_reads.py,
#      test_check_migration_number_collision.py,
#      test_check_graphql_nullability_drift.py.
#
#   2. Filename allowlist (small, hardcoded):
#      - test_mock_helpers.py — the helper's own self-tests.
#      - test_inspect.py — exercises scripts/spotcheck/inspect.py
#        which collides with stdlib ``inspect``; legitimately requires
#        module-level sys.modules rebinds.
#
# Per-file opt-out: add a ``# manual-sys-modules-mock-allowed: <reason>``
# comment in the first 20 lines.  Per-line opt-out: add a
# ``# noqa: manual-sys-modules-mock`` trailing comment on the
# assignment.
#
# Issue #4434.  Parent: #4430.
#
# Usage
# ─────
#
#   scripts/check-no-manual-sys-modules-mock.sh
#       # scan scripts/tests/test_*.py (production scope)
#   scripts/check-no-manual-sys-modules-mock.sh [path]
#       # scan a specific file or directory (used by tests)
#
# Exit codes
# ──────────
#
#   0 — No violations found.
#   1 — At least one file uses the manual sys.modules mocking pattern
#       at module level outside an allowlisted shape.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Determine scan targets ──────────────────────────────────────────
# With no argument: scan scripts/tests/test_*.py (the production
# scope per the issue).
# With an argument: scan that path (file or directory) — used by tests.
py_files=()
if [[ $# -eq 0 ]]; then
    while IFS= read -r found; do
        py_files+=("$found")
    done < <(find "$REPO_ROOT/scripts/tests" -maxdepth 1 -type f -name 'test_*.py' \
        | LC_ALL=C sort)
else
    target="$1"
    if [[ -f "$target" && "$target" == *.py ]]; then
        py_files+=("$target")
    elif [[ -d "$target" ]]; then
        # When given a directory, scan only test_*.py files at the
        # top level.  This mirrors the production scope and keeps the
        # test fixture behaviour consistent.
        while IFS= read -r found; do
            py_files+=("$found")
        done < <(find "$target" -maxdepth 1 -type f -name 'test_*.py' \
            | LC_ALL=C sort)
    fi
fi

# Empty file list → no violations possible.
if [[ ${#py_files[@]} -eq 0 ]]; then
    exit 0
fi

# ─── Run the AST scanner ─────────────────────────────────────────────
# The Python scanner emits one line per violation in the form:
#   <path>:<lineno>:<snippet>
python_output="$(python3 "$REPO_ROOT/scripts/check_no_manual_sys_modules_mock.py" \
    "${py_files[@]}")"

# ─── Report violations ───────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: scripts/tests/test_*.py file(s) use the manual"
echo "       sys.modules mocking pattern at module level — the legacy"
echo "       save/replay shape that #4430 superseded with"
echo "       scripts/tests/_mock_helpers.py::mock_sys_modules."
echo ""
echo "  The manual pattern is fragile: a single forgotten restore loop"
echo "  leaks the MagicMock into every test collected later in the"
echo "  same pytest process and breaks unrelated tests that import the"
echo "  real module (the #4426 failure mode pinned by"
echo "  test_scripts_tests_isolation.py)."
echo ""
echo "  Fix: replace the manual save/replay block with the"
echo "  ``mock_sys_modules`` context manager from"
echo "  scripts/tests/_mock_helpers.py.  See"
echo "  scripts/tests/README.md §\"Pattern (mock_sys_modules — current)\""
echo "  for the canonical recipe.  Concretely:"
echo ""
echo "      # Before (manual save/replay — superseded):"
echo "      _modules_to_mock = {\"structlog\": MagicMock(), ...}"
echo "      _saved_modules: dict[str, object] = {}"
echo "      for _mod_name, _mock_mod in _modules_to_mock.items():"
echo "          if _mod_name in sys.modules:"
echo "              _saved_modules[_mod_name] = sys.modules[_mod_name]"
echo "          sys.modules[_mod_name] = _mock_mod"
echo "      import my_script  # noqa: E402"
echo "      for _mod_name in list(_modules_to_mock.keys()):"
echo "          if _mod_name in _saved_modules:"
echo "              sys.modules[_mod_name] = _saved_modules[_mod_name]"
echo "          elif _mod_name in sys.modules:"
echo "              del sys.modules[_mod_name]"
echo ""
echo "      # After (mock_sys_modules — current):"
echo "      from tests._mock_helpers import mock_sys_modules"
echo "      with mock_sys_modules({\"structlog\": MagicMock(), ...}):"
echo "          import my_script as _script  # noqa: E402"
echo ""
echo "  See scripts/tests/test_audit_correctly_labeled_s3_orphans.py"
echo "  for the reference implementation post-#4430."
echo ""
echo "  If a test legitimately needs module-level sys.modules"
echo "  mutation (e.g. the stdlib-name-collision case in"
echo "  test_inspect.py), add either:"
echo "    - a ``# manual-sys-modules-mock-allowed: <reason>`` header"
echo "      comment in the first 20 lines, OR"
echo "    - a ``# noqa: manual-sys-modules-mock`` trailing comment on"
echo "      the specific assignment line."
echo ""
echo "  Violating file(s):"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations module-level sys.modules assignment(s)"
    echo "  outside the mock_sys_modules helper or the documented"
    echo "  allowlist."
    exit 1
fi

exit 0
