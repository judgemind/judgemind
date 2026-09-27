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
# This check is line-text-based (not AST-based). The scan runs in one
# Python process (``scripts/_check_bash_set_u_empty_array_scan.py``,
# #4720) and takes well under a second. For each shell script:
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
#      scan from ``decl_line + 1`` to EOF, tracking control-flow
#      depth, looking for either:
#        - an *unconditional* assignment ``<name>=(`` or
#          ``<name>+=(`` — i.e. one at the same control-flow depth
#          as the ``<name>=()`` line, NOT inside a deeper ``if`` /
#          ``case`` / ``while`` / ``until`` / ``for`` block. This
#          unconditionally binds the array; verdict: safe.
#        - an *iteration-form* read ``"${<name>[@]}"`` or
#          ``"${<name>[*]}"`` (element expansion, NOT the size form
#          ``${#<name>[@]}``) — verdict: flag.
#      Reads inside an explicit length-guard block — ``if [
#      "${#<name>[@]}" -gt 0 ]; then ... fi`` (or the ``[[ ... ]]`` /
#      ``-ge 1`` / ``-ne 0`` / ``!= 0`` variants) — are exempt: the
#      iteration only fires when the array is non-empty. A closed
#      ``if [[ ${#<name>[@]} -eq 0 ]]; then ... exit|return ...; fi``
#      block before any iteration also short-circuits to "safe": the
#      array is guaranteed non-empty after the early-exit check.
#      Conditional assignments — ``<name>+=(...)`` inside a deeper
#      block — are NOT treated as binding (#4479): the prior
#      first-``+=``-wins logic missed bugs like ``block-on-new-
#      issue.sh`` where the ``+=`` was inside a ``case`` arm of an
#      arg-parse loop and never executed when the user didn't pass
#      the relevant flag.
#
# What it does NOT flag
# ---------------------
#   - ``declare -a <name>=()`` — the inline form is fine.
#   - ``<name>=()`` followed by an *unconditional* ``<name>+=(...)``
#     (at the same control-flow depth as the declaration) BEFORE any
#     iteration read — append-then-iterate is bash-3.2-safe because
#     the array is no longer empty by the time iteration runs.
#   - ``<name>=()`` whose iteration is wrapped in an explicit length-
#     guard block: ``if [ "${#<name>[@]}" -gt 0 ]; then for x in
#     "${<name>[@]}"; do ...; done; fi`` (or ``[[ ... ]]`` / ``-ge 1``
#     / ``-ne 0`` / ``!= 0`` variants). The iteration only fires when
#     the array is non-empty.
#   - ``<name>=()`` followed by an early-exit-on-empty guard — ``if
#     [[ ${#<name>[@]} -eq 0 ]]; then exit|return ...; fi`` (or ``-lt
#     1`` / ``-le 0`` / ``== 0`` variants) — *before* the iteration.
#     The array is guaranteed non-empty after the check.
#   - ``<name>=()`` whose only subsequent reads are size form
#     ``${#<name>[@]}`` / ``${#<name>[*]}`` — bash 3.2 handles size
#     reads of empty initialised arrays cleanly.
#   - ``<name>=()`` whose iteration uses the parameter-expansion
#     guard ``${<name>[@]+"${<name>[@]}"}`` — the leading ``[@]+...``
#     substitutes nothing on empty.
#   - ``<name>=("a" "b")`` with content — not bare-empty, not flagged.
#   - ``declare -a <name>`` followed by ``<name>+=(...)`` BEFORE any
#     ``${#<name>[@]}`` read — append-then-read is bash 3.2 / 5.x
#     compatible because ``+=`` on an undeclared array binds it.
#   - Files without any ``set -u`` / ``set -o nounset`` directive — no
#     nounset, no bug.
#   - Comment lines (first non-whitespace is ``#``).
#
# What it DOES flag (since #4479)
# -------------------------------
#   - ``<name>=()`` whose only subsequent ``+=`` is *conditional* —
#     i.e. inside an ``if`` / ``case`` / ``while`` / ``until`` /
#     ``for`` block at deeper control-flow depth than the
#     declaration — followed by an iteration read that is NOT
#     wrapped in a length-guard or preceded by an early-exit guard.
#     This was the ``block-on-new-issue.sh`` shape from #4051: the
#     ``LABELS+=(...)`` inside the arg-parse ``case`` arm only ran
#     when the user supplied ``--label``; with no flag, the
#     subsequent ``for label in "${LABELS[@]}"; do`` tripped
#     ``unbound variable`` on bash 3.2.
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
#   scripts/check-bash-set-u-empty-array.sh                # scan repo's scripts/
#   scripts/check-bash-set-u-empty-array.sh [dir]          # scan a specific directory
#   scripts/check-bash-set-u-empty-array.sh --fix [dir]    # apply length-guard wrap to shape (B) violations
#   scripts/check-bash-set-u-empty-array.sh --fix --dry-run [dir]
#                                                          # print the patch to stdout, do NOT modify files
#
# --fix mode (#4492)
# ------------------
# Applies the canonical length-guard wrap to shape (B) violations:
#
#     for v in "${arr[@]}"; do  ───►  if [ "${#arr[@]}" -gt 0 ]; then
#         echo "$v"                       for v in "${arr[@]}"; do
#     done                                    echo "$v"
#                                         done
#                                     fi
#
# Same shape as ruff's ``--fix`` mode for Python lints. Without
# ``--fix`` the script behaves exactly as today (report-only). With
# ``--fix --dry-run`` the patch is printed to stdout but no file is
# modified. With ``--fix`` (no ``--dry-run``) the patch is applied
# in-place AND the diff is printed to stdout.
#
# Shape (A) violations are not auto-fixed — the canonical fix is a
# one-character edit (``declare -a <name>`` → ``<name>=()``) and is
# left to the operator. ``--fix`` mode only acts on shape (B).
#
# Exit codes
# ----------
#   0 — No violations found, OR ``--fix`` (without ``--dry-run``)
#       successfully patched every shape (B) violation.
#   1 — One or more shell scripts trip shape (A) or shape (B). The
#       offending lines are printed with file:line:content plus the
#       suggested fix. Also returned by ``--fix --dry-run`` (the
#       patch is printed but the violations are still outstanding).
#       Also returned by ``--fix`` when one or more shape (A)
#       violations remain unfixed (shape (A) is operator-fixed only).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Argument parsing ────────────────────────────────────────────────────
# Accept ``--fix`` and ``--dry-run`` as flags, in any order, before or
# after a positional ``[dir]`` argument. Unknown flags exit 2 with a
# usage hint.
FIX_MODE=0
DRY_RUN=0
SCAN_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fix)
            FIX_MODE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            sed -n '156,190p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        --*)
            echo "check-bash-set-u-empty-array: unknown flag '$1'" >&2
            echo "Usage: $0 [--fix [--dry-run]] [dir]" >&2
            exit 2
            ;;
        *)
            if [[ -z "$SCAN_DIR" ]]; then
                SCAN_DIR="$1"
                shift
            else
                echo "check-bash-set-u-empty-array: unexpected extra argument '$1'" >&2
                exit 2
            fi
            ;;
    esac
