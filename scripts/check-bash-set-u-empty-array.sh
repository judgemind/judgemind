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
            sed -n '155,189p' "${BASH_SOURCE[0]}"
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
# Populated in Pass B below. Consumed by the --fix block at the
# bottom of the script.
fix_files=()
fix_inames=()
fix_iter_lines=()

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
                shape_a_violations=$((shape_a_violations + 1))
            fi
        fi

        decl_idx=$((decl_idx + 1))
    done

    # Pass 3 — bare-empty ``<name>=()`` + iteration-empty footgun
    # ----------------------------------------------------------
    # Find every ``<name>=()`` line. For each, scan forward looking
    # for whichever of these comes first:
    #
    #   (a) an *unconditional* assignment ``<name>=(...)`` /
    #       ``<name>+=(...)`` — this binds the array, the scan
    #       short-circuits with verdict "assigned" (safe).
    #   (b) an iteration-form read ``${<name>[@]}`` / ``${<name>[*]}``
    #       (NOT the size form ``${#<name>[@]}``, NOT the guarded
    #       form ``${<name>[@]+...}``) — verdict "read" (flag).
    #
    # "Unconditional" means: the assignment is at the same control-
    # flow depth as the ``<name>=()`` declaration. An assignment at
    # depth > base is *conditional* — it only binds the array on
    # some code paths, so the scan continues forward looking for
    # either an unconditional bind or an iteration read. This is
    # the fix for #4479: the prior linear scan stopped at the
    # *first* ``<name>+=`` it saw, even if that ``+=`` was inside an
    # ``if`` / ``case`` / ``while`` branch that may not execute.
    # The post-#4051 ``block-on-new-issue.sh`` is the canonical
    # example: ``LABELS=()`` then ``LABELS+=("$2")`` inside a
    # ``case`` arm of an arg-parse loop, then unguarded ``for label
    # in "${LABELS[@]}"; do ...`` — runtime-broken on bash 3.2 when
    # no ``--label`` was supplied, but the pre-#4479 check missed it.
    #
    # Length-guard recognition (#4479 AC#2 — keep current scripts/
    # tree clean): an iteration read inside an ``if [ "${#<name>[@]}"
    # -gt 0 ]; then ... fi`` (or ``[[ ... ]]`` / ``-ne 0`` / ``!= 0``
    # / ``-ge 1``) block is exempt from the flag. The block is at
    # depth = base_depth + 1 and the matching ``fi`` is the first
    # ``fi`` at depth = base_depth on the line walk after the
    # opening ``if``.
    #
    # Early-exit-on-empty recognition: a closed ``if [[
    # ${#<name>[@]} -eq 0 ]]; then ... exit|return ...; fi`` block
    # before any iteration read marks the array as "guaranteed
    # non-empty after this point" — verdict short-circuits to
    # "assigned" the moment the closing ``fi`` is processed. This
    # covers the ``check-shard-coverage.sh`` style "early-exit if
    # we found nothing, then iterate".

    # ─── Pass A: precompute control-flow depth at each line ───
    # We track TWO depth counters:
    #
    #   depth_at_line[i]      — total nesting depth after line i.
    #                           Used for length-guard / early-exit
    #                           block scoping (``if [ ... ]; then
    #                           ... fi`` blocks).
    #   branch_depth_at_line[i] — depth counting ONLY ``if`` / ``case``
    #                           branches (not ``while`` / ``until`` /
    #                           ``for`` loop bodies). Used to decide
    #                           whether a ``<name>+=(...)`` is
    #                           branch-conditional (and thus does
    #                           NOT bind unconditionally) versus
    #                           inside a loop body (which we treat
    #                           as binding — the loop body runs zero
    #                           or more times, but for the static
    #                           scan we accept the loop-binding
    #                           pattern that real codebases use,
    #                           e.g. ``arr=(); while read line; do
    #                           arr+=("$line"); done; for x in
    #                           "${arr[@]}"...``). The runtime risk
    #                           there — empty input → empty array →
    #                           bash-3.2 trip — is a separate bug
    #                           class. The #4479 fix targets the
    #                           ``if``/``case`` arm shape that #4051
    #                           hit (conditional ``+=`` inside an
    #                           arg-parse ``case``).
    #
    # One-liners (``if X; then Y; fi`` / ``for X; do Y; done``) net
    # to 0 — detected by presence of the matching closer on the
    # same line.
    depth_at_line=()
    branch_depth_at_line=()
    cur_depth=0
    cur_branch_depth=0
    li=0
    while (( li < nlines )); do
        dline_text="${lines[$li]}"

        # Strip leading whitespace cheaply via parameter expansion
        # plus a regex peel — bash 3.2-safe (no ${var/#pattern/}
        # tricks needed beyond what 3.2 supports).
        trimmed="$dline_text"
        while [[ "$trimmed" == [[:space:]]* ]]; do
            trimmed="${trimmed# }"
            trimmed="${trimmed#	}"
        done

        # Skip comments and blank lines for depth tracking.
        if [[ -z "$trimmed" || "$trimmed" == \#* ]]; then
            depth_at_line+=("$cur_depth")
            branch_depth_at_line+=("$cur_branch_depth")
            li=$((li + 1))
            continue
        fi

        # Detect openers / closers. Pattern matching uses bash
        # regex (``[[ =~ ]]``) rather than ``case`` because the
        # bare word ``case`` is a reserved keyword and cannot
        # appear as a glob alternation pattern in a ``case``
        # statement.
        #
        # An "opener" is one of ``if`` / ``while`` / ``until`` /
        # ``for`` / ``case`` at start-of-trimmed-line. A "one-
        # liner" (e.g. ``if X; then Y; fi``) carries its matching
        # closer on the same line and contributes no net depth
        # change. ``elif`` is treated as continuation (no depth
        # change) of an already-open ``if`` block.
        line_delta=0
        branch_delta=0
        if [[ "$trimmed" =~ ^elif([[:space:]]|$) ]]; then
            :  # continuation, no depth change
        elif [[ "$trimmed" =~ ^(if|while|until|for)([[:space:]]|$) ]]; then
            # Opener of an if/while/until/for block. Look for a
            # matching closer on the same line — one-liner.
            opener="${BASH_REMATCH[1]}"
            closer="fi"
            case "$opener" in
                while|until|for) closer="done" ;;
            esac
            if [[ "$trimmed" =~ \;[[:space:]]*${closer}([[:space:]]|\;|$) ]] || \
               [[ "$trimmed" =~ [[:space:]]${closer}([[:space:]]|\;|$) ]]; then
                :  # one-liner, no net change
            else
                line_delta=1
                if [[ "$opener" == "if" ]]; then
                    branch_delta=1
                fi
            fi
        elif [[ "$trimmed" =~ ^case([[:space:]]|$) ]]; then
            # ``case`` opener — this counts as a branch.
            if [[ "$trimmed" =~ \;[[:space:]]*esac([[:space:]]|\;|$) ]] || \
               [[ "$trimmed" =~ [[:space:]]esac([[:space:]]|\;|$) ]]; then
                :
            else
                line_delta=1
                branch_delta=1
            fi
        elif [[ "$trimmed" =~ ^fi([[:space:]]|\;|$) ]]; then
            line_delta=-1
            branch_delta=-1
        elif [[ "$trimmed" =~ ^esac([[:space:]]|\;|$) ]]; then
            line_delta=-1
            branch_delta=-1
        elif [[ "$trimmed" =~ ^done([[:space:]]|\;|$) ]]; then
            line_delta=-1
        fi

        # Apply the delta. Note: for closers, depth_at_line[i]
        # records the depth *after* the close — i.e. the depth
        # the next line opens at.
        cur_depth=$((cur_depth + line_delta))
        if (( cur_depth < 0 )); then
            cur_depth=0  # defensive: malformed file shouldn't crash us
        fi
        cur_branch_depth=$((cur_branch_depth + branch_delta))
        if (( cur_branch_depth < 0 )); then
            cur_branch_depth=0
        fi
        depth_at_line+=("$cur_depth")
        branch_depth_at_line+=("$cur_branch_depth")
        li=$((li + 1))
    done

    # ─── Pass B: per-name forward scan ───
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
            # base_branch_depth is the ``if``/``case`` nesting at
            # the ``<name>=()`` line — used to decide whether a
            # later ``+=`` is at the same branch depth (binding)
            # or deeper (branch-conditional, not binding).
            base_branch_depth="${branch_depth_at_line[$init_idx]}"

            # Name-specific regexes. Anchoring rationale: see
            # the assign / read / guarded-form notes preserved
            # below from the prior implementation.
            iassign_regex="(^|[^A-Za-z0-9_])${iname}\\+?=\\("
            iread_regex="\\\$\\{${iname}\\[[@*]\\]\\}"
            iguarded_regex="\\\$\\{${iname}\\[[@*]\\]\\+"
            # Length-guard openers. Match both ``[ ... ]`` and
            # ``[[ ... ]]`` test forms, with optional double-quotes
            # around the size expression, and either ``-gt 0`` /
            # ``-ge 1`` / ``-ne 0`` / ``!= 0`` for "non-empty".
            ilen_guard_open_regex="^[[:space:]]*if[[:space:]]+\\[\\[?[[:space:]]+\"?\\\$\\{#${iname}\\[[@*]\\]\\}\"?[[:space:]]+(-gt[[:space:]]+0|-ge[[:space:]]+1|-ne[[:space:]]+0|!=[[:space:]]+0)[[:space:]]+\\]\\]?[[:space:]]*\\;?[[:space:]]*then"
            # Early-exit openers — same shape but checking == 0.
            iearly_exit_open_regex="^[[:space:]]*if[[:space:]]+\\[\\[?[[:space:]]+\"?\\\$\\{#${iname}\\[[@*]\\]\\}\"?[[:space:]]+(-eq[[:space:]]+0|-lt[[:space:]]+1|-le[[:space:]]+0|==[[:space:]]+0)[[:space:]]+\\]\\]?[[:space:]]*\\;?[[:space:]]*then"
            # Stop statement (exit / return) inside an early-exit block.
            istop_regex="^[[:space:]]*(exit|return)([[:space:]]|$)"

            iscan_idx=$((init_idx + 1))
            iverdict=""
            iverdict_line=0
            iverdict_content=""

            # Length-guard tracking: when we open an
            # ``if [ "${#name[@]}" -gt 0 ]`` block, record the
            # depth at which it opened. Reads inside that block
            # (depth > open_depth) are exempt. The block closes
            # when depth drops back to open_depth.
            len_guard_open_depth=-1

            # Early-exit-on-empty tracking: when we see an
            # ``if [[ ${#name[@]} -eq 0 ]]; then ... exit|return ;
            # fi`` block close cleanly, mark the array as
            # guaranteed non-empty going forward.
            in_early_exit_block=0
            early_exit_open_depth=-1
            early_exit_has_stop=0

            while (( iscan_idx < nlines )); do
                isline="${lines[$iscan_idx]}"
                idepth="${depth_at_line[$iscan_idx]}"

                # Skip comments inside the scan.
                if [[ "$isline" =~ ^[[:space:]]*# ]]; then
                    iscan_idx=$((iscan_idx + 1))
                    continue
                fi

                # ── Length-guard block close detection ──
                # If we were inside a length-guard block and depth
                # has dropped back to the open-depth, the block
                # has just closed — clear the tracker.
                if (( len_guard_open_depth >= 0 )) && (( idepth <= len_guard_open_depth )); then
                    len_guard_open_depth=-1
                fi

                # ── Length-guard block open detection ──
                # An ``if [ "${#name[@]}" -gt 0 ]; then`` line
                # opens a block at depth = idepth. Reads of
                # ``name`` inside this block are exempt.
                if [[ "$isline" =~ $ilen_guard_open_regex ]]; then
                    # idepth has already been incremented by Pass A
                    # for this opener line, so we record idepth - 1
                    # as the "outside" depth.
                    len_guard_open_depth=$((idepth - 1))
                fi

                # ── Early-exit-on-empty block tracking ──
                # If we were tracking an early-exit candidate and
                # depth has dropped back, the block has just
                # closed. If a stop statement was seen inside it,
                # short-circuit verdict to "assigned".
                if (( in_early_exit_block )) && (( idepth <= early_exit_open_depth )); then
                    if (( early_exit_has_stop )); then
                        iverdict="assigned"
                        break
                    fi
                    in_early_exit_block=0
                    early_exit_open_depth=-1
                    early_exit_has_stop=0
                fi

                # Open a fresh early-exit candidate?
                if [[ "$isline" =~ $iearly_exit_open_regex ]]; then
                    in_early_exit_block=1
                    early_exit_open_depth=$((idepth - 1))
                    early_exit_has_stop=0
                fi

                # Stop statement inside the early-exit candidate?
                if (( in_early_exit_block )) && (( idepth > early_exit_open_depth )); then
                    if [[ "$isline" =~ $istop_regex ]]; then
                        early_exit_has_stop=1
                    fi
                fi

                # ── Check assignment ──
                # An assignment ``<name>=(`` or ``<name>+=(`` is
                # treated as binding (verdict "assigned") UNLESS
                # it is inside an ``if`` / ``case`` branch deeper
                # than the declaration's branch depth. That is,
                # we accept loop-body bindings (``while`` / ``for``
                # / ``until``) as binding because real codebases
                # use ``arr=(); while read line; do arr+=...; done;
                # for x in "${arr[@]}"; do ...`` and the runtime
                # risk (empty input → empty array → bash-3.2 trip)
                # is a separate, lower-severity bug class than the
                # ``if``/``case``-arm conditional that #4051 hit.
                # The ``branch_depth`` check is what distinguishes
                # the two.
                ibranch_depth="${branch_depth_at_line[$iscan_idx]}"
                if [[ "$isline" =~ $iassign_regex ]]; then
                    if (( ibranch_depth <= base_branch_depth )); then
                        iverdict="assigned"
                        break
                    fi
                    # else: branch-conditional assignment (inside
                    # an ``if`` / ``case`` arm) — does NOT bind
                    # the array unconditionally. Continue scanning.
                fi

                # Skip lines using the guarded ``${name[@]+...}``
                # parameter-expansion idiom. The line is bash-3.2-
                # safe even when the array is empty, so do not
                # treat it as a flagged read. The scan continues
                # past the guarded line — the array may still be
                # empty afterward.
                if [[ "$isline" =~ $iguarded_regex ]]; then
                    iscan_idx=$((iscan_idx + 1))
                    continue
                fi

                # ── Check iteration read ──
                if [[ "$isline" =~ $iread_regex ]]; then
                    # If we're inside an active length-guard
                    # block for this name, the read is safe.
                    if (( len_guard_open_depth >= 0 )) && (( idepth > len_guard_open_depth )); then
                        iscan_idx=$((iscan_idx + 1))
                        continue
                    fi
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
                # Stash data for --fix mode (shape B only).
                fix_files+=("$file")
                fix_inames+=("$iname")
                fix_iter_lines+=("$iverdict_line")
            fi
        fi

        init_idx=$((init_idx + 1))
    done
done

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
