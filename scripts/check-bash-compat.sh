#!/usr/bin/env bash
# check-bash-compat.sh — Forbid bash 4+ constructs in ``scripts/**/*.sh``
# so shell scripts remain executable on operator laptops running macOS's
# stock bash 3.2.
#
# Why this check exists
# ---------------------
# Operator laptops run macOS, which ships with bash 3.2.57 (Apple has
# frozen the OS bash at 3.2 since 2007 for GPLv3 licensing reasons).
# Shell scripts in this repo — especially ``scripts/check-*.sh`` hygiene
# guards — are invoked both from CI (ubuntu-latest, bash 5+) and from
# operator shells. A script that uses a bash 4+ feature passes CI but
# fails locally with a cryptic error:
#
#     $ scripts/check-foo.sh
#     /bin/bash: mapfile: command not found
#     exit 127
#
# The #3081 PR (the ``check-terminal-routing-comments.sh`` guard) hit
# this on its first draft — ``mapfile -t`` silently exited 127 on the
# operator's laptop. The bug was caught only by grepping other check
# scripts for the repo's unwritten convention; two existing guards
# (``check-bare-shadcn-accent.sh`` — formerly the narrow
# ``check-admin-dispatcher-brand-accent.sh`` retired in #2832 — and
# ``check-no-inline-ecs-healthcheck.sh``) had inline comments noting
# the constraint, but it was not codified anywhere enforceable. Issue
# #3082 codifies it: this check + the ``docs/agent/code-standards.md``
# §macOS bash 3.2 compatibility note.
#
# Forbidden constructs
# --------------------
# Each of the following is a bash 4+ feature that emits either
# ``command not found`` (builtins) or ``bad substitution`` / ``syntax
# error`` (syntax) on bash 3.2:
#
#   1. ``mapfile`` / ``readarray`` (bash 4.0+ builtin) — read lines
#      into an array. Use ``while IFS= read -r line; do arr+=("$line");
#      done < <(cmd)`` instead.
#
#   2. ``declare -A`` / ``typeset -A`` (bash 4.0+) — associative arrays.
#      Use parallel indexed arrays, namespaced variables (``var_foo``,
#      ``var_bar``), or a temp file + grep.
#
#   3. ``declare -g`` (bash 4.2+) — declare a global from inside a
#      function. Use plain ``VAR=value`` at global scope, or write to
#      stdout and ``VAR=$(fn)`` at the caller.
#
#   4. ``${var,,}`` / ``${var,}`` / ``${var^^}`` / ``${var^}`` (bash
#      4.0+) — case-modifying parameter expansion. Use
#      ``tr '[:upper:]' '[:lower:]'`` (or ``[:lower:]`` → ``[:upper:]``)
#      instead.
#
#   5. ``local -n`` / ``declare -n`` / ``typeset -n`` (bash 4.3+) —
#      nameref variables. Pass the value by expansion, not by name, or
#      use ``eval`` with strict quoting if a reference really is
#      needed (rare).
#
#   6. ``;;&`` in ``case`` (bash 4.0+) — fall-through to the next
#      pattern. Split into separate ``if`` arms or repeat the body.
#
#   7. ``|&`` pipe shorthand (bash 4.0+) — pipe both stdout and stderr.
#      Use ``2>&1 |`` instead (works on bash 3.2+ and is more explicit).
#
# NOT forbidden (works on bash 3.2):
#   - ``echo -e`` — bash 3.2's ``echo`` builtin supports ``-e``.
#   - Process substitution ``< <(...)`` — bash 3.2 supports it.
#   - ``[[ ... ]]`` conditional — bash 2+.
#   - ``$(...)`` command substitution — bash 2+.
#   - Indexed arrays ``arr=(a b c)`` + ``"${arr[@]}"`` — bash 2+.
#
# Pattern matching strategy
# -------------------------
# The check is text-based (not AST-based) so it runs in a few hundred
# milliseconds on a cold macOS laptop. Each construct has its own
# ``grep -nE`` pattern tuned to minimise false positives:
#
#   - Builtins (mapfile, readarray, declare -A, declare -g, local -n,
#     declare -n, typeset -n, typeset -A) require a leading word
#     boundary so the name is the start of a statement — not a
#     substring of some longer token (e.g. ``my_mapfile_wrapper``).
#
#   - Parameter expansions (``${var,,}`` etc.) use the exact sigil form.
#     The check does not flag ``${var,default}`` because that pattern
#     simply isn't valid anywhere (bash has no such form) — any
#     ``${x,...}`` you see in the wild is a case-conversion.
#
#   - Syntax shorthands (``;;&``, ``|&``) match the literal token
#     surrounded by whitespace or line boundaries.
#
# Comment lines (first non-whitespace char is ``#``) are excluded, so
# docstrings and explanatory prose may reference the forbidden tokens
# freely. The check script and its test file are allow-listed by path
# for the same reason.
#
# Usage
# -----
#   scripts/check-bash-compat.sh          # scan scripts/ under repo root
#   scripts/check-bash-compat.sh [dir]    # scan a specific directory
#
# Exit codes
# ----------
#   0 — No bash 4+ constructs detected in tracked shell scripts.
#   1 — One or more shell scripts use a forbidden construct. The
#       offending lines are printed with file:line:content plus a
#       pointer to docs/agent/code-standards.md §macOS bash 3.2
#       compatibility.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── Files that legitimately mention the forbidden tokens ────────────────
# The check script itself and its test must spell the tokens literally.
# scripts/check-terminal-routing-comments.sh,
# scripts/check-bare-shadcn-accent.sh, and
# scripts/check-no-inline-ecs-healthcheck.sh each have a comment
# explaining that they avoid ``mapfile`` for bash 3.2 reasons — those
# comments are excluded via the generic comment-line filter, not by
# allow-listing.
EXCLUDE_FILES=(
    "scripts/check-bash-compat.sh"
    "scripts/tests/test_check_bash_compat.sh"
)

