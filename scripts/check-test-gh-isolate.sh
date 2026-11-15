#!/usr/bin/env bash
# check-test-gh-isolate.sh — Fail when a test can reach real GitHub (#5071).
#
# A test that runs the real `gh` (directly, or through a script such as
# sweep-completed-parents.sh) acts on real GitHub with the developer's login.
# In #5071 a dispatcher test closed an open issue and posted comments that
# way; in CI gh is not logged in, so the call failed quietly and nobody
# noticed. Tests are cut off from GitHub at the source:
#
#   * pytest: a session fixture in each test root's conftest.py, between the
#     `# --- BEGIN gh-isolate (#5071) ---` and `# --- END ...` markers. The
#     canonical copy is scripts/tests/conftest.py.
#   * shell tests: `. .../_gh-isolate.sh` (scripts/tests/_gh-isolate.sh) at
#     the top of the file.
#
# Both empty GH_CONFIG_DIR, unset the GitHub token variables, and put a `gh`
# shim first on PATH that exits 4 with "real gh called from a test".
#
# What it checks
#   1. Every test_*.py under a tests/ directory has a conftest.py with the
#      gh-isolate block in one of its parent directories (up to the repo
#      root). Unlike git writes, a gh call cannot be spotted textually (it
#      hides behind scripts and helpers), so every Python test is in scope.
#   2. Every conftest.py copy of the block is identical to the canonical one.
#   3. Every test_*.sh under a tests/ directory sources _gh-isolate.sh at top
#      level.
#
# Usage:
#   scripts/check-test-gh-isolate.sh             # scan the repo
#   scripts/check-test-gh-isolate.sh <repo_root> # scan another tree (tests)
#
# Exit codes:
#   0 — every test is cut off from GitHub and every copy matches.
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

BEGIN_MARK='# --- BEGIN gh-isolate (#5071) ---'
END_MARK='# --- END gh-isolate (#5071) ---'
CANON="scripts/tests/conftest.py"
HELPER="scripts/tests/_gh-isolate.sh"
SH_SOURCE_RE='^(\.|source)[[:space:]].*_gh-isolate\.sh'

# Print the block (markers included) from a file.
extract_block() {
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0 == b { on = 1 }
        on { print }
        $0 == e { on = 0 }
    ' "$1"
}

# List test files with this name pattern inside a tests/ directory.
find_tests() {
    local prune=() dir
    for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
        prune+=(-name "$dir" -o)
    done
    find . \( "${prune[@]}" -false \) -prune -o -type f -name "$1" -print 2>/dev/null \
        | sed 's#^\./##' | grep -E '(^|/)tests/' | sort || true
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
has_isolate_conftest() {
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
        echo "_gh-isolate.sh"
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
        echo "FAIL: tests can reach real GitHub through gh (#5071):" >&2
    fi
    failures=$((failures + 1))
    echo "  - $1" >&2
}

py_missing=()
py_drift=()
sh_missing=()

# 1. Every Python test root has the conftest block in scope. Report each
#    uncovered tests/ root once, naming one example file.
seen_roots=" "
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    has_isolate_conftest "$f" && continue
    root="$(tests_root_of "$f")"
    [[ "$seen_roots" == *" $root "* ]] && continue
    seen_roots="$seen_roots$root "
    report "$root/ has Python tests (e.g. $f) but no conftest.py above them has the gh-isolate block"
    py_missing+=("$root/conftest.py")
done < <(find_tests 'test_*.py')

# 2. Every copy of the block matches the canonical one.
if [[ ! -f "$CANON" ]] || ! grep -qxF "$BEGIN_MARK" "$CANON"; then
    report "$CANON (the canonical copy) is missing the gh-isolate block"
else
    canon_block="$(extract_block "$CANON")"
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        f="${f#./}"
        [[ "$f" == "$CANON" ]] && continue
        if [[ "$(extract_block "$f")" != "$canon_block" ]]; then
            report "$f has a gh-isolate block that differs from $CANON"
            py_drift+=("$f")
        fi
    done < <(grep -rlxF "$BEGIN_MARK" . --include='conftest.py' "${exclude_args[@]}" 2>/dev/null | sort)
fi

# 3. Every shell test sources the helper at top level.
while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if ! grep -qE "$SH_SOURCE_RE" "$f"; then
        report "$f does not source _gh-isolate.sh"
        sh_missing+=("$f")
    fi
done < <(find_tests 'test_*.sh')

if [[ "$failures" -eq 0 ]]; then
    echo "OK: every test is cut off from real GitHub (#5071)"
    exit 0
fi

echo "" >&2
echo "Fix:" >&2
if [[ "${#py_missing[@]}" -gt 0 ]]; then
    echo "  Copy the lines from '$BEGIN_MARK' to '$END_MARK'" >&2
    echo "  in $CANON into each conftest.py below (create it if missing)," >&2
    echo "  after its imports (the block needs 'import os' and 'import pytest'):" >&2
    printf '%s\n' "${py_missing[@]}" | sort -u | sed 's/^/    /' >&2
fi
if [[ "${#py_drift[@]}" -gt 0 ]]; then
    echo "  Make the block identical to $CANON (edit the canonical copy, then copy it):" >&2
    for f in "${py_drift[@]}"; do
        echo "    diff <(sed -n '/^# --- BEGIN gh-isolate/,/^# --- END gh-isolate/p' $CANON) \\" >&2
        echo "         <(sed -n '/^# --- BEGIN gh-isolate/,/^# --- END gh-isolate/p' $f)" >&2
    done
fi
if [[ "${#sh_missing[@]}" -gt 0 ]]; then
    echo "  Add this line right after 'set -euo pipefail' (after the _git-env-scrub.sh line, if any):" >&2
    for f in "${sh_missing[@]}"; do
        echo "    $f:" >&2
        echo "      . \"\$(dirname \"\${BASH_SOURCE[0]}\")/$(helper_path_from "$f")\"  # no real GitHub from tests (#5071)" >&2
    done
fi
exit 1
