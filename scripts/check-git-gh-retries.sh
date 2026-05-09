#!/usr/bin/env bash
# check-git-gh-retries.sh — Every ``subprocess.run([... "git" | "gh" ...])``
# call site in ``scripts/dispatcher/daemon.py`` must either:
#   (a) route through the shared ``_subprocess_with_retry`` helper, OR
#   (b) carry a nearby ``# cold-path (#3089)`` annotation explaining why
#       a single-failure outcome is acceptable.
#
# Why this check exists
# ---------------------
# Each unretried network subprocess in the daemon hot path is a
# single-transient-failure-cost bug of the same shape as #3053 (fixed
# ``_gh_issue_is_open``) and #3085 (fixed ``_baseline_fetch_origin_main``).
# Issue #3089 did an audit pass and decided per-site: wrap hot-path
# calls in the shared ``_subprocess_with_retry`` helper; annotate
# cold-path calls with a ``# cold-path (#3089): ...`` comment. This
# check enforces that every future author of a new ``git`` / ``gh``
# subprocess.run either picks up the retry helper or documents why not
# — preventing the cascade of flake-induced agent terminations that
# motivated the audit.
#
# Rule
# ----
# Every ``subprocess.run(...)`` call site in ``scripts/dispatcher/daemon.py``
# whose first positional argument resolves to a list literal beginning
# with ``"git"`` or ``"gh"`` MUST have a ``# cold-path (#3089)`` comment
# within ``COLDPATH_WINDOW_LINES`` lines immediately above the call line.
#
# The check deliberately does NOT check for the retry helper wiring —
# the helper itself uses ``subprocess.run(cmd, ...)`` internally, and
# the parametric ``cmd`` arg means the first-literal-element heuristic
# won't fire there. Hot-path sites route through the helper via
# ``self._subprocess_with_retry(cmd, ...)`` (NOT ``subprocess.run``),
# so they disappear from this check's radar entirely after the refactor
# — no false positives. The ``# cold-path (#3089)`` comment annotates
# the surviving direct-subprocess.run sites, which by definition are
# cold-path by construction.
#
# Tracking: issue #3089.
#
# Usage
# -----
#   scripts/check-git-gh-retries.sh            # scan the real daemon.py
#   scripts/check-git-gh-retries.sh [file]     # scan a specific file (tests)
#
# Exit codes
# ----------
#   0 — Every call site has a cold-path annotation within the window.
#   1 — At least one call site is missing a nearby cold-path comment.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_FILE="${1:-$REPO_ROOT/scripts/dispatcher/daemon.py}"

# Window size — 60 lines matches the typical subprocess.run call-site
# shape (a few lines of setup + the call + try/except handler), with
# some slack for docstrings or helper prologue. Override with the
# COLDPATH_WINDOW_LINES env var for ad-hoc experiments; production
# always uses the default.
: "${COLDPATH_WINDOW_LINES:=60}"

# ─── Fast exit if the target file does not exist ─────────────────────────
if [[ ! -f "$TARGET_FILE" ]]; then
    echo "check-git-gh-retries: no target file at $TARGET_FILE — nothing to check."
    exit 0
fi

# ─── Locate candidate call sites ─────────────────────────────────────────
# We use python to parse the AST because a regex-only approach would
# either miss name-bound cmd lists (``cmd = [...]; subprocess.run(cmd, ...)``)
# or produce too many false positives on multi-line calls. The python
# helper emits one line per offending call site: just the line number.
python_output="$(python3 - "$TARGET_FILE" <<'PYEOF'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
tree = ast.parse(path.read_text())


def _literal_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _list_first_literal(node):
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    if not node.elts:
        return None
    return _literal_str(node.elts[0])


def _enclosing_func(line_no):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= line_no <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def _resolve_name(name, before_line):
    """Resolve ``name`` to its most recent list-literal Assign within
    the enclosing function scope. Returns None if the name is a
    function parameter (cannot resolve) or if no in-scope Assign
    binds it to a list literal."""
    scope = _enclosing_func(before_line)
    if scope is None:
        return None
    best = None
    for sub in ast.walk(scope):
        if isinstance(sub, ast.Assign) and sub.lineno < before_line:
            for target in sub.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(sub.value, (ast.List, ast.Tuple)):
                        best = sub.value
    return best


for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    ):
        continue
    if not node.args:
        continue
    first = node.args[0]
    tool = None
    if isinstance(first, (ast.List, ast.Tuple)):
        tool = _list_first_literal(first)
    elif isinstance(first, ast.Name):
        resolved = _resolve_name(first.id, node.lineno)
        if resolved is not None:
            tool = _list_first_literal(resolved)
    if tool in ("git", "gh"):
        print(node.lineno)
PYEOF
)"

# ─── Check each call site ────────────────────────────────────────────────
violations=0
missing=()

if [[ -z "${python_output// /}" ]]; then
    echo "check-git-gh-retries: no git/gh subprocess.run sites in $TARGET_FILE — nothing to check."
    exit 0
fi

while IFS= read -r call_line; do
    [[ -z "$call_line" ]] && continue
    window_start=$((call_line - COLDPATH_WINDOW_LINES))
    (( window_start < 1 )) && window_start=1

    # Use awk to extract the window lines and grep for the annotation.
    # ``# cold-path (#3089)`` is the exact marker — any other form is
    # rejected so operators can't accidentally satisfy the check with a
    # generic cold-path comment.
    window_text="$(awk -v s="$window_start" -v e="$call_line" \
        'NR >= s && NR <= e' "$TARGET_FILE")"
    if ! printf '%s' "$window_text" | grep -qE '# cold-path \(#3089\)'; then
        missing+=("$call_line")
        violations=$((violations + 1))
    fi
done <<< "$python_output"

if (( violations > 0 )); then
    echo "ERROR: subprocess.run([\"git\"|\"gh\", ...]) site(s) missing a nearby '# cold-path (#3089)' annotation."
    echo ""
    echo "  Target: $TARGET_FILE"
    echo "  Window: ${COLDPATH_WINDOW_LINES} lines above each call site."
    echo ""
    echo "  Every git/gh subprocess call site must either:"
    echo "    (a) route through self._subprocess_with_retry(...), OR"
    echo "    (b) carry a '# cold-path (#3089): <reason>' comment nearby."
    echo ""
    echo "  See #3089 for the audit + classification reference."
    echo ""
    echo "  Sites missing an annotation:"
    echo ""
    # Length-guard the iteration — see #4479.
    if [ "${#missing[@]}" -gt 0 ]; then
        for line in "${missing[@]}"; do
            snippet="$(sed -n "${line}p" "$TARGET_FILE")"
            echo "    $TARGET_FILE:${line}: ${snippet}"
        done
    fi
    echo ""
    echo "  Fix: add a comment above the call such as"
    echo ""
    echo "    # cold-path (#3089): local fs op, no network — retry adds no value."
    echo ""
    echo "  Or route through the shared helper:"
    echo ""
    echo "    outcome = self._subprocess_with_retry(cmd, event_name='<site>', timeout=...)"
    echo "    if not outcome['ok']:"
    echo "        ..."
    exit 1
fi

total_count="$(printf '%s\n' "$python_output" | grep -c . || true)"
echo "check-git-gh-retries: ${total_count} direct git/gh subprocess.run site(s) all covered by a cold-path (#3089) annotation within ${COLDPATH_WINDOW_LINES} lines."
exit 0
