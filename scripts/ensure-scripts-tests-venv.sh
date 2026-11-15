#!/usr/bin/env bash
# ensure-scripts-tests-venv.sh — the cached minimal venv scripts/tests runs in.
#
# permanent: true
#
# Prints the path of a per-checkout venv holding exactly the packages in
# scripts/tests/requirements-ci.txt, the file CI's `scripts-tests` job
# installs from. All messages go to stderr. The pre-push hook runs
# `pytest scripts/tests/` in this venv so an import the minimal CI
# environment cannot satisfy (e.g. a script importing a package whose
# __init__ pulls in psycopg) fails locally, not one CI round trip later
# (#5045). Before #5045 the hook did not run scripts/tests at all, and
# agents ran it by hand in a full package venv where psycopg was present.
#
# One venv per checkout, reused across pushes:
#   path         <checkout>/tmp/scripts-tests-venv (tmp/ is gitignored
#                and on every repo-walk exclusion list)
#   fingerprint  <venv>/.scripts-tests-fingerprint: sha256 of the
#                requirements file, packages/judgemind-config/pyproject.toml
#                and the interpreter version.
# When the fingerprint matches, the venv is reused as is (well under a
# second). When it differs, or a build was interrupted (no fingerprint),
# the venv is deleted and rebuilt (a few seconds with a warm pip cache).
#
# Usage:
#   scripts/ensure-scripts-tests-venv.sh          # print the venv path
#
# Environment:
#   SCRIPTS_TESTS_VENV    venv path (default <checkout>/tmp/scripts-tests-venv)
#   SCRIPTS_TESTS_PYTHON  interpreter to build it with (default python3.12,
#                         CI's version; python3 when that is missing and is 3.12+)
#
# Exit codes:
#   0  venv path printed on stdout
#   1  error: pip could not install the requirements. A FAILED message
#      and Fix: block are on stderr.
#   3  no usable Python 3.12+ interpreter. A WARNING and Fix: block are
#      on stderr; callers degrade, they do not fail.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REQUIREMENTS="$REPO_ROOT/scripts/tests/requirements-ci.txt"
CONFIG_PYPROJECT="$REPO_ROOT/packages/judgemind-config/pyproject.toml"
VENV="${SCRIPTS_TESTS_VENV:-$REPO_ROOT/tmp/scripts-tests-venv}"
FINGERPRINT_FILE="$VENV/.scripts-tests-fingerprint"

case "${1:-}" in
    "") ;;
    -h|--help)
        sed -n '2,39p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "ensure-scripts-tests-venv.sh: unknown argument: $1" >&2
        exit 2
        ;;
esac

case "$VENV" in
    ""|/|"$HOME"|"$REPO_ROOT")
        echo "ensure-scripts-tests-venv.sh: refusing to use '$VENV' as the venv path (it is deleted on rebuild)" >&2
        exit 2
        ;;
esac

if [ ! -f "$REQUIREMENTS" ]; then
    echo "FAILED: $REQUIREMENTS not found" >&2
    echo "Fix:" >&2
    echo "  git checkout origin/main -- scripts/tests/requirements-ci.txt" >&2
    exit 1
fi

# --- interpreter --------------------------------------------------------
py_is_312_plus() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null
}

python_bin="${SCRIPTS_TESTS_PYTHON:-}"
if [ -z "$python_bin" ]; then
    for candidate in python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && py_is_312_plus "$candidate"; then
            python_bin="$candidate"
            break
        fi
    done
fi
if [ -z "$python_bin" ] || ! py_is_312_plus "$python_bin"; then
    echo "WARNING: no Python 3.12+ interpreter for the scripts/tests venv (#5045)" >&2
    echo "Fix:" >&2
    echo "  brew install python@3.12     # or set SCRIPTS_TESTS_PYTHON=/path/to/python3.12" >&2
    exit 3
fi

# --- fingerprint --------------------------------------------------------
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        shasum -a 256 | cut -d' ' -f1
    fi
}

py_version="$("$python_bin" -c 'import sys; print(sys.version.split()[0])')"
want="$(
    {
        cat "$REQUIREMENTS"
        [ -f "$CONFIG_PYPROJECT" ] && cat "$CONFIG_PYPROJECT"
        echo "python=$py_version"
    } | sha256_of
)"

have=""
[ -f "$FINGERPRINT_FILE" ] && have="$(cat "$FINGERPRINT_FILE" 2>/dev/null)"
if [ "$have" = "$want" ] && [ -x "$VENV/bin/pytest" ]; then
    echo "ensure-scripts-tests-venv: reusing $VENV" >&2
    echo "$VENV"
    exit 0
fi

# --- build (under a lock, so two pushes in one checkout do not race) ----
mkdir -p "$(dirname "$VENV")"
lock="$VENV.lock"
got_lock=false
for _ in $(seq 1 120); do
    if mkdir "$lock" 2>/dev/null; then
        got_lock=true
        break
    fi
    sleep 1
done
if [ "$got_lock" != true ]; then
    echo "FAILED: could not take $lock after 120s" >&2
    echo "Fix:" >&2
    echo "  rmdir $lock     # if no other push is running in this checkout" >&2
    exit 1
fi
trap 'rmdir "$lock" 2>/dev/null' EXIT

# Another push may have built it while we waited for the lock.
have=""
[ -f "$FINGERPRINT_FILE" ] && have="$(cat "$FINGERPRINT_FILE" 2>/dev/null)"
if [ "$have" = "$want" ] && [ -x "$VENV/bin/pytest" ]; then
    echo "ensure-scripts-tests-venv: reusing $VENV" >&2
    echo "$VENV"
    exit 0
fi

echo "ensure-scripts-tests-venv: building $VENV from scripts/tests/requirements-ci.txt ($python_bin $py_version)..." >&2
rm -rf "$VENV"
pip_log="$(dirname "$VENV")/scripts-tests-venv-pip.log"
if ! "$python_bin" -m venv "$VENV" >"$pip_log" 2>&1 \
        || ! (cd "$REPO_ROOT" && "$VENV/bin/python" -m pip install --quiet \
                --disable-pip-version-check -r "$REQUIREMENTS") >>"$pip_log" 2>&1; then
    echo "FAILED: could not build the scripts/tests venv at $VENV" >&2
    tail -n 20 "$pip_log" | sed 's/^/  /' >&2
    echo "  Full log: $pip_log" >&2
    echo "Fix:" >&2
    echo "  scripts/ensure-scripts-tests-venv.sh     # retry (e.g. once the network is back)" >&2
    echo "  PREPUSH_SCRIPTS_TESTS=0 git push         # push without it; CI still runs scripts/tests" >&2
    rm -rf "$VENV"
    exit 1
fi

echo "$want" > "$FINGERPRINT_FILE"
echo "ensure-scripts-tests-venv: built $VENV" >&2
echo "$VENV"
exit 0
