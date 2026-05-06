#!/usr/bin/env bash
# check-bash-set-u-empty-array.sh — Forbid the bash 5.x footgun where
# ``declare -a <name>`` (without ``=()``) is later expanded as
# ``${#<name>[@]}`` or ``"${<name>[@]}"`` under ``set -u``.
#
# Why this check exists
# ---------------------
# ``declare -a <name>`` declares an indexed array but does NOT assign
# it. On bash 3.2 (macOS operator laptops), reading
# ``${#empty_declared_array[@]}`` returns 0 cleanly. On bash 5.x (Linux
# CI runners) under ``set -u``, the same read trips ``unbound
# variable``:
#
#     scripts/check-cloudwatch-alarm-docs.sh: line 231: unresolved: unbound variable
#
# This is a textbook platform-skew bug — passes locally, fails CI —
# exactly the gap that ``scripts/check-bash-compat.sh`` covers for the
# bash 4+ feature set. Surfaced in #4119 / PR #4140; tracked here as
# #4143.
#
# The canonical fix is one character: ``declare -a <name>`` →
# ``<name>=()``. The latter both declares the variable AND assigns an
# empty array, so the subsequent ``${#<name>[@]}`` read sees a
# bound-but-empty array and returns 0 on every bash version.
#
# Detection strategy
# ------------------
# This check is line-text-based (not AST-based) so it runs in a few
# hundred milliseconds on a cold macOS laptop. For each shell script:
#
#   1. Detect whether ``set -u`` (or ``set -o nounset``, or any ``set
#      -[a-z]*u`` flags combo like ``-eu`` / ``-euo pipefail``) appears
#      anywhere in the file. If not, skip the file — no nounset, no
#      bug.
#
#   2. Find every ``declare -a <name>`` (or ``typeset -a <name>``)
#      that is NOT immediately followed by ``=`` — i.e. a bare declare
#      without an inline assignment. Record the line number and the
#      array name.
#
#   3. For each (name, declare_line) pair: scan the file from
#      ``declare_line + 1`` to EOF looking for either:
#        - an assignment ``<name>=(`` or ``<name>+=(`` (clears the bug
#          — array is bound before any read), OR
#        - a read ``${#<name>[@]}`` or ``"${<name>[@]}"`` or
#          ``"${<name>[*]}"`` (triggers the bug).
#      Whichever comes first determines the verdict: read-first → flag,
#      assign-first → safe.
#
# What it does NOT flag
# ---------------------
#   - ``declare -a <name>=()`` — the inline form is fine.
#   - ``<name>=()`` — the no-declare form is fine.
#   - ``declare -a <name>`` followed by ``<name>+=(...)`` BEFORE any
#     ``${#<name>[@]}`` read — append-then-read is bash 3.2 / 5.x
#     compatible because ``+=`` on an undeclared array binds it.
#   - Files without any ``set -u`` / ``set -o nounset`` directive — no
#     nounset, no bug.
#   - Comment lines (first non-whitespace is ``#``).
#
# Sibling check: ``scripts/check-bash-compat.sh`` covers the bash-4+
# constructs that simply don't exist on bash 3.2 (mapfile, declare
# -A, namerefs, case-conversion expansions, ;;&, |&). This check
# covers a different class of failure: a construct that exists on
# both bash 3.2 and 5.x but has *different observable behavior*
# under ``set -u``. The two checks are complementary, not
# overlapping.
#
# Usage
# -----
#   scripts/check-bash-set-u-empty-array.sh          # scan repo's scripts/
#   scripts/check-bash-set-u-empty-array.sh [dir]    # scan a specific directory
#
# Exit codes
# ----------
#   0 — No violations found.
#   1 — One or more shell scripts have ``declare -a <name>`` followed
#       by ``${#<name>[@]}`` (or ``"${<name>[@]}"``) under ``set -u``
#       without an intervening assignment. The offending lines are
#       printed with file:line:content plus the suggested fix.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── Files that legitimately mention the forbidden pattern ───────────────
# The check script and its test must spell the pattern literally.
EXCLUDE_FILES=(
    "scripts/check-bash-set-u-empty-array.sh"
    "scripts/tests/test_check_bash_set_u_empty_array.sh"
)

# ─── Directories to exclude from the scan ────────────────────────────────
EXCLUDE_DIRS=(
    ".git"
    ".venv"
    "node_modules"
    "__pycache__"
    "tmp"
)

# ─── Locate the shell scripts to scan ────────────────────────────────────
SCRIPTS_DIR="$SCAN_DIR/scripts"
if [[ ! -d "$SCRIPTS_DIR" ]]; then
    # When invoked against a test TMPDIR that doesn't have a scripts/
    # subdir, treat SCAN_DIR itself as the target.
    SCRIPTS_DIR="$SCAN_DIR"
fi

prune_args=()
for d in "${EXCLUDE_DIRS[@]}"; do
    prune_args+=(-name "$d" -type d -prune -o)
done

sh_files=()
while IFS= read -r f; do
    [[ -n "$f" ]] && sh_files+=("$f")
done < <(find "$SCRIPTS_DIR" "${prune_args[@]+"${prune_args[@]}"}" \
    -type f -name '*.sh' -print 2>/dev/null | sort || true)

