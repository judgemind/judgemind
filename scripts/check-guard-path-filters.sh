#!/usr/bin/env bash
# permanent: true
# check-guard-path-filters.sh — a guard must run on the PRs that touch the
# code it guards.
#
# Thin wrapper around scripts/check_guard_path_filters.py (stdlib-only
# Python; see its docstring for the full rules). Wired into ci.yml as the
# last step of the unfiltered repo-guards job and picked up by
# scripts/run-ci-guards.sh in the pre-push hook.
#
# Why (#5088): the S3-delete and page-cap guards scan packages/*/src but ran
# only inside scripts-tests, whose `scripts` paths-filter leaves that tree
# out, so a PR touching only packages/*/src skipped both guards. This check
# fails when a guard is wired only into path-filtered jobs whose filter does
# not cover the paths the guard declares (`# scans: ...`) it scans.
#
# Usage:
#   scripts/check-guard-path-filters.sh [--repo-root PATH]
#
# Exit codes:
#   0 — every guard runs on the PRs that touch what it scans.
#   1 — a guard can be skipped by a PR it exists to catch (stderr has a
#       Fix: block).
#   2 — usage error or a workflow the check can't read.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check_guard_path_filters.py" "$@"