done
if [[ $DRY_RUN -eq 1 && $FIX_MODE -eq 0 ]]; then
    echo "check-bash-set-u-empty-array: --dry-run requires --fix" >&2
    exit 2
fi
SCAN_DIR="${SCAN_DIR:-$REPO_ROOT}"

# Files that legitimately mention the forbidden pattern (this script and
# its test) are skipped by EXCLUDE_SUFFIXES in the scanner helper below.

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

# Shape (A) violation count — tracked separately because --fix mode
# does not act on shape (A) (the fix is a one-character ``declare -a
# <name>`` → ``<name>=()`` edit left to the operator).
shape_a_violations=0

# Parallel arrays of shape (B) violation data. Each index i describes
# one violation:
#
#   fix_files[i]      — absolute path to the offending shell file
#   fix_inames[i]     — array name (e.g. ``LABELS``)
#   fix_iter_lines[i] — 1-based line number of the iteration read
#
# Populated from the scanner's B records below. Consumed by the --fix block at the
# bottom of the script.
fix_files=()
fix_inames=()
fix_iter_lines=()

# The three passes (nounset detection, shape A, shape B with its
# control-flow depth tracking) run in one Python process over every file
# (#4720). The pure-bash line walk they replace took ~10s of CPU on an
# idle laptop, which made this the slowest guard in run-ci-guards.sh.
# ``_check_bash_set_u_empty_array_scan.py`` is a line-for-line port of
# that walk with the same regexes, so the verdicts are unchanged. It
# emits one record per line:
#
#   R<TAB><text>                        a report line
#   A                                   one shape (A) violation
#   B<TAB><line><TAB><name><TAB><file>  one shape (B) violation (for --fix)
SCAN_HELPER="$REPO_ROOT/scripts/_check_bash_set_u_empty_array_scan.py"
scan_out="$(mktemp "${TMPDIR:-/tmp}/check-bash-set-u-empty-array.XXXXXX")"
trap 'rm -f "$scan_out"' EXIT
scan_rc=0
printf '%s\0' "${sh_files[@]}" | python3 "$SCAN_HELPER" > "$scan_out" || scan_rc=$?
if (( scan_rc != 0 )); then
    echo "check-bash-set-u-empty-array: scanner $SCAN_HELPER failed (exit $scan_rc)" >&2
    exit 2
