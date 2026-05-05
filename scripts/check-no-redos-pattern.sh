#!/usr/bin/env bash
# check-no-redos-pattern.sh — Detect ReDoS-shaped regex patterns in Python.
#
# Flags ``re.compile(r"<pattern>", ...re.IGNORECASE...)`` calls where
# ``<pattern>`` begins with an unanchored lazy quantifier — i.e. the
# pattern starts (optionally inside a wrapping group) with a wildcard
# followed by ``+?`` or ``*?`` BEFORE any literal anchor (``^``, ``\b``,
# ``\A``, ``\Z``, ``$``, or a literal character).
#
# Why this check exists
# ---------------------
# This shape exhibits O(n^2) per call on inputs that lack the literal
# anchor.  Issue #4104 documented a real production incident: a leading
# ``([^\n]+?)\s+Judge of the Superior Court`` pattern with re.IGNORECASE
# pegged the LA scraper at 15+ seconds per 50KB federal opinion text.
# The fix anchored the pattern at start-of-line with ``re.MULTILINE``;
# the same anti-pattern could exist anywhere a contributor reaches for a
# leading lazy capture as a "match anything before this literal" idiom.
#
# Heuristic — not perfect
# -----------------------
# False negatives are acceptable.  False positives can be suppressed
# with a trailing ``# noqa: redos-pattern`` comment on the same line as
# the ``re.compile(`` call site.  The goal is to catch the common case
# before it lands.
#
# What is flagged
# ---------------
# A re.compile(r"...", flags) call is flagged when:
#   1. ``re.IGNORECASE`` (or ``re.I``) is in the flags argument, AND
#   2. The pattern string starts (after at most one opening ``(``,
#      ``(?:``, ``(?P<name>``, ``(?=``, or ``(?!``) with a wildcard
#      construct (``.``, ``[...]``, ``\S``, ``\W``, ``\D``) followed by
#      ``+?`` or ``*?`` BEFORE any of these anchors:
#        - ``^`` or ``$`` or ``\A`` or ``\Z`` or ``\b`` or ``\B``
#        - A literal alphanumeric character (or escaped literal like
#          ``\.``, ``\(``, ``\/``, etc.) at the top level.
#
# What is NOT flagged
# -------------------
#   - Patterns starting with ``^``, ``\b``, ``\A`` (anchored).
#   - Patterns starting with a literal character (``Department\s+\S+?...``)
#     — the literal acts as an effective anchor.
#   - Patterns without ``re.IGNORECASE`` — case-sensitive scans of large
#     inputs do not trigger the same backtracking class.
#   - re.compile with a ``# noqa: redos-pattern`` suppression on the
#     same line (or the line opening the call).
#   - Files outside ``packages/`` and ``scripts/`` (the scan scope).
#   - Files under ``scripts/archive/`` (already-run one-off scripts).
#
# Issue: #4117.
#
# Usage
# -----
#   scripts/check-no-redos-pattern.sh                # scan packages/ + scripts/
#   scripts/check-no-redos-pattern.sh [path]         # scan a specific file or dir
#
# Exit codes
# ----------
#   0 — No violations found.
#   1 — At least one violating ``re.compile`` call site detected.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Determine scan targets ──────────────────────────────────────────────
# With no argument: scan packages/ and scripts/ (the production scope).
# With an argument: scan that path (file or directory) — used by tests.
if [[ $# -eq 0 ]]; then
    SCAN_TARGETS=("$REPO_ROOT/packages" "$REPO_ROOT/scripts")
else
    SCAN_TARGETS=("$1")
fi

# ─── Filter to existing .py paths ────────────────────────────────────────
py_files=()
for target in "${SCAN_TARGETS[@]}"; do
    if [[ -f "$target" && "$target" == *.py ]]; then
        py_files+=("$target")
    elif [[ -d "$target" ]]; then
        # Find every .py under the directory, excluding common vendored
        # / generated dirs.
        while IFS= read -r found; do
            py_files+=("$found")
        done < <(find "$target" \
            -type d \( \
                -name '.venv' -o \
                -name '__pycache__' -o \
                -name 'node_modules' -o \
                -name '.git' -o \
                -path '*/scripts/archive' \
            \) -prune \
            -o -type f -name '*.py' -print)
    fi
done

if [[ ${#py_files[@]} -eq 0 ]]; then
    exit 0
fi

# ─── Run the AST + pattern scanner ───────────────────────────────────────
# The Python scanner emits one line per violation in the form:
#   <path>:<lineno>:<pattern-snippet>
#
# Files are passed as positional args; the scanner reads each one.
python_output="$(python3 "$REPO_ROOT/scripts/check_no_redos_pattern.py" "${py_files[@]}")"

# ─── Report violations ───────────────────────────────────────────────────
if [[ -z "${python_output// /}" ]]; then
    exit 0
fi

violations=0
echo "ERROR: Found ReDoS-shaped re.compile() pattern(s) with re.IGNORECASE."
echo ""
echo "  These patterns start with an unanchored lazy quantifier (+? or *?)"
echo "  before any literal anchor.  This shape exhibits O(n^2) per call"
echo "  on inputs that lack the literal anchor — see issue #4104 for the"
echo "  production incident."
echo ""
echo "  Fix options:"
echo "    1. Anchor the pattern at start-of-line with re.MULTILINE and ^."
echo "    2. Bound the wildcard with a {min,max} length cap."
echo "    3. Replace with a literal-leading pattern."
echo ""
echo "  Suppress (false positives only):"
echo "    re.compile(r\"...\", re.IGNORECASE)  # noqa: redos-pattern"
echo ""
echo "  Violating call sites:"
echo ""
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    echo "    $entry"
    violations=$((violations + 1))
done <<< "$python_output"

if (( violations > 0 )); then
    echo ""
    echo "  Found $violations occurrence(s) of leading-lazy-quantifier with re.IGNORECASE."
    exit 1
fi

exit 0
