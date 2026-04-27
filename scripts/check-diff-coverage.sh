#!/usr/bin/env bash
# check-diff-coverage.sh — Run pytest + diff-cover locally for a Python package.
#
# Catches diff-coverage failures before pushing, saving a CI round-trip.
# CI requires >= 90% coverage on new/changed lines (diff coverage).
# This script runs the same check locally so agents can fix gaps early.
#
# Usage:
#   scripts/check-diff-coverage.sh <package>        # e.g. scraper-framework
#   scripts/check-diff-coverage.sh <package> --skip-tests  # skip pytest, use existing coverage.xml
#
# The <package> argument is the directory name inside packages/ (e.g. scraper-framework,
# nlp-pipeline) or a path for infra packages.
#
# Exit codes:
#   0 — Diff coverage >= 90%.
#   1 — Diff coverage < 90% (uncovered lines are printed).
#   2 — Setup error (missing venv, missing package, etc.).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── Parse arguments ─────────────────────────────────────────────────────
PACKAGE=""
SKIP_TESTS=false
FAIL_UNDER=90

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-tests)
            SKIP_TESTS=true
            shift
            ;;
        --fail-under)
            FAIL_UNDER="$2"
            shift 2
            ;;
        --fail-under=*)
            FAIL_UNDER="${1#*=}"
            shift
            ;;
        -*)
            echo "Unknown option: $1" >&2
            echo "Usage: scripts/check-diff-coverage.sh <package> [--skip-tests] [--fail-under N]" >&2
            exit 2
            ;;
        *)
            PACKAGE="$1"
            shift
            ;;
    esac
done

if [[ -z "$PACKAGE" ]]; then
    echo "Usage: scripts/check-diff-coverage.sh <package> [--skip-tests] [--fail-under N]" >&2
    echo "" >&2
    echo "Python packages:" >&2
    for d in "$REPO_ROOT"/packages/*/pyproject.toml; do
        [[ -f "$d" ]] && echo "  $(basename "$(dirname "$d")")" >&2
    done
    for d in "$REPO_ROOT"/infra/*/pyproject.toml; do
        [[ -f "$d" ]] && echo "  infra/$(basename "$(dirname "$d")")" >&2
    done
    exit 2
fi

