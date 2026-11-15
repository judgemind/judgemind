#!/usr/bin/env bash
# permanent: true
# check-image-path-filters.sh — every file a Docker image COPYs must be in
# the on.push.paths filter of the workflow that rebuilds that image.
#
# Thin wrapper around scripts/check_image_path_filters.py (stdlib-only
# Python; see its docstring for the full rules). Wired into ci.yml as the
# image-path-filter-check job and picked up by scripts/run-ci-guards.sh in
# the pre-push hook.
#
# Why (#5060): deploy-scraper.yml's path filter was a hand-kept copy of the
# scraper Dockerfile's COPY list and missed scripts/reingest_from_s3.py and
# scripts/rebuild_db.py, so changes to them merged without rebuilding the
# image that the raw-key reconciler runs them from.
#
# Usage:
#   scripts/check-image-path-filters.sh [--repo-root PATH]
#
# Exit codes:
#   0 — every COPY'd file triggers its image's workflow.
#   1 — one or more COPY'd files are missing (stderr has a Fix: block).
#   2 — usage error or a workflow/Dockerfile shape the check can't read.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check_image_path_filters.py" "$@"
