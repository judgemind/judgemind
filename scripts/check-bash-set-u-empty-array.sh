#!/usr/bin/env bash
# check-bash-set-u-empty-array.sh — Forbid two sibling bash + ``set -u``
# empty-array footguns:
#
#   (A) ``declare -a <name>`` (without ``=()``) is later expanded as
#       ``${#<name>[@]}`` or ``"${<name>[@]}"``. Trips on bash 5.x
#       (Linux CI), passes on bash 3.2 (macOS).
#
#   (B) ``<name>=()`` is later iterated as ``"${<name>[@]}"`` /
#       ``"${<name>[*]}"`` while still empty (no intervening
#       ``<name>+=(...)`` or ``<name>=(...)``-with-content). Trips on
#       bash 3.2 (macOS), passes on bash 5.x.
#
# Both are different declaration forms of the same root-cause class:
# declared-but-empty indexed arrays read with ``[@]`` / ``[*]`` under
# nounset, where bash 3.2 and bash 5.x disagree on what ``unbound``
# means.
#
# Why this check exists
# ---------------------
# Shape (A): ``declare -a <name>`` declares an indexed array but does
# NOT assign it. On bash 3.2 (macOS operator laptops), reading
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
# Shape (B): ``<name>=()`` initialises the array empty, but iterating
# ``"${<name>[@]}"`` while it is *still* empty trips ``unbound
# variable`` on bash 3.2 — the inverse-direction skew of shape (A).
# The size read ``${#<name>[@]}`` itself is fine on bash 3.2, but the
# ``[@]`` / ``[*]`` element-expansion form is not. Surfaced in #4332's
# ``scripts/run-ci-guards.sh`` umbrella (worked around at lines 254 +
# 280 with ``if [ "${#arr[@]}" -gt 0 ]`` length guards); tracked here
# as #4336.
#
# The canonical fix for shape (B) is the same length-guard one-liner:
# wrap iteration in ``if [ "${#<name>[@]}" -gt 0 ]; then`` so the
# loop body is skipped when the array is empty. Pre-populating the
# array with at least one assignment before reading also fixes it.
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
#   2. (Shape A) Find every ``declare -a <name>`` (or ``typeset -a
#      <name>``) that is NOT immediately followed by ``=`` — i.e. a
#      bare declare without an inline assignment. Record the line
#      number and the array name. For each (name, declare_line) pair:
#      scan the file from ``declare_line + 1`` to EOF looking for
#      either:
#        - an assignment ``<name>=(`` or ``<name>+=(`` (clears the bug
#          — array is bound before any read), OR
#        - a read ``${#<name>[@]}`` or ``"${<name>[@]}"`` or
#          ``"${<name>[*]}"`` (triggers the bug).
#      Whichever comes first determines the verdict: read-first → flag,
#      assign-first → safe.
#
#   3. (Shape B) Find every bare-empty ``<name>=()`` declaration —
#      i.e. an indexed-array initialiser where the parens are empty
#      (whitespace-only between ``(`` and ``)``). Record the line
#      number and the array name. For each (name, decl_line) pair:
#      scan from ``decl_line + 1`` to EOF looking for either:
#        - an assignment ``<name>=(`` or ``<name>+=(`` (the bug is
#          cleared the moment any element appends or a fresh
#          assignment lands), OR
#        - an iteration-form read ``"${<name>[@]}"`` or
#          ``"${<name>[*]}"`` (i.e. element expansion, NOT the size
#          form ``${#<name>[@]}`` which is bash-3.2-safe — flags the
#          bug).
#      Same first-wins verdict logic.
#
# What it does NOT flag
# ---------------------
#   - ``declare -a <name>=()`` — the inline form is fine.
#   - ``<name>=()`` followed by ``<name>+=(...)`` BEFORE any iteration
#     read — append-then-iterate is bash-3.2-safe because the array is
#     no longer empty by the time iteration runs.
#   - ``<name>=()`` whose only subsequent reads are size form
#     ``${#<name>[@]}`` / ``${#<name>[*]}`` — bash 3.2 handles size
#     reads of empty initialised arrays cleanly.
#   - ``<name>=("a" "b")`` with content — not bare-empty, not flagged.
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
#   1 — One or more shell scripts trip shape (A) or shape (B). The
#       offending lines are printed with file:line:content plus the
#       suggested fix.

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

