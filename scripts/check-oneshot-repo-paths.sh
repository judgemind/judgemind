#!/usr/bin/env bash
# check-oneshot-repo-paths.sh — Detect ECS oneshot scripts that reference repo
# paths without a fallback mechanism.
#
# Scripts run via ecs-run-task.sh are uploaded as single files to S3.  The repo
# filesystem is NOT available inside the container.  If a script resolves
# the module-level REPO_ROOT / _REPO_ROOT name to locate config files (e.g.
# data-quality-baselines.json), those files will be missing, and the script
# may silently degrade.
#
# Implementation history
# ----------------------
# Pre-#4483 this guard greptext-scanned scripts/*.py for the literal pattern
# `_?REPO_ROOT`. That false-positived on:
#   * mentions inside module / function / class docstrings
#     (e.g. ``... AST shape ``_REPO_ROOT / "scripts" / "<name>.py"`` ...``);
#   * mentions inside string-literal Fix-block / error-message output
#     (e.g. ``lines.append("sys.path.insert(0, str(REPO_ROOT / 'scripts'))")``).
#
# Issue #4483 replaced that grep with an AST walk implemented in
# ``scripts/check_oneshot_repo_paths.py``. The Python helper only flags real
# ``ast.Name(id=^_?REPO_ROOT$)`` Load / Store references — string constants,
# docstrings, and inline comments are skipped because the AST distinguishes
# Name from Constant. This shell script remains the canonical CI entry point
# (referenced by .github/workflows/ci.yml) and forwards all arguments to the
# Python helper unchanged.
#
# Usage:
#   scripts/check-oneshot-repo-paths.sh          # exits 0 if clean, 1 if violations
#   scripts/check-oneshot-repo-paths.sh --dir P  # scan directory P instead of scripts/
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more scripts reference REPO_ROOT without a validated fallback.
#   2 — Usage error (e.g. --dir target does not exist).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/check_oneshot_repo_paths.py"

if [[ ! -x "$HELPER" ]]; then
    echo "ERROR: helper $HELPER is missing or not executable" >&2
    exit 2
fi

exec python3 "$HELPER" "$@"