if [[ ${#sh_files[@]} -eq 0 ]]; then
    echo "check-bash-set-u-empty-array: no *.sh files found under $SCRIPTS_DIR — nothing to check."
    exit 0
fi

# ─── Per-file scan ───────────────────────────────────────────────────────
violations=0
report_lines=()

# Match any ``set`` invocation that turns on nounset:
#   set -u
#   set -eu
#   set -euo pipefail
#   set -o nounset
# Comment-leading whitespace is stripped before this regex is applied.
NOUNSET_REGEX='^[[:space:]]*set[[:space:]]+(-[a-zA-Z]*u[a-zA-Z]*([[:space:]]|$)|-o[[:space:]]+nounset)'

# Match ``declare -a <name>`` or ``typeset -a <name>`` where <name> is
# NOT followed by ``=`` (i.e. bare declare). Captures <name> in
# BASH_REMATCH[2].
DECLARE_BARE_REGEX='^[[:space:]]*(declare|typeset)[[:space:]]+-[a-zA-Z]*a[[:space:]]+([A-Za-z_][A-Za-z0-9_]*)([[:space:]]|$)'

for file in "${sh_files[@]}"; do
    # Skip allow-listed files.
    skip=false
    for excl in "${EXCLUDE_FILES[@]}"; do
        if [[ "$file" == *"$excl" ]]; then
            skip=true
            break
        fi
    done
    if "$skip"; then
        continue
    fi

    # Read file into an array of lines. Use the ``while read`` idiom —
    # not ``mapfile`` — because this very script must run on bash 3.2.
    lines=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        lines+=("$line")
    done < "$file"

    nlines=${#lines[@]}
    [[ $nlines -eq 0 ]] && continue

    # Pass 1: does this file enable ``set -u``?
    has_nounset=false
    i=0
    while (( i < nlines )); do
        l="${lines[$i]}"
        # Skip comment lines.
        if [[ "$l" =~ ^[[:space:]]*# ]]; then
            i=$((i + 1))
            continue
        fi
        if [[ "$l" =~ $NOUNSET_REGEX ]]; then
            has_nounset=true
            break
        fi
        i=$((i + 1))
    done

    if ! "$has_nounset"; then
        continue
    fi

    # Pass 2: find bare ``declare -a <name>`` lines and check for
    # read-before-assign. We re-walk the file linearly, recording each
    # bare declare and then checking forward.
    decl_idx=0
    while (( decl_idx < nlines )); do
        dline="${lines[$decl_idx]}"

        # Skip comments.
        if [[ "$dline" =~ ^[[:space:]]*# ]]; then
            decl_idx=$((decl_idx + 1))
            continue
        fi

        if [[ "$dline" =~ $DECLARE_BARE_REGEX ]]; then
            name="${BASH_REMATCH[2]}"

            # Build name-specific patterns. ``<name>=`` and
            # ``<name>+=`` are bash assignment forms; the read forms
            # are ``${#<name>[@]}`` / ``${#<name>[*]}`` /
            # ``${<name>[@]...}`` / ``${<name>[*]...}``.
            #
            # Regex notes:
            #   - Anchor reads at ``\$\{`` (literal ``${``) so we don't
            #     match ``$<name>`` (scalar read, not array).
            #   - Anchor assigns at ``<name>=(`` or ``<name>+=(`` so
            #     a substring like ``foo_bar=...`` does not falsely
            #     match when the array name is ``foo``.
            assign_regex="(^|[^A-Za-z0-9_])${name}\\+?=\\("
            read_regex="\\\$\\{#?${name}\\["

            scan_idx=$((decl_idx + 1))
            verdict=""
            verdict_line=0
            verdict_content=""
            while (( scan_idx < nlines )); do
                sline="${lines[$scan_idx]}"

                # Skip comments inside the scan.
                if [[ "$sline" =~ ^[[:space:]]*# ]]; then
                    scan_idx=$((scan_idx + 1))
                    continue
                fi

                # Check assignment first — it is the safe outcome and
                # we want to short-circuit cleanly.
                if [[ "$sline" =~ $assign_regex ]]; then
                    verdict="assigned"
                    break
                fi

                if [[ "$sline" =~ $read_regex ]]; then
                    verdict="read"
                    verdict_line=$((scan_idx + 1))
                    verdict_content="$sline"
                    break
                fi

                scan_idx=$((scan_idx + 1))
            done

            if [[ "$verdict" == "read" ]]; then
                report_lines+=("  [declare -a $name read before assign under set -u]")
                report_lines+=("    $file:$((decl_idx + 1)): $dline")
                report_lines+=("    $file:$verdict_line: $verdict_content")
                report_lines+=("    fix: replace 'declare -a $name' with '$name=()'")
                violations=$((violations + 1))
            fi
        fi

        decl_idx=$((decl_idx + 1))
    done
done

if (( violations > 0 )); then
    echo "ERROR: bash 5.x set -u + declare -a empty-array footgun(s) detected in scripts/**/*.sh."
    echo ""
    echo "  'declare -a <name>' declares an indexed array but does NOT"
    echo "  assign it. On bash 3.2 (macOS) reading \${#<name>[@]} returns"
    echo "  0 cleanly; on bash 5.x (Linux CI) under 'set -u' the same"
    echo "  read trips '<name>: unbound variable'. Fix: replace"
    echo "  'declare -a <name>' with '<name>=()' so the variable is"
    echo "  bound to an empty array at declaration time."
    echo ""
    for line in "${report_lines[@]}"; do
        echo "$line"
    done
    echo ""
    echo "  Total violations: $violations"
    echo ""
    echo "  See: scripts/check-bash-set-u-empty-array.sh header for the"
    echo "  full rationale and #4143 / PR #4140 for the originating"
    echo "  incident."
    exit 1
fi

echo "check-bash-set-u-empty-array: ${#sh_files[@]} shell script(s) scanned — all clean."
exit 0
