#!/usr/bin/env bash
# check-no-duplicate-parent-regex.sh — Forbid copies of the canonical
# ``Parent: #N`` regex outside ``scripts/dispatcher/parent_issue.py``.
#
# Background — why duplicates accrue
# ──────────────────────────────────
# Three call sites historically maintained the same regex independently:
#
#   - ``scripts/dispatcher/daemon.py`` — DispatcherDaemon._parse_parent_issue
#   - ``scripts/dispatcher/agent-runner-entrypoint.sh`` — inline Python shim
#   - ``scripts/_sweep_completed_parents.py`` — _PARENT_RE
#
# PR for #4508 extracted the regex into the shared module
# ``scripts/dispatcher/parent_issue.py`` so all three call sites
# delegate. This guard prevents the next agent who clones one of those
# files (or files a follow-up touching the same conceptual surface)
# from re-introducing a local copy.
#
# Same drift-prevention principle behind the ``framework.s3_keys``
# extraction (#4447 / #4456) and ``check-no-duplicate-framework-helpers.sh``.
#
# What this guard scans
# ─────────────────────
# Every text file under ``scripts/`` and ``packages/`` (.py, .sh) for
# any line that matches the canonical regex shape:
#
#   re-search style:  parent\\s*:\\s*#
#
# We scan for the regex *fragment* (``parent\\s*:\\s*#``) rather than
# the full anchored form so a copy that drops a quantifier or anchor
# but keeps the parent: #N intent is still caught. The only legal
# occurrence is in ``scripts/dispatcher/parent_issue.py`` (the
# canonical home).
#
# Allowlist
# ─────────
# Files that legitimately need to repeat the regex (none today) opt
# out via a ``# allow-duplicate-parent-regex: <issue-or-PR-ref>``
# marker. The marker must cite the justifying issue or PR.
#
# Issue #4508. Cross-reference: docs/agent/issue-authoring.md §"test-side
# coverage of production rules" (the principle), #4456 (sibling guard),
# #4346 (Fix-block contract).
#
# Usage
# ─────
#
#   scripts/check-no-duplicate-parent-regex.sh
#       # scan scripts/ and packages/ from REPO_ROOT
#   scripts/check-no-duplicate-parent-regex.sh [scan-root]
#       # scan a specific tree (used by tests)
#
# Exit codes
# ──────────
#
#   0 — No violations found (regex appears only in the canonical home).
#   1 — At least one file outside the canonical home contains a copy
#       of the regex without the allowlist marker.
#   2 — Usage error (scan-root missing or unreadable).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SCAN_ROOT="${1:-$REPO_ROOT}"

if [[ ! -d "$SCAN_ROOT" ]]; then
    echo "ERROR: scan root not found: $SCAN_ROOT" >&2
    exit 2
fi

# The canonical home for the regex. All other files are violations.
CANONICAL_RELPATH="scripts/dispatcher/parent_issue.py"

# Allowlist marker — paste this anywhere in the file (within the first
# 50 lines for performance) to opt out:
#   # allow-duplicate-parent-regex: #<issue-or-PR>
ALLOWLIST_MARKER='allow-duplicate-parent-regex:'

# Collect candidate files. Limit to .py and .sh (the only file types
# that have historically carried a copy). Skip .venv, node_modules,
# .git, and the test fixture trees.
candidates=()
while IFS= read -r -d '' f; do
    candidates+=("$f")
done < <(
    find "$SCAN_ROOT/scripts" "$SCAN_ROOT/packages" \
        \( -path '*/.venv' -o -path '*/node_modules' -o -path '*/.git' \
           -o -path '*/__pycache__' \) -prune \
        -o -type f \( -name '*.py' -o -name '*.sh' \) -print0 \
        2>/dev/null
)

if [[ ${#candidates[@]} -eq 0 ]]; then
    # No candidate files — vacuously clean.
    exit 0
fi

# Find files that contain the regex fragment.
violations=()
for f in "${candidates[@]}"; do
    # Skip the canonical home.
    relpath="${f#"$SCAN_ROOT/"}"
    if [[ "$relpath" == "$CANONICAL_RELPATH" ]]; then
        continue
    fi

    # Look for the regex fragment ``parent\s*:\s*#`` in any form.
    # The single-quoted-grep pattern is portable across BSD/GNU.
    if ! grep -qE 'parent\\s\*:\\s\*#' "$f" 2>/dev/null; then
        continue
    fi

    # Found a candidate — check for the allowlist marker.
    if head -n 50 "$f" 2>/dev/null | grep -qE "$ALLOWLIST_MARKER"; then
        continue
    fi

    violations+=("$relpath")
done

if [[ ${#violations[@]} -eq 0 ]]; then
    exit 0
fi

# Report violations + Fix block.
echo "ERROR: Found copies of the canonical 'Parent: #N' regex outside"
echo "       scripts/dispatcher/parent_issue.py."
echo ""
echo "  Issue #4508 extracted the regex into the shared helper module"
echo "  scripts/dispatcher/parent_issue.py so daemon.py, the agent-runner"
echo "  entrypoint shim, and scripts/_sweep_completed_parents.py all"
echo "  delegate. Re-introducing a local copy regresses the maintainability"
echo "  win — a future bug fix in the helper would fix one site but leave"
echo "  the duplicate stale, producing the silent drift class behind"
echo "  PR #4453 ↔ #4455."
echo ""
echo "  Violating file(s):"
echo ""
for v in "${violations[@]}"; do
    echo "    $v"
done
echo ""
echo "  Fix: replace the local regex with an import from the canonical"
echo "  helper. The canonical migration:"
echo ""
echo "    -import re"
echo "    -..."
echo "    -match = re.search(r\"(?im)^\\s*parent\\s*:\\s*#(\\d+)\\s*\$\", body)"
echo "    -return int(match.group(1)) if match else None"
echo "    +from dispatcher.parent_issue import parse_parent_issue"
echo "    +..."
echo "    +return parse_parent_issue(body)"
echo ""
echo "  Reference implementations:"
echo "    - scripts/dispatcher/daemon.py::DispatcherDaemon._parse_parent_issue"
echo "    - scripts/_sweep_completed_parents.py (top-level scripts/)"
echo "    - scripts/dispatcher/agent-runner-entrypoint.sh (embedded shim)"
echo ""
echo "  If your file genuinely cannot import from parent_issue (e.g., it"
echo "  lives outside scripts/ and cannot extend sys.path), add a marker"
echo "  line citing the justifying issue:"
echo ""
echo "    # allow-duplicate-parent-regex: #<issue-or-PR>"
echo ""
echo "  See #4508 for the full rationale."
exit 1
