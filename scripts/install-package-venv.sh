#!/usr/bin/env bash
# install-package-venv.sh — Create and populate a Python venv for one of
# the Judgemind packages, installing any local sibling dependencies first.
#
# Problem this solves (#2491): `packages/scraper-framework` and
# `packages/nlp-pipeline` both declare `judgemind-config` as a runtime
# dependency, but `judgemind-config` is a local, unpublished package.
# Running `pip install -e ".[dev]"` on a fresh venv therefore fails with:
#
#   ERROR: Could not find a version that satisfies the requirement
#          judgemind-config (from versions: none)
#
# This helper installs `judgemind-config` from the monorepo first (when
# the target package depends on it), then the target package — so a
# fresh worktree can bring up a package venv in one command.
#
# Usage:
#   scripts/install-package-venv.sh <package-name>
#
# Examples:
#   scripts/install-package-venv.sh scraper-framework
#   scripts/install-package-venv.sh nlp-pipeline
#   scripts/install-package-venv.sh judgemind-config
#
# The package name is the directory under `packages/` (e.g. "scraper-framework"
# for `packages/scraper-framework`). The script is idempotent: if the venv
# already exists it is reused; pip install -e is cheap when already
# satisfied.
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

if [[ $# -ne 1 ]]; then
    echo "Usage: scripts/install-package-venv.sh <package-name>" >&2
    echo "" >&2
    echo "Examples:" >&2
    echo "  scripts/install-package-venv.sh scraper-framework" >&2
    echo "  scripts/install-package-venv.sh nlp-pipeline" >&2
    exit 1
fi

PKG="$1"
PKG_DIR="$REPO_ROOT/packages/$PKG"

if [[ ! -d "$PKG_DIR" ]]; then
    echo "ERROR: package directory not found: $PKG_DIR" >&2
    echo "" >&2
    echo "Available packages:" >&2
    # List only directories that contain a pyproject.toml
    for d in "$REPO_ROOT"/packages/*/; do
        if [[ -f "$d/pyproject.toml" ]]; then
            echo "  $(basename "$d")" >&2
        fi
    done
    exit 1
fi

if [[ ! -f "$PKG_DIR/pyproject.toml" ]]; then
    echo "ERROR: $PKG_DIR has no pyproject.toml — is this a Python package?" >&2
    exit 1
fi

VENV_DIR="$PKG_DIR/.venv"
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

# ── Install local sibling dependencies first ─────────────────────────────
# judgemind-config is a local unpublished package. Any target package that
# depends on it must have it installed first, or pip will try PyPI and
# fail. This is cheap when already installed.

if [[ "$PKG" != "judgemind-config" ]]; then
    # Check whether the target package depends on judgemind-config. We
    # grep the pyproject.toml rather than parsing it — the dependency is
    # declared as a simple string list entry and this avoids needing
    # tomllib in the system python.
    if grep -q '^\s*"judgemind-config"' "$PKG_DIR/pyproject.toml"; then
        echo "Installing local sibling dependency: judgemind-config"
        "$PIP" install -e "$REPO_ROOT/packages/judgemind-config" --quiet
    fi
fi

# ── Install the target package with dev extras ───────────────────────────

echo "Installing $PKG with [dev] extras"
"$PIP" install -e "$PKG_DIR[dev]" --quiet

echo "Done. Venv ready at $VENV_DIR"
