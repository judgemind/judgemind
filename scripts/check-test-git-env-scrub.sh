#!/usr/bin/env bash
# check-test-git-env-scrub.sh — Fail when a test that runs git can
# inherit git's repository env vars (issue #5058).
#
# `git push` (pre-push hook), `git rebase --exec`, `git bisect run` and `!`
# aliases run their command with GIT_DIR (and, in a linked worktree, more
# GIT_* vars) exported. A test that runs `git init` / `commit` / `config` /
# `update-ref` in a scratch directory inherits them and writes to THAT
# repository: in #5045 a push set core.bare=true in the shared .git and
# moved main. Tests are protected at the source:
#
#   * pytest: a session fixture in each test root's conftest.py, between
#     the `# --- BEGIN git-env-scrub (#5058) ---` and `# --- END ...`
#     markers. The canonical copy is scripts/tests/conftest.py.
#   * shell tests: `. .../_git-env-scrub.sh` (scripts/tests/_git-env-scrub.sh)
#     at the top of the file.
#
# What it checks
#   1. Every test_*.py under a tests/ directory that runs git has a
#      conftest.py with the git-env-scrub block in one of its parent
#      directories (up to the repo root).
#   2. Every conftest.py copy of the block is identical to the canonical one.
#   3. Every test_*.sh under a tests/ directory that runs git sources
#      _git-env-scrub.sh at top level.
#
# Any git invocation counts, not only a list of write verbs (#5089): clone,
# checkout -b, add, push, worktree add, reset ... all write, and even a read
# with GIT_DIR exported reads the wrong repository. It is detected
# textually, on non-comment lines:
#   * Python: a "git" string ("git" list element, shutil.which("git")), a
#     _git( helper call, a string that starts with "git <arg>" (an f-string
#     or a shell=True command), or "git" after && ; | inside a string.
#   * shell: git in command position (line start, after ; & | ( { ` ! $( ,
#     then / do / else / exec / command / time / env, or VAR=x prefixes).
# A file that only mocks git is flagged too; scrubbing there is harmless.
#
# Usage:
#   scripts/check-test-git-env-scrub.sh             # scan the repo
#   scripts/check-test-git-env-scrub.sh <repo_root> # scan another tree (tests)
#
# Exit codes:
#   0 — every git-running test is covered and every copy matches.
#   1 — one or more violations (listed with a Fix: block).

# venv: none
# permanent: true

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO_ROOT"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

BEGIN_MARK='# --- BEGIN git-env-scrub (#5058) ---'
END_MARK='# --- END git-env-scrub (#5058) ---'
CANON="scripts/tests/conftest.py"
HELPER="scripts/tests/_git-env-scrub.sh"

PY_GIT_RE="[\"']git[\"']|_git\\(|[\"']git[[:space:]]+[-a-z]|(&&|;|\\|)[[:space:]]*git[[:space:]]+[-a-z]"
SH_GIT_RE='(^|[;&|({`!]|\$\(|(then|do|else|exec|command|time|env)[[:space:]])[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git[[:space:]]+(-|[a-z])'
SH_SOURCE_RE='^(\.|source)[[:space:]].*_git-env-scrub\.sh'
COMMENT_RE='^[[:space:]]*#'

# True when file $1 matches ERE $2 on a line that is not a comment.
code_matches() {
    grep -vE "$COMMENT_RE" "$1" | grep -qE "$2"
}

# Print the block (markers included) from a file.
extract_block() {
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { on = 1 }
        on { print }
        $0 == e { on = 0 }
    ' "$1"
}

# Keep only paths inside a tests/ directory.
in_tests_dir() {
    grep -E '(^|/)tests/' || true
}

