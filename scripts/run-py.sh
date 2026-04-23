#!/usr/bin/env bash
# run-py.sh — Run a Python script with the correct venv automatically.
#
# Reads a `# venv: <package-name>` comment from the first 10 lines of the
# target script and activates the corresponding venv at
# packages/<package-name>/.venv/bin/python3.  If no header is found, falls
# back to the system `python3` with a warning.
#
# Usage:
#   scripts/run-py.sh <script.py> [args...]
#
# Examples:
#   scripts/run-py.sh scripts/audit_field_completeness.py --json
#   scripts/run-py.sh scripts/reingest_from_s3.py --all
#
# Exit codes:
#   The exit code of the Python script, or 1 on setup errors.

set -euo pipefail

# ── Resolve repo root ────────────────────────────────────────────────────
# Walk up from this script's location looking for a directory that has both
# CLAUDE.md and .git as a directory (not a file).  In the main repo .git is
# a directory; in worktrees .git is a file containing "gitdir: …".  This
# prevents stopping at a worktree root when the script is invoked from
# inside one.
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -d "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root" >&2
    return 1
}
REPO_ROOT="$(find_repo_root)"

# ── Validate arguments ───────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
    echo "Usage: scripts/run-py.sh <script.py> [args...]" >&2
    exit 1
fi

SCRIPT_PATH="$1"
shift

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "ERROR: script not found: $SCRIPT_PATH" >&2
    exit 1
fi

# ── Read the # venv: header ──────────────────────────────────────────────
# Scan the first 10 lines for a comment like: # venv: scraper-framework

VENV_PACKAGE=""
line_num=0
while IFS= read -r line; do
    line_num=$((line_num + 1))
    if [[ $line_num -gt 10 ]]; then
        break
    fi
    # Match "# venv: <package-name>" with optional leading whitespace
    if [[ "$line" =~ ^[[:space:]]*#[[:space:]]*venv:[[:space:]]*([a-zA-Z0-9_-]+) ]]; then
        VENV_PACKAGE="${BASH_REMATCH[1]}"
        break
    fi
done < "$SCRIPT_PATH"

# ── Resolve the Python interpreter ───────────────────────────────────────

if [[ -z "$VENV_PACKAGE" ]]; then
    echo "WARNING: no '# venv: <package>' header found in first 10 lines of $SCRIPT_PATH" >&2
    echo "  Running with system python3. Add '# venv: <package-name>' to the script" >&2
    echo "  to use the correct venv automatically." >&2
    exec python3 "$SCRIPT_PATH" "$@"
fi

VENV_DIR="$REPO_ROOT/packages/$VENV_PACKAGE/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"
PKG_DIR="$REPO_ROOT/packages/$VENV_PACKAGE"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "ERROR: venv not found at $VENV_DIR" >&2
    echo "" >&2
    echo "This script requires the '$VENV_PACKAGE' package venv." >&2
    echo "Create it with:" >&2
    echo "" >&2
    echo "    python3.12 -m venv $PKG_DIR/.venv" >&2
    echo "    $PKG_DIR/.venv/bin/pip install -e \"$PKG_DIR[dev]\" --quiet" >&2
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "ERROR: Python interpreter not found at $VENV_PYTHON" >&2
    echo "" >&2
    echo "The venv at $VENV_DIR appears to be corrupt. Recreate it:" >&2
    echo "" >&2
    echo "    rm -rf $VENV_DIR" >&2
    echo "    python3.12 -m venv $PKG_DIR/.venv" >&2
    echo "    $PKG_DIR/.venv/bin/pip install -e \"$PKG_DIR[dev]\" --quiet" >&2
    exit 1
fi

# ── Check that dependencies are installed ─────────────────────────────────
# Look for .dist-info directories beyond pip/setuptools/wheel in site-packages.

has_deps=false
for sp_dir in "$VENV_DIR"/lib/python*/site-packages; do
    if [[ ! -d "$sp_dir" ]]; then
        continue
    fi
    for dist_info in "$sp_dir"/*.dist-info; do
        if [[ ! -d "$dist_info" ]]; then
            continue
        fi
        dist_name="$(basename "$dist_info")"
        case "$dist_name" in
            pip-*|setuptools-*|wheel-*|_distutils_hack*)
                continue
                ;;
            *)
                has_deps=true
                break 2
                ;;
        esac
    done
done

if [[ "$has_deps" != "true" ]]; then
    echo "ERROR: venv at $VENV_DIR exists but has no dependencies installed." >&2
    echo "" >&2
    echo "This script requires the '$VENV_PACKAGE' package and its deps." >&2
    echo "Install them with:" >&2
    echo "" >&2
    echo "    $PKG_DIR/.venv/bin/pip install -e \"$PKG_DIR[dev]\" --quiet" >&2
    exit 1
fi

# ── Run the script ────────────────────────────────────────────────────────
exec "$VENV_PYTHON" "$SCRIPT_PATH" "$@"
