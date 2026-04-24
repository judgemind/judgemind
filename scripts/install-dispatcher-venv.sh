#!/usr/bin/env bash
# install-dispatcher-venv.sh — Create and populate a Python venv for the
# dispatcher daemon scripts at scripts/dispatcher/.
#
# Problem this solves (#2880): scripts/dispatcher/ has no pyproject.toml, so
# scripts/install-package-venv.sh (which requires a packages/<name>/ directory
# with a pyproject.toml) cannot bootstrap the dispatcher's venv. This dedicated
# helper fills that gap: it creates scripts/dispatcher/.venv and installs the
# four runtime dependencies the dispatcher tests need.
#
# Usage:
#   scripts/install-dispatcher-venv.sh
#
# No arguments are accepted — the venv path is fixed at
# scripts/dispatcher/.venv relative to the repo root. The script is idempotent:
# if the venv already exists it is reused; pip install is cheap when
# dependencies are already satisfied.
#
# Dependency installation order:
#   1. pytest, ruff, boto3  (PyPI)
#   2. judgemind-config      (local editable, last so a PyPI flake does not
#                             leave a half-populated venv that claims readiness)
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
    echo "Usage: scripts/install-dispatcher-venv.sh" >&2
    echo "" >&2
    echo "This script takes no arguments — the venv path is fixed at" >&2
    echo "  scripts/dispatcher/.venv" >&2
    exit 1
fi

VENV_DIR="$REPO_ROOT/scripts/dispatcher/.venv"
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
# pytest and ruff are needed for running the dispatcher test suite locally.
# boto3 is needed by the dispatcher daemon's AWS interactions.
# Install these first so a judgemind-config install failure does not leave
# a venv that looks partially ready.

echo "Installing PyPI dependencies: pytest ruff boto3"
"$PIP" install --quiet pytest ruff boto3

# ── Install local sibling dependency ─────────────────────────────────────
# judgemind-config is an unpublished local package. Install it last (editable)
# so a transient PyPI flake does not prevent the PyPI deps from landing.

echo "Installing local sibling dependency: judgemind-config"
"$PIP" install --quiet -e "$REPO_ROOT/packages/judgemind-config"

echo "Done. Venv ready at $VENV_DIR"
