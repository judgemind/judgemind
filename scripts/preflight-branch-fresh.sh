#!/usr/bin/env bash
# preflight-branch-fresh.sh — Standalone wrapper around preflight_branch_fresh.
#
# Verifies the current branch is not behind origin/main. Used by the /task
# skill's A.1a step (see .claude/skills/task/SKILL.md) to refresh the worktree
# base before implementing, preventing stale-merge-base surprises at push time
# (see #3075).
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_branch_fresh
# --fetch`, which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts — same pattern as check-duplicate-pr.sh
# (see #2706).
#
# Usage:
#   scripts/preflight-branch-fresh.sh           # check only (no fetch)
#   scripts/preflight-branch-fresh.sh --fetch   # fetch origin main first, then check
#
# Exit codes (pass-through from preflight_branch_fresh):
#   0 — Branch is at or ahead of origin/main. Safe to proceed.
#   1 — Branch is behind origin/main. A PREFLIGHT FAIL line is printed to
#       stderr with the commit-count gap. Caller should rebase.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_branch_fresh "$@" || exit_code=$?

exit "$exit_code"
