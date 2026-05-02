#!/usr/bin/env bash
# install-dispatcher-v3-venv.sh — Create and populate a Python venv for the
# dispatcher v3 task-runner scripts at scripts/dispatcher_v3/.
#
# Mirrors install-dispatcher-venv.sh: scripts/dispatcher_v3/ has no
# pyproject.toml, so scripts/install-package-venv.sh (which requires a
# packages/<name>/ directory) cannot bootstrap its venv. This dedicated
# helper creates scripts/dispatcher_v3/.venv and installs the dependencies
# needed to run the v3 unit tests.
#
# Usage:
#   scripts/install-dispatcher-v3-venv.sh
#
# No arguments are accepted — the venv path is fixed at
# scripts/dispatcher_v3/.venv relative to the repo root. Idempotent: if the
# venv already exists it is reused; pip install is cheap when dependencies
# are already satisfied.
#
# Dependency installation order:
#   1. pytest, pytest-xdist, ruff, boto3  (PyPI)
#
# v3 has no judgemind-config dependency at this stage — boto3 is the only
# AWS-side import needed by the task-runner. If a future v3 module imports
# judgemind-config, add the editable install here and update this comment.
#
# Exit codes:
#   0 — venv is populated and usable
#   1 — usage error or install failure

set -euo pipefail

# ── Resolve repo root ────────────────────────────────────────────────────
# Walk up from this script's location looking for a directory that has both
# CLAUDE.md and .git. In worktrees .git is a file; the main repo has .git
# as a directory. We accept either.
find_repo_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    while [[ "$dir" != "/" ]]; do
        if [[ -f "$dir/CLAUDE.md" && -e "$dir/.git" ]]; then
            echo "$dir"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    echo "ERROR: cannot find repo root (looked for CLAUDE.md + .git)" >&2
    return 1
}
REPO_ROOT="$(find_repo_root)"

# ── Validate arguments ───────────────────────────────────────────────────

if [[ $# -ne 0 ]]; then
    echo "Usage: scripts/install-dispatcher-v3-venv.sh" >&2
    echo "" >&2
    echo "This script takes no arguments — the venv path is fixed at" >&2
    echo "  scripts/dispatcher_v3/.venv" >&2
    exit 1
fi

VENV_DIR="$REPO_ROOT/scripts/dispatcher_v3/.venv"
PY=python3.12

# ── Create the venv if missing ───────────────────────────────────────────

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating venv at $VENV_DIR"
    "$PY" -m venv "$VENV_DIR"
fi

PIP="$VENV_DIR/bin/pip"

if [[ ! -x "$PIP" ]]; then
    echo "ERROR: pip not found in venv at $VENV_DIR (venv may be corrupt)" >&2
    echo "Delete it and re-run: rm -rf $VENV_DIR" >&2
    exit 1
fi

# ── Install PyPI dependencies ─────────────────────────────────────────────
# pytest + pytest-xdist + ruff for the test loop. boto3 for the v3
# task-runner's AWS interactions (DescribeTasks, RunTask, etc.) — kept
# in the venv even though runners.py itself doesn't import it, so future
# v3 modules can be added without touching this install list.

echo "Installing PyPI dependencies: pytest pytest-xdist ruff boto3"
"$PIP" install --quiet pytest pytest-xdist ruff boto3

echo "Done. Venv ready at $VENV_DIR"