# ─── Directories to exclude from the scan ────────────────────────────────
# Anything outside scripts/ is not our concern for this check. Within
# scripts/, exclude vendored/cache dirs that may contain shell fixtures
# we do not author.
EXCLUDE_DIRS=(
    ".git"
    ".venv"
    "node_modules"
    "__pycache__"
    # Local developer scratch (not tracked).
    "tmp"
)

# ─── The forbidden-construct patterns ────────────────────────────────────
# Each entry is: <label>\t<extended-regex>.
# The label is used for human-readable reporting; the regex is passed
# to ``grep -nE``.
#
# Bash 3.2 doesn't support associative arrays (the first thing this
# check forbids!), so we use parallel indexed arrays instead — which
# is itself the recommended workaround documented in
# docs/agent/code-standards.md §macOS bash 3.2 compatibility.
PATTERN_LABELS=(
    "mapfile (bash 4+ builtin)"
    "readarray (bash 4+ builtin)"
    "declare -A (associative array, bash 4+)"
    "typeset -A (associative array, bash 4+)"
    "declare -g (global declare, bash 4.2+)"
    "local -n (nameref, bash 4.3+)"
    "declare -n (nameref, bash 4.3+)"
    "typeset -n (nameref, bash 4.3+)"
    "\${var,,} / \${var,} (lowercase expansion, bash 4+)"
    "\${var^^} / \${var^} (uppercase expansion, bash 4+)"
    ";;& (case fall-through, bash 4+)"
    "|& (stderr pipe shorthand, bash 4+)"
)

# Per-construct rewrite suggestions (#4346). Each entry mirrors
# PATTERN_LABELS by index. Bash 3.2 has no associative arrays — see
# PATTERN_LABELS comment — so we use a parallel indexed array.
PATTERN_FIXES=(
    "Use: while IFS= read -r line; do arr+=(\"\$line\"); done < <(cmd)"
    "Use: while IFS= read -r line; do arr+=(\"\$line\"); done < <(cmd)"
    "Use parallel indexed arrays (KEYS=(...) + VALS=(...)) or namespaced vars."
    "Use parallel indexed arrays (KEYS=(...) + VALS=(...)) or namespaced vars."
    "Use plain VAR=value at global scope, or write to stdout and VAR=\$(fn) at the caller."
    "Pass the value by expansion, not by name. Or use 'eval' with strict quoting."
    "Pass the value by expansion, not by name. Or use 'eval' with strict quoting."
    "Pass the value by expansion, not by name. Or use 'eval' with strict quoting."
    "Use: lower=\$(echo \"\$var\" | tr '[:upper:]' '[:lower:]')"
    "Use: upper=\$(echo \"\$var\" | tr '[:lower:]' '[:upper:]')"
    "Split into separate 'if' arms or repeat the case body across patterns."
    "Use '2>&1 |' instead — works on bash 3.2+ and is more explicit."
)

# Leading word boundary: start-of-line or whitespace or ``;``/``&``/
# ``(``. This keeps ``my_mapfile_wrapper`` and ``foo.readarray`` from
# tripping the check while catching ``mapfile -t`` at the start of a
# line or after ``if ``/``then ``/``|``/``;``.
_LB='(^|[[:space:];&|()])'