fi

TAB=$'\t'
while IFS= read -r rec || [[ -n "$rec" ]]; do
    case "$rec" in
        "R$TAB"*)
            report_lines+=("${rec#R$TAB}")
            ;;
        A)
            violations=$((violations + 1))
            shape_a_violations=$((shape_a_violations + 1))
            ;;
        "B$TAB"*)
            rest="${rec#B$TAB}"
            fix_iter_lines+=("${rest%%$TAB*}")
            rest="${rest#*$TAB}"
            fix_inames+=("${rest%%$TAB*}")
            fix_files+=("${rest#*$TAB}")
            violations=$((violations + 1))
            ;;
    esac
done < "$scan_out"

if (( violations == 0 )); then
    echo "check-bash-set-u-empty-array: ${#sh_files[@]} shell script(s) scanned — all clean."
    exit 0
fi

# ─── --fix mode (#4492) ────────────────────────────────────────────────
# When --fix is set, walk the shape (B) violations recorded above and
# wrap the offending iteration line (and its enclosing for-loop body,
# if applicable) in an ``if [ "${#<name>[@]}" -gt 0 ]; then ... fi``
# block. Print the unified diff to stdout for every file modified.
#
# Algorithm per violation:
#   1. Read the file into an array of lines.
#   2. Locate the iteration line by 1-based line number.
#   3. If the iteration line is the opener of a multi-line ``for ... do``
#      block (matches ``^[[:space:]]*for[[:space:]].*do[[:space:]]*$``),
#      find the matching ``done`` at the same line-prefix indentation.
#      Otherwise the iteration is a single-line read (``echo "${arr[@]}"``)
#      and only that single line is wrapped.
#   4. Insert ``<indent>if [ "${#<name>[@]}" -gt 0 ]; then`` immediately
#      before the iteration line and ``<indent>fi`` immediately after
#      the block's closing line. Inner content keeps its existing
#      indentation (per AC#1: "with preserved indentation").
#
# Multiple violations in the same file are processed back-to-front so
# earlier line numbers stay valid as later edits are applied.
if (( FIX_MODE )); then
    # Group violations by file so each file is opened, edited, and
    # diffed exactly once. We process files in the order they appear
    # in fix_files[]; for each file we collect the indices of every
    # violation belonging to that file, then apply the edits back-
    # to-front so earlier line numbers remain valid.
    files_seen=()
    nfix=${#fix_files[@]}
    fi_loop_idx=0
    while (( fi_loop_idx < nfix )); do
        cur_file="${fix_files[$fi_loop_idx]}"

        # Skip if we've already processed this file.
        already_seen=0
        for seen in "${files_seen[@]+"${files_seen[@]}"}"; do
            if [[ "$seen" == "$cur_file" ]]; then
                already_seen=1
                break
            fi
        done
        if (( already_seen )); then
            fi_loop_idx=$((fi_loop_idx + 1))
            continue
        fi
        files_seen+=("$cur_file")

        # Collect every (iname, iter_line) pair for this file.
        per_file_inames=()
        per_file_iter_lines=()
        gather_idx=0
        while (( gather_idx < nfix )); do
            if [[ "${fix_files[$gather_idx]}" == "$cur_file" ]]; then
                per_file_inames+=("${fix_inames[$gather_idx]}")
                per_file_iter_lines+=("${fix_iter_lines[$gather_idx]}")
            fi
            gather_idx=$((gather_idx + 1))
        done

        # Sort the (iter_line, iname) pairs by iter_line DESCENDING so
        # we apply edits back-to-front. Bash 3.2 has no associative
        # arrays; use a simple O(n²) selection sort over the parallel
        # arrays.
        nper=${#per_file_iter_lines[@]}
        sort_i=0
        while (( sort_i < nper )); do
            sort_max=$sort_i
            sort_j=$((sort_i + 1))
            while (( sort_j < nper )); do
                if (( ${per_file_iter_lines[$sort_j]} > ${per_file_iter_lines[$sort_max]} )); then
                    sort_max=$sort_j
                fi
                sort_j=$((sort_j + 1))
            done
            if (( sort_max != sort_i )); then
                tmp_line="${per_file_iter_lines[$sort_i]}"
                tmp_name="${per_file_inames[$sort_i]}"
                per_file_iter_lines[$sort_i]="${per_file_iter_lines[$sort_max]}"
                per_file_inames[$sort_i]="${per_file_inames[$sort_max]}"
                per_file_iter_lines[$sort_max]="$tmp_line"
                per_file_inames[$sort_max]="$tmp_name"
            fi
            sort_i=$((sort_i + 1))
        done

        # Read the file into an array of lines.
        edit_lines=()
        while IFS= read -r el || [[ -n "$el" ]]; do
            edit_lines+=("$el")
        done < "$cur_file"
        edit_nlines=${#edit_lines[@]}

        # Apply each violation's wrap, back-to-front.
        edit_i=0
        while (( edit_i < nper )); do
            ename="${per_file_inames[$edit_i]}"
            eline="${per_file_iter_lines[$edit_i]}"
            edit_idx=$((eline - 1))
            iter_text="${edit_lines[$edit_idx]}"

            # Compute leading-whitespace prefix of the iteration line.
            indent=""
            ws_i=0
            ws_n=${#iter_text}
            while (( ws_i < ws_n )); do
                ws_ch="${iter_text:$ws_i:1}"
                if [[ "$ws_ch" == " " || "$ws_ch" == $'\t' ]]; then
                    indent="${indent}${ws_ch}"
                    ws_i=$((ws_i + 1))
                else
                    break
                fi
            done

            # Trim leading whitespace (cheap parameter expansion).
            iter_trimmed="$iter_text"
            while [[ "$iter_trimmed" == [[:space:]]* ]]; do
                iter_trimmed="${iter_trimmed# }"
                iter_trimmed="${iter_trimmed#	}"
            done

            # Determine block end: multi-line ``for ... do`` opener vs
            # single-line read. The opener pattern matches:
            #   for X in "${arr[@]}"; do
            #   for X in "${arr[*]}"; do
            # with optional trailing comment / whitespace.
            block_end_idx=$edit_idx
            if [[ "$iter_trimmed" =~ ^for[[:space:]].*do([[:space:]]|$|\;) ]]; then
                # Multi-line for loop: find the matching ``done`` at
                # the same indentation. We scan forward, tracking the
                # nested for/while/until depth (these all close with
                # ``done``).
                fdepth=1
                scan_j=$((edit_idx + 1))
                while (( scan_j < edit_nlines )); do
                    sl="${edit_lines[$scan_j]}"
                    sl_trimmed="$sl"
                    while [[ "$sl_trimmed" == [[:space:]]* ]]; do
                        sl_trimmed="${sl_trimmed# }"
                        sl_trimmed="${sl_trimmed#	}"
                    done
                    # Skip comments.
                    if [[ "$sl_trimmed" == \#* || -z "$sl_trimmed" ]]; then
                        scan_j=$((scan_j + 1))
                        continue
                    fi
                    # One-liner ``for X; do Y; done`` would close on
                    # the same line; we only enter this branch when
                    # the opener was multi-line, so a one-liner here
                    # is just a nested complete construct that nets
                    # to zero.
                    if [[ "$sl_trimmed" =~ ^(for|while|until)([[:space:]]|$) ]]; then
                        if [[ "$sl_trimmed" =~ \;[[:space:]]*done([[:space:]]|\;|$) ]] || \
                           [[ "$sl_trimmed" =~ [[:space:]]done([[:space:]]|\;|$) ]]; then
                            :  # nested one-liner, no net change
                        else
                            fdepth=$((fdepth + 1))
                        fi
                    elif [[ "$sl_trimmed" =~ ^done([[:space:]]|\;|$) ]]; then
                        fdepth=$((fdepth - 1))
                        if (( fdepth == 0 )); then
                            block_end_idx=$scan_j
                            break
                        fi
                    fi
                    scan_j=$((scan_j + 1))
                done
                if (( fdepth != 0 )); then
                    echo "check-bash-set-u-empty-array: --fix could not find matching 'done' for '$ename' iteration at $cur_file:$eline; skipping" >&2
                    edit_i=$((edit_i + 1))
                    continue
                fi
            fi

            # Build the wrapped block.
            if_line="${indent}if [ \"\${#${ename}[@]}\" -gt 0 ]; then"
            fi_line="${indent}fi"

            # Splice into edit_lines: insert if_line BEFORE edit_idx,
            # leave the iter line and any block body unchanged, insert
            # fi_line AFTER block_end_idx.
            # Bash 3.2-safe array splice via rebuild-into-new-array.
            new_lines=()
            ri=0
            while (( ri < edit_nlines )); do
                if (( ri == edit_idx )); then
                    new_lines+=("$if_line")
                fi
                new_lines+=("${edit_lines[$ri]}")
                if (( ri == block_end_idx )); then
                    new_lines+=("$fi_line")
                fi
                ri=$((ri + 1))
            done
            edit_lines=("${new_lines[@]}")
            edit_nlines=${#edit_lines[@]}
            edit_i=$((edit_i + 1))
        done

        # Emit the unified diff. Use ``diff -u`` against a temporary
        # file holding the new contents. The diff command exits 1 when
        # the files differ (that's the expected case), so we capture
        # its rc explicitly.
        tmp_new="$cur_file.--fix.tmp.$$"
        # Rebuild the file contents, preserving newline at EOF.
        : > "$tmp_new"
        write_i=0
        while (( write_i < edit_nlines )); do
            printf '%s\n' "${edit_lines[$write_i]}" >> "$tmp_new"
            write_i=$((write_i + 1))
        done

        diff_rc=0
        diff -u "$cur_file" "$tmp_new" || diff_rc=$?

        if (( DRY_RUN )); then
            rm -f "$tmp_new"
        else
            # Preserve the original file's mode bits — ``mv`` on top
            # of an existing file keeps the destination's mode on
            # Linux + macOS, so write the new bytes via a copy + mv
            # idiom that overwrites in place without changing perms.
            cat "$tmp_new" > "$cur_file"
            rm -f "$tmp_new"
        fi

        fi_loop_idx=$((fi_loop_idx + 1))
    done

    # Exit codes for --fix mode:
    #   --fix --dry-run : exit 1 (violations still outstanding)
    #   --fix           : exit 0 if all violations were shape (B) and
    #                     thus auto-fixed; exit 1 if any shape (A)
    #                     violations remain (operator must hand-edit).
    if (( DRY_RUN )); then
        exit 1
    fi
    if (( shape_a_violations > 0 )); then
        echo "check-bash-set-u-empty-array: --fix applied $((violations - shape_a_violations)) shape (B) wrap(s); $shape_a_violations shape (A) violation(s) remain (manual fix required)." >&2
        exit 1
    fi
    exit 0
fi

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
echo "  pre-populate the array before iterating. Use --fix to apply"
echo "  the wrap automatically."
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
