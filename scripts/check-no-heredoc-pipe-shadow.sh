#!/usr/bin/env bash
# check-no-heredoc-pipe-shadow.sh — Forbid the silent-miscompile pattern
# `... | python3 << TAG` whose Python body reads stdin via
# `json.load(sys.stdin)` / `sys.stdin.read()` / `sys.stdin.readlines()`.
#
# The bug: when bash sees both a pipe predecessor AND a heredoc on the
# same `python3` invocation, the heredoc wins as the process's stdin
# and the piped data is silently discarded. A `json.load(sys.stdin)`
# body then attempts to parse the Python source itself as JSON and
# raises `JSONDecodeError: Expecting value: line 1 column 1`. The
# error is runtime-only — no syntax warning, no shellcheck rule, no
# failing test until the script runs against a real input.
#
# The fix is one of:
#   1. Pipe via stdin **without** a heredoc:
#        echo "$X" | python3 -c '<single-line code>'
#   2. Heredoc **without** a pipe (Python reads the heredoc as code,
#      not data); pass data via argv or env var.
#   3. Heredoc + tmpfile — write the payload to a tmp file, pass path
#      as argv. See scripts/ecs-wait-task.sh for the canonical worked
#      example (the script that surfaced this footgun — issue #4267).
#
# Usage:
#   scripts/check-no-heredoc-pipe-shadow.sh          # scan repo root
#   scripts/check-no-heredoc-pipe-shadow.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more tracked files contain the bad pattern.
#
# See issue #4267 for the original report and #4252 for the
# scripts/ecs-wait-task.sh PR that surfaced the failure mode.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# ─── Directories to exclude from the scan ────────────────────────────────
EXCLUDE_DIRS=(
    ".git"
    ".venv"
    "node_modules"
    ".next"
    "__pycache__"
    ".vite"
    "tmp"
)

# ─── Files that legitimately mention the pattern as context ──────────────
EXCLUDE_FILES=(
    "scripts/check-no-heredoc-pipe-shadow.sh"
    "scripts/tests/test_check_no_heredoc_pipe_shadow.sh"
)

# ─── Build find arguments ───────────────────────────────────────────────

prune_args=()
first=true
for dir in "${EXCLUDE_DIRS[@]}"; do
    if $first; then
        prune_args+=("(" "-name" "$dir")
        first=false
    else
        prune_args+=("-o" "-name" "$dir")
    fi
done
prune_args+=(")" "-prune" "-o")

# Limit to shell-ish files. Markdown/YAML excluded — those cannot
# literally execute, so any heredoc pattern there is documentation.
SCAN_TARGETS=()
while IFS= read -r f; do
    SCAN_TARGETS+=("$f")
done < <(
    find "$SCAN_DIR" \
        "${prune_args[@]}" \
        -type f \
        \( -name "*.sh" -o -name "*.bash" \) \
        -print
)

# ─── Per-file scanner (pure bash, portable to bash 3.2 / macOS) ─────────
#
# State machine:
#   in_heredoc == 0: look for `... | python3 [-flags]* <<[-]['"]?TAG['"]?`
#                    on the same logical line. If matched: capture TAG,
#                    record start lineno + start line.
#   in_heredoc == 1: look for terminator (line == TAG, with leading
#                    whitespace allowed iff `<<-` was used).  In the
#                    body, look for `json.load(sys.stdin)` /
#                    `sys.stdin.read()` / `sys.stdin.readlines()`.
#                    On terminator: if dirty, print start line.
#
# Returns:
#   0 — file clean (or no heredoc-pipe pattern)
#   1 — at least one violation; violation lines emitted on stdout.
#
# The regex matches python3 invocations that have BOTH a pipe
# predecessor (`|`) and a heredoc redirection (`<<`). We deliberately
# do not match `python3 <<...` without a leading pipe — that form
# is safe (Python reads the heredoc as source code; stdin is unset).
HEREDOC_START_RE='\|[[:space:]]*python3([[:space:]]+-[A-Za-z]+)*[[:space:]]*<<-?[[:space:]]*'\''?"?([A-Za-z_][A-Za-z0-9_]*)"?'\''?'