PATTERN_REGEXES=(
    "${_LB}mapfile([[:space:]]|$)"
    "${_LB}readarray([[:space:]]|$)"
    "${_LB}declare[[:space:]]+-[a-zA-Z]*A"
    "${_LB}typeset[[:space:]]+-[a-zA-Z]*A"
    "${_LB}declare[[:space:]]+-[a-zA-Z]*g"
    "${_LB}local[[:space:]]+-[a-zA-Z]*n"
    "${_LB}declare[[:space:]]+-[a-zA-Z]*n"
    "${_LB}typeset[[:space:]]+-[a-zA-Z]*n"
    '\$\{[A-Za-z_][A-Za-z0-9_]*,[,]?\}'
    '\$\{[A-Za-z_][A-Za-z0-9_]*\^[\^]?\}'
    ';;&'
    '[^|]\|&([[:space:]]|$)'
)

# ─── Locate the shell scripts to scan ────────────────────────────────────
# Only scripts/**/*.sh is in scope. Anything under scripts/archive/ or
# scripts/tests/fixtures/ is still scanned — agents learning from the
# test fixtures should see compliant patterns there too.
SCRIPTS_DIR="$SCAN_DIR/scripts"
if [[ ! -d "$SCRIPTS_DIR" ]]; then
    # When invoked against a test TMPDIR that doesn't have a scripts/
    # subdir, treat SCAN_DIR itself as the target. This lets the test
    # harness construct minimal fixtures without mirroring the repo
    # layout.
    SCRIPTS_DIR="$SCAN_DIR"
fi

# Build a sorted list of *.sh files, excluding directories in
# EXCLUDE_DIRS. Uses a while-read loop (not mapfile — see rule #1!).
#
# The find invocation uses the ``-prune`` idiom: for each excluded
# directory name, ``-name <dir> -type d -prune -o`` short-circuits
# the descent before printing. Ordering matters — the prune clauses
# must come before the match clause so the prune applies first.
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
    echo "check-bash-compat: no *.sh files found under $SCRIPTS_DIR — nothing to check."
    exit 0
fi

# ─── Scan every file × every pattern ─────────────────────────────────────
violations=0
# Accumulate matches into a single text buffer so we can print them
# grouped by construct at the end. Again — indexed arrays, not
# associative.
report_lines=()

for file in "${sh_files[@]}"; do
    # Skip allow-listed files by path suffix.
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

    # Walk each pattern.
    idx=0
    while (( idx < ${#PATTERN_REGEXES[@]} )); do
        label="${PATTERN_LABELS[$idx]}"
        regex="${PATTERN_REGEXES[$idx]}"

        # grep -nE emits ``lineno:content``. The leading filename is
        # added by grep's ``-H`` when multiple files are provided; here
        # we pass a single file and prepend the filename ourselves to
        # keep the output format consistent.
        matches=$(grep -nE "$regex" "$file" 2>/dev/null || true)
        if [[ -z "$matches" ]]; then
            idx=$((idx + 1))
            continue
        fi

        while IFS= read -r m; do
            [[ -z "$m" ]] && continue
            lineno="${m%%:*}"
            content="${m#*:}"

            # Skip comment lines — the first non-whitespace character
            # is ``#``. This lets this script, peer check scripts, and
            # test fixtures discuss the forbidden tokens in prose.
            if [[ "$content" =~ ^[[:space:]]*# ]]; then
                continue
            fi

            report_lines+=("  [$label]")
            report_lines+=("    $file:$lineno: $content")
            violations=$((violations + 1))
        done <<< "$matches"

        idx=$((idx + 1))
    done
done

if (( violations > 0 )); then
    echo "ERROR: bash 4+ construct(s) detected in scripts/**/*.sh."
    echo ""
    echo "  These scripts must run on macOS's stock bash 3.2. Each"
    echo "  forbidden construct has a bash 3.2-compatible rewrite — see"
    echo "  docs/agent/code-standards.md §macOS bash 3.2 compatibility."
    echo ""
    for line in "${report_lines[@]}"; do
        echo "$line"
    done
    echo ""
    echo "  Total violations: $violations"
    echo ""
    # Per-construct Fix block (#4346). Show the rewrite for every
    # distinct label that appeared in the report. We deduplicate via a
    # simple "have we already shown this label?" check.
    echo "  Fix:"
    fix_idx=0
    while (( fix_idx < ${#PATTERN_LABELS[@]} )); do
        fix_label="${PATTERN_LABELS[$fix_idx]}"
        fix_text="${PATTERN_FIXES[$fix_idx]}"
        # Did this label appear in the report?
        if printf '%s\n' "${report_lines[@]}" | grep -qF "[$fix_label]"; then
            echo "    [$fix_label]"
            echo "      $fix_text"
        fi
        fix_idx=$((fix_idx + 1))
    done
    echo ""
    echo "  If a construct must appear as explanatory context (e.g."
    echo "  \"we avoid mapfile because ...\"), put it in a comment line"
    echo "  (first non-whitespace char is '#'). Comments are exempted."
    exit 1
fi

echo "check-bash-compat: ${#sh_files[@]} shell script(s) scanned — all clean."
exit 0
