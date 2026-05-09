#!/usr/bin/env bash
# check-no-duplicate-blocked-by-regex.sh — Forbid copies of the canonical
# ``Blocked by #N`` regex outside ``scripts/dispatcher/blocked_by.py``.
#
# Background — why duplicates accrue
# ──────────────────────────────────
# Three call sites historically maintained the same regex independently:
#
#   - ``scripts/dispatcher/daemon.py`` — DispatcherDaemon._parse_blocked_by
#     plus the inline ``re.findall`` inside ``_normalise_issue_record``
#     (the issue-#2989 enrichment path)
#   - ``scripts/dispatcher/agent-runner-entrypoint.sh`` — inline Python shim
#
# PR for #4514 extracted the regex into the shared module
# ``scripts/dispatcher/blocked_by.py`` so all three call sites delegate.
# This guard prevents the next agent who clones one of those files (or
# files a follow-up touching the same conceptual surface) from
# re-introducing a local copy.
#
# Same drift-prevention principle behind the ``parent_issue`` extraction
# (#4508 / PR #4511) and the ``framework.s3_keys`` extraction (#4447 /
# #4456).
#
# What this guard scans
# ─────────────────────
# Every text file under ``scripts/`` and ``packages/`` (.py, .sh) for
# any line that matches the canonical regex shape:
#
#   re-search style:  blocked by:?\\s*\#
#   (also matches the no-colon shape  blocked by\\s*\#  )
#
# We scan for the regex *fragment* (``blocked\s+by`` with hash and
# optional colon nearby) rather than the full anchored form so a copy
# that drops a quantifier or anchor but keeps the Blocked by #N intent
# is still caught. The only legal occurrence is in
# ``scripts/dispatcher/blocked_by.py`` (the canonical home).
#
# Out of scope (#4514): bash-native ``grep``/``sed`` patterns inside
# ``scripts/*.sh`` files (e.g. ``scripts/block-issue.sh``,
# ``scripts/unblock-dependents.sh``). They use the regex idiomatically
# as part of bash text processing — extracting them into Python would
# add invocation overhead without proportional drift-prevention value.
# The .sh files use ``Blocked by #`` as a literal substring grep, not
# as a regex with ``\s+`` / ``:?`` operators, so they don't hit the
# fragment we scan for.
#
# Allowlist
# ─────────
# Files that legitimately need to repeat the regex (none today) opt
# out via a ``# allow-duplicate-blocked-by-regex: <issue-or-PR-ref>``
# marker. The marker must cite the justifying issue or PR.
#
# Issue #4514. Cross-reference: #4508 (sibling guard for ``Parent: #N``),
# docs/agent/issue-authoring.md §"test-side coverage of production rules"
# (the principle), #4346 (Fix-block contract).
#
# Usage
# ─────
#
#   scripts/check-no-duplicate-blocked-by-regex.sh
#       # scan scripts/ and packages/ from REPO_ROOT
#   scripts/check-no-duplicate-blocked-by-regex.sh [scan-root]
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
CANONICAL_RELPATH="scripts/dispatcher/blocked_by.py"

# Allowlist marker — paste this anywhere in the file (within the first
# 50 lines for performance) to opt out:
#   # allow-duplicate-blocked-by-regex: #<issue-or-PR>
ALLOWLIST_MARKER='allow-duplicate-blocked-by-regex:'

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

# Find files that contain the regex fragment. The fragment we look for
# is the regex-form ``blocked by`` with the ``\s`` whitespace operator
# adjacent to ``#`` — narrow enough that bash-native literal-substring
# greps in scripts/*.sh (e.g. ``grep -E "Blocked by #" file``) do NOT
# trip the guard, but the full Python regex shape (with ``\s+`` /
# ``\\s+``) does.
violations=()
for f in "${candidates[@]}"; do
    # Skip the canonical home.
    relpath="${f#"$SCAN_ROOT/"}"
    if [[ "$relpath" == "$CANONICAL_RELPATH" ]]; then
        continue
    fi

    # Look for the regex *fragment* as it appears in Python source —
    # ``blocked by`` followed by literal ``:?\s+#`` (the daemon's
    # colon-optional shape) or literal ``\s+#`` (the entrypoint shim's
    # pre-#4514 no-colon shape). The pattern matches the LITERAL bytes
    # of the regex as it appears in source code: ``[:?]*`` consumes
    # zero or more ``:`` / ``?`` characters in the source text, then
    # ``\\s`` matches the literal two characters ``\s`` (how a regex
    # whitespace operator is spelled in Python / inline-Python source),
    # then ``\+`` matches the literal ``+`` quantifier, then literal
    # ``#``.
    #
    # The escape strategy mirrors ``check-no-duplicate-parent-regex.sh``:
    # we double-escape every regex meta character that we want grep to
    # treat as a literal byte (``\\s`` → ``\s`` in the ERE pattern,
    # which on both BSD and GNU grep matches the literal two-byte
    # sequence ``\s`` — NOT a whitespace class — because of the
    # leading literal ``\``).
    #
    # This pattern is narrow enough that bash-native literal-substring
    # greps in scripts/*.sh (e.g. ``grep -qE "^Blocked by #${BLOCKER}\b"``
    # in scripts/block-issue.sh, ``--search "\"Blocked by #$N\""`` in
    # scripts/unblock-dependents.sh, awk literal-string interpolation
    # in block-issue.sh) do NOT trip the guard, but the full Python
    # regex shape (with the ``\s`` operator) does.
    if ! grep -qE 'blocked by[:?]*\\s\+#' "$f" 2>/dev/null; then
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
echo "ERROR: Found copies of the canonical 'Blocked by #N' regex outside"
echo "       scripts/dispatcher/blocked_by.py."
echo ""
echo "  Issue #4514 extracted the regex into the shared helper module"
echo "  scripts/dispatcher/blocked_by.py so daemon.py (both call sites)"
echo "  and the agent-runner entrypoint shim all delegate. Re-introducing"
echo "  a local copy regresses the maintainability win — a future bug fix"
echo "  in the helper would fix one site but leave the duplicate stale,"
echo "  producing the silent drift class behind the agent-runner-entrypoint"
echo "  no-colon-only regex that pre-#4514 disagreed with the daemon's"
echo "  colon-optional regex."
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
echo "    -matches = re.findall(r\"(?im)^\\s*blocked by:?\\s+#(\\d+)\\s*\$\", body)"
echo "    -return [int(m) for m in matches]"
echo "    +from dispatcher.blocked_by import parse_blocked_by"
echo "    +..."
echo "    +return parse_blocked_by(body)"
echo ""
echo "  Reference implementations:"
echo "    - scripts/dispatcher/daemon.py::DispatcherDaemon._parse_blocked_by"
echo "    - scripts/dispatcher/daemon.py::_normalise_issue_record"
echo "    - scripts/dispatcher/agent-runner-entrypoint.sh (embedded shim)"
echo ""
echo "  If your file genuinely cannot import from blocked_by (e.g., it"
echo "  lives outside scripts/ and cannot extend sys.path), add a marker"
echo "  line citing the justifying issue:"
echo ""
echo "    # allow-duplicate-blocked-by-regex: #<issue-or-PR>"
echo ""
echo "  See #4514 for the full rationale."
exit 1