# Match a bare-empty array initialiser ``<name>=()`` — i.e. the parens
# are empty (whitespace-only between ``(`` and ``)``). Captures <name>
# in BASH_REMATCH[1]. Anchored at start-of-line (with optional
# leading whitespace) so a substring like ``foo=()`` inside an
# expression is not picked up. Excludes ``declare -a <name>=()`` /
# ``local -a <name>=()`` / ``readonly <name>=()`` and friends — those
# inline-assignment forms are not the bare-empty pattern this check
# targets, and they also fall under shape (A)'s "inline OK" carve-out.
EMPTY_INIT_REGEX='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=\([[:space:]]*\)[[:space:]]*$'

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

    # Pass 3: find bare-empty ``<name>=()`` lines and check for an
    # iteration-form read (``"${<name>[@]}"`` / ``"${<name>[*]}"``)
    # that fires *before* any intervening ``<name>+=(...)`` or
    # ``<name>=(...)``-with-content assignment. Same first-wins
    # verdict logic as Pass 2 but with a different declaration
    # matcher and a stricter read regex (size form ``${#<name>[@]}``
    # is not flagged — it is bash-3.2-safe).
    init_idx=0
    while (( init_idx < nlines )); do
        iline="${lines[$init_idx]}"

        # Skip comments.
        if [[ "$iline" =~ ^[[:space:]]*# ]]; then
            init_idx=$((init_idx + 1))
            continue
        fi

        if [[ "$iline" =~ $EMPTY_INIT_REGEX ]]; then
            iname="${BASH_REMATCH[1]}"

            # Build name-specific patterns. Same assignment regex as
            # Pass 2 — ``<name>=(`` or ``<name>+=(`` clears the bug
            # by binding the array with at least one (or about to be
            # one) element.
            #
            # The read regex is *stricter* than Pass 2's: it matches
            # the bare element-expansion forms ``${<name>[@]}`` and
            # ``${<name>[*]}`` only — i.e. ``[@]`` or ``[*]``
            # IMMEDIATELY followed by ``}``. It deliberately does
            # NOT match the size form ``${#<name>[@]}`` (bash-3.2-
            # safe on an empty initialised array; flagging it here
            # would over-report).
            #
            # Anchoring rationale: same as Pass 2 — leading
            # ``[^A-Za-z0-9_]`` (or start-of-line) before the name
            # ensures a substring like ``foo_bar=`` does not match
            # when the array is ``foo``.
            #
            # Guarded-form exemption: lines containing the defensive
            # parameter-expansion guard ``${<name>[@]+"${<name>[@]}"}``
            # (or the ``[*]`` variant) are exempted in the scan
            # below before the read regex is applied. The leading
            # ``[@]+...`` substitutes nothing when the array is
            # unset OR empty — the canonical bash-3.2-safe idiom for
            # "iterate this maybe-empty array under set -u" — and
            # the inner ``${<name>[@]}`` only evaluates when the
            # array is non-empty, so it is not a footgun. We detect
            # the guarded form via a substring presence check
            # (``[@]+`` or ``[*]+`` after ``${<name>``) and skip
            # the line.
            iassign_regex="(^|[^A-Za-z0-9_])${iname}\\+?=\\("
            iread_regex="\\\$\\{${iname}\\[[@*]\\]\\}"
            iguarded_regex="\\\$\\{${iname}\\[[@*]\\]\\+"

            iscan_idx=$((init_idx + 1))
            iverdict=""
            iverdict_line=0
            iverdict_content=""
            while (( iscan_idx < nlines )); do
                isline="${lines[$iscan_idx]}"

                # Skip comments inside the scan.
                if [[ "$isline" =~ ^[[:space:]]*# ]]; then
                    iscan_idx=$((iscan_idx + 1))
                    continue
                fi

                # Check assignment first — it is the safe outcome.
                if [[ "$isline" =~ $iassign_regex ]]; then
                    iverdict="assigned"
                    break
                fi

                # Skip lines using the guarded ``${name[@]+...}``
                # parameter-expansion idiom. The line is bash-3.2-
                # safe even when the array is empty, so do not
                # treat it as a flagged read. The scan continues
                # past the guarded line — the array is still empty
                # afterward, so a later unguarded iteration would
                # still be flagged.
                if [[ "$isline" =~ $iguarded_regex ]]; then
                    iscan_idx=$((iscan_idx + 1))
                    continue
                fi

                if [[ "$isline" =~ $iread_regex ]]; then
                    iverdict="read"
                    iverdict_line=$((iscan_idx + 1))
                    iverdict_content="$isline"
                    break
                fi

                iscan_idx=$((iscan_idx + 1))
            done

            if [[ "$iverdict" == "read" ]]; then
                report_lines+=("  [$iname=() iterated empty under set -u (bash 3.2 footgun)]")
                report_lines+=("    $file:$((init_idx + 1)): $iline")
                report_lines+=("    $file:$iverdict_line: $iverdict_content")
                report_lines+=("    fix: guard with 'if [ \"\${#$iname[@]}\" -gt 0 ]; then ... fi'")
                report_lines+=("         or pre-populate '$iname' before iterating")
                violations=$((violations + 1))
            fi
        fi

        init_idx=$((init_idx + 1))
    done
done

if (( violations > 0 )); then
    echo "ERROR: bash + set -u empty-array footgun(s) detected in scripts/**/*.sh."
    echo ""
    echo "  Shape (A) — 'declare -a <name>' declares an indexed array"
    echo "  but does NOT assign it. On bash 3.2 (macOS) reading"
    echo "  \${#<name>[@]} returns 0 cleanly; on bash 5.x (Linux CI)"
    echo "  under 'set -u' the same read trips '<name>: unbound"
    echo "  variable'. Fix: replace 'declare -a <name>' with"
    echo "  '<name>=()' so the variable is bound to an empty array at"
    echo "  declaration time."
    echo ""
    echo "  Shape (B) — '<name>=()' initialises the array empty, but"
    echo "  iterating \"\${<name>[@]}\" / \"\${<name>[*]}\" while it is"
    echo "  still empty trips 'unbound variable' on bash 3.2 (the"
    echo "  inverse-direction skew of shape A). Fix: guard iteration"
    echo "  with 'if [ \"\${#<name>[@]}\" -gt 0 ]; then ... fi', or"
    echo "  pre-populate the array before iterating."
    echo ""
    for line in "${report_lines[@]}"; do
        echo "$line"
    done
    echo ""
    echo "  Total violations: $violations"
    echo ""
    echo "  See: scripts/check-bash-set-u-empty-array.sh header for the"
    echo "  full rationale, #4143 / PR #4140 for shape (A), and"
    echo "  #4332 / #4336 for shape (B)."
    exit 1
fi

echo "check-bash-set-u-empty-array: ${#sh_files[@]} shell script(s) scanned — all clean."
exit 0