# ─── Resolve package directory ───────────────────────────────────────────
if [[ "$PACKAGE" == infra/* ]]; then
    PKG_DIR="$REPO_ROOT/$PACKAGE"
elif [[ -d "$REPO_ROOT/packages/$PACKAGE" ]]; then
    PKG_DIR="$REPO_ROOT/packages/$PACKAGE"
elif [[ -d "$REPO_ROOT/$PACKAGE" ]]; then
    PKG_DIR="$REPO_ROOT/$PACKAGE"
else
    echo "ERROR: Package '$PACKAGE' not found." >&2
    echo "Tried: packages/$PACKAGE, $PACKAGE" >&2
    exit 2
fi

# ─── Detect package type ────────────────────────────────────────────────
if [[ -f "$PKG_DIR/pyproject.toml" ]]; then
    PKG_TYPE="python"
elif [[ -f "$PKG_DIR/package.json" ]]; then
    PKG_TYPE="typescript"
else
    echo "ERROR: No pyproject.toml or package.json found in $PKG_DIR" >&2
    exit 2
fi

# ─── Python package ─────────────────────────────────────────────────────
if [[ "$PKG_TYPE" == "python" ]]; then
    VENV="$PKG_DIR/.venv"
    if [[ ! -d "$VENV" ]]; then
        echo "ERROR: No .venv found at $VENV" >&2
        echo "Create one: python3.12 -m venv $VENV && $VENV/bin/pip install -e '.[dev]'" >&2
        exit 2
    fi

    PYTEST="$VENV/bin/pytest"
    if [[ ! -x "$PYTEST" ]]; then
        echo "ERROR: pytest not found in $VENV" >&2
        echo "Install: $VENV/bin/pip install -e '.[dev]'" >&2
        exit 2
    fi

    # Ensure diff-cover is installed
    DIFF_COVER="$VENV/bin/diff-cover"
    if [[ ! -x "$DIFF_COVER" ]]; then
        echo "Installing diff-cover into $VENV..." >&2
        "$VENV/bin/pip" install diff-cover --quiet
    fi

    COV_FILE="$PKG_DIR/coverage.xml"

    # Run tests to generate coverage (unless --skip-tests)
    if [[ "$SKIP_TESTS" == "false" ]]; then
        echo "Running pytest with coverage for $PACKAGE..." >&2
        if ! (cd "$PKG_DIR" && "$PYTEST" tests/ -x -q --tb=line) 2>&1; then
            echo "" >&2
            echo "FAILED: Tests failed for $PACKAGE. Fix tests before checking diff coverage." >&2
            exit 1
        fi
    fi

    if [[ ! -f "$COV_FILE" ]]; then
        echo "ERROR: No coverage.xml found at $COV_FILE" >&2
        echo "Ensure pytest is configured with --cov-report=xml in pyproject.toml." >&2
        echo "Or run: $PYTEST $PKG_DIR/tests/ -v --tb=short" >&2
        exit 2
    fi

    # Ensure origin/main is available for comparison
    git -C "$REPO_ROOT" fetch origin main --quiet 2>/dev/null || true

    echo "" >&2
    echo "Running diff-cover for $PACKAGE (fail-under=$FAIL_UNDER%)..." >&2
    echo "────────────────────────────────────────────────────────────" >&2

    # Run diff-cover with the same flags as CI
    cd "$REPO_ROOT"
    if "$DIFF_COVER" "$COV_FILE" \
            --compare-branch=origin/main \
            --fail-under="$FAIL_UNDER" \
            --diff-range-notation='..'; then
        echo "" >&2
        echo "PASSED: Diff coverage >= ${FAIL_UNDER}% for $PACKAGE" >&2
        exit 0
    else
        echo "" >&2
        echo "FAILED: Diff coverage < ${FAIL_UNDER}% for $PACKAGE" >&2
        echo "" >&2
        echo "The output above shows which new/changed lines lack test coverage." >&2
        echo "Add tests for those lines, then re-run:" >&2
        echo "  scripts/check-diff-coverage.sh $PACKAGE" >&2
        exit 1
    fi
fi

# ─── TypeScript package ────────────────────────────────────────────────
if [[ "$PKG_TYPE" == "typescript" ]]; then
    # Check for node_modules
    if [[ ! -d "$PKG_DIR/node_modules" ]]; then
        echo "ERROR: No node_modules found in $PKG_DIR" >&2
        echo "Run: cd $PKG_DIR && npm install" >&2
        exit 2
    fi

    # Ensure diff-cover is installed (globally via pip, as CI does)
    if ! command -v diff-cover &>/dev/null; then
        if [[ -x "$REPO_ROOT/packages/scraper-framework/.venv/bin/diff-cover" ]]; then
            DIFF_COVER="$REPO_ROOT/packages/scraper-framework/.venv/bin/diff-cover"
        else
            echo "ERROR: diff-cover not found. Install it:" >&2
            echo "  pip install diff-cover" >&2
            echo "  OR: any-python-package/.venv/bin/pip install diff-cover" >&2
            exit 2
        fi
    else
        DIFF_COVER="diff-cover"
    fi

    COV_FILE="$PKG_DIR/coverage/lcov.info"

    # Run tests to generate coverage (unless --skip-tests)
    if [[ "$SKIP_TESTS" == "false" ]]; then
        echo "Running npm test with coverage for $PACKAGE..." >&2
        if ! (cd "$PKG_DIR" && npm test 2>&1); then
            echo "" >&2
            echo "FAILED: Tests failed for $PACKAGE. Fix tests before checking diff coverage." >&2
            exit 1
        fi
    fi

    if [[ ! -f "$COV_FILE" ]]; then
        echo "ERROR: No lcov.info found at $COV_FILE" >&2
        echo "Ensure tests generate coverage output to coverage/lcov.info." >&2
        exit 2
    fi

    # Ensure origin/main is available for comparison
    git -C "$REPO_ROOT" fetch origin main --quiet 2>/dev/null || true

    echo "" >&2
    echo "Running diff-cover for $PACKAGE (fail-under=$FAIL_UNDER%)..." >&2
    echo "────────────────────────────────────────────────────────────" >&2

    cd "$REPO_ROOT"
    if "$DIFF_COVER" "$COV_FILE" \
            --compare-branch=origin/main \
            --fail-under="$FAIL_UNDER" \
            --diff-range-notation='..'; then
        echo "" >&2
        echo "PASSED: Diff coverage >= ${FAIL_UNDER}% for $PACKAGE" >&2
        exit 0
    else
        echo "" >&2
        echo "FAILED: Diff coverage < ${FAIL_UNDER}% for $PACKAGE" >&2
        echo "" >&2
        echo "The output above shows which new/changed lines lack test coverage." >&2
        echo "Add tests for those lines, then re-run:" >&2
        echo "  scripts/check-diff-coverage.sh $PACKAGE" >&2
        exit 1
    fi
fi