scan_file() {
    local path="$1"
    local in_heredoc=0
    local dirty=0
    local tag=""
    local start_lineno=0
    local start_line=""
    local dash_form=0
    local lineno=0
    local line
    local file_violations=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        if [[ $in_heredoc -eq 0 ]]; then
            if [[ "$line" =~ $HEREDOC_START_RE ]]; then
                tag="${BASH_REMATCH[2]}"
                if [[ "$line" == *"<<-"* ]]; then
                    dash_form=1
                else
                    dash_form=0
                fi
                in_heredoc=1
                dirty=0
                start_lineno=$lineno
                start_line="$line"
            fi
            continue
        fi
        # in_heredoc — check terminator first.
        if [[ $dash_form -eq 1 ]]; then
            # POSIX <<- allows leading TABs (and we accept any whitespace).
            local stripped="${line#"${line%%[! 	]*}"}"
            if [[ "$stripped" == "$tag" ]]; then
                if [[ $dirty -eq 1 ]]; then
                    printf '%s:%d: %s\n' "$path" "$start_lineno" "$start_line"
                    file_violations=$((file_violations + 1))
                fi
                in_heredoc=0
                dirty=0
                tag=""
                start_lineno=0
                start_line=""
                dash_form=0
                continue
            fi
        else
            if [[ "$line" == "$tag" ]]; then
                if [[ $dirty -eq 1 ]]; then
                    printf '%s:%d: %s\n' "$path" "$start_lineno" "$start_line"
                    file_violations=$((file_violations + 1))
                fi
                in_heredoc=0
                dirty=0
                tag=""
                start_lineno=0
                start_line=""
                dash_form=0
                continue
            fi
        fi
        # Body line — check for stdin reads.
        if [[ "$line" == *"json.load(sys.stdin)"* \
            || "$line" == *"sys.stdin.read()"* \
            || "$line" == *"sys.stdin.readlines()"* ]]; then
            dirty=1
        fi
    done < "$path"

    # Unterminated heredoc with a dirty body — also flag.
    if [[ $in_heredoc -eq 1 && $dirty -eq 1 ]]; then
        printf '%s:%d: %s\n' "$path" "$start_lineno" "$start_line"
        file_violations=$((file_violations + 1))
    fi

    if [[ $file_violations -gt 0 ]]; then
        return 1
    fi
    return 0
}

violations=0
violation_lines=""

if [[ ${#SCAN_TARGETS[@]} -gt 0 ]]; then
    for f in "${SCAN_TARGETS[@]}"; do
        # Skip explicitly-excluded files.
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$f" == *"$excl" ]]; then
                skip=true
                break
            fi
        done
        if "$skip"; then
            continue
        fi

        # Skip docs/investigations/* — post-mortems may reference the
        # pattern as historical context.
        if [[ "$f" == *"docs/investigations/"* ]]; then
            continue
        fi

        out=$(scan_file "$f" || true)
        if [[ -n "$out" ]]; then
            violation_lines+="$out"$'\n'
            count=$(printf '%s' "$out" | grep -c '^' || true)
            violations=$((violations + count))
        fi
    done
fi

if [[ $violations -gt 0 ]]; then
    echo "ERROR: Found forbidden 'pipe + python3 <<HEREDOC + stdin-read' pattern."
    echo ""
    echo "  When a 'python3' invocation has BOTH a pipe predecessor AND a"
    echo "  heredoc, bash gives the heredoc precedence as Python's stdin."
    echo "  The piped data is silently discarded, so 'json.load(sys.stdin)'"
    echo "  parses the Python source itself as JSON and raises"
    echo "  'JSONDecodeError: Expecting value: line 1 column 1'."
    echo ""
    echo "  See issue #4267 for the original report. See"
    echo "  scripts/ecs-wait-task.sh for the canonical fix recipe (heredoc"
    echo "  + tmpfile + argv) that surfaced this footgun (issue #4252)."
    echo ""
    printf '%s' "$violation_lines"
    echo ""
    echo "  Fix recipes:"
    echo "    1. Pipe + python3 -c (no heredoc):"
    echo "       echo \"\$X\" | python3 -c '<single-line code>'"
    echo "    2. Heredoc + argv / env (no pipe)."
    echo "    3. Heredoc + tmpfile + argv (most flexible) — see"
    echo "       scripts/ecs-wait-task.sh for the worked example."
    echo ""
    echo "  Found $violations occurrence(s)."
    exit 1
fi

echo "All clean — no 'pipe + python3 <<HEREDOC + stdin-read' usage detected."
exit 0