# The outermost tests/ directory on a path (where its conftest.py belongs).
tests_root_of() {
    local path="$1" prefix
    prefix="${path%%/tests/*}"
    if [[ "$path" == tests/* ]]; then
        echo "tests"
    elif [[ "$prefix" != "$path" ]]; then
        echo "$prefix/tests"
    else
        dirname "$path"
    fi
}

# Walk up from a file's directory looking for a conftest.py with the block.
has_scrub_conftest() {
    local dir
    dir="$(dirname "$1")"
    while :; do
        if [[ -f "$dir/conftest.py" ]] && grep -qxF "$BEGIN_MARK" "$dir/conftest.py"; then
            return 0
        fi
        [[ "$dir" == "." || "$dir" == "/" ]] && return 1
        dir="$(dirname "$dir")"
    done
}

# Relative path from a file's directory to the shell helper.
helper_path_from() {
    local dir rel=""
    dir="$(dirname "$1")"
    if [[ "$dir" == "scripts/tests" ]]; then
        echo "_git-env-scrub.sh"
        return
    fi
    while [[ "$dir" != "." ]]; do
        rel="../$rel"
        dir="$(dirname "$dir")"
    done
    echo "${rel}${HELPER}"
}

failures=0
report() {
    if [[ "$failures" -eq 0 ]]; then
        echo "FAIL: tests can inherit git's repository env vars (GIT_DIR etc., #5058):" >&2
    fi
    failures=$((failures + 1))
    echo "  - $1" >&2
}

py_missing=()
py_drift=()
sh_missing=()

# 1. Python tests that run git need the conftest block in scope.
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    f="${f#./}"
    code_matches "$f" "$PY_GIT_RE" || continue
    if ! has_scrub_conftest "$f"; then
        report "$f runs git but no conftest.py above it has the git-env-scrub block"
        py_missing+=("$(tests_root_of "$f")/conftest.py")
    fi
done < <(grep -rlE "$PY_GIT_RE" . --include='test_*.py' "${exclude_args[@]}" 2>/dev/null | in_tests_dir | sort)

# 2. Every copy of the block matches the canonical one.
if [[ ! -f "$CANON" ]] || ! grep -qxF "$BEGIN_MARK" "$CANON"; then
    report "$CANON (the canonical copy) is missing the git-env-scrub block"
else
    canon_block="$(extract_block "$CANON")"
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        f="${f#./}"
        [[ "$f" == "$CANON" ]] && continue
        if [[ "$(extract_block "$f")" != "$canon_block" ]]; then
            report "$f has a git-env-scrub block that differs from $CANON"
            py_drift+=("$f")
        fi
    done < <(grep -rlxF "$BEGIN_MARK" . --include='conftest.py' "${exclude_args[@]}" 2>/dev/null | sort)
fi

# 3. Shell tests that run git source the helper at top level.
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    f="${f#./}"
    code_matches "$f" "$SH_GIT_RE" || continue
    if ! grep -qE "$SH_SOURCE_RE" "$f"; then
        report "$f runs git but does not source _git-env-scrub.sh"
        sh_missing+=("$f")
    fi
done < <(grep -rlE "$SH_GIT_RE" . --include='test_*.sh' "${exclude_args[@]}" 2>/dev/null | in_tests_dir | sort)

if [[ "$failures" -eq 0 ]]; then
    echo "OK: every git-running test scrubs git's repository env vars (#5058)"
    exit 0
fi

echo "" >&2
echo "Fix:" >&2
if [[ "${#py_missing[@]}" -gt 0 ]]; then
    echo "  Copy the lines from '$BEGIN_MARK' to '$END_MARK'" >&2
    echo "  in $CANON into each conftest.py below, after its imports" >&2
    echo "  (the block needs 'import subprocess' and 'import pytest'):" >&2
    printf '%s\n' "${py_missing[@]}" | sort -u | sed 's/^/    /' >&2
fi
if [[ "${#py_drift[@]}" -gt 0 ]]; then
    echo "  Make the block identical to $CANON (edit the canonical copy, then copy it):" >&2
    for f in "${py_drift[@]}"; do
        echo "    diff <(sed -n '/^# --- BEGIN git-env-scrub/,/^# --- END git-env-scrub/p' $CANON) \\" >&2
        echo "         <(sed -n '/^# --- BEGIN git-env-scrub/,/^# --- END git-env-scrub/p' $f)" >&2
    done
fi
if [[ "${#sh_missing[@]}" -gt 0 ]]; then
    echo "  Add this line right after 'set -euo pipefail' (or 'set -uo pipefail'):" >&2
    for f in "${sh_missing[@]}"; do
        echo "    $f:" >&2
        echo "      . \"\$(dirname \"\${BASH_SOURCE[0]}\")/$(helper_path_from "$f")\"  # unset inherited GIT_DIR etc. (#5058)" >&2
    done
fi
exit 1
