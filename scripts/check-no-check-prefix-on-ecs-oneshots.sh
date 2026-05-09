#!/usr/bin/env bash
# check-no-check-prefix-on-ecs-oneshots.sh — Thin wrapper around the
# Python implementation. See check-no-check-prefix-on-ecs-oneshots.py
# for the full behaviour description, exit codes, and Fix-block format.
#
# permanent: true
#
# Usage:
#   scripts/check-no-check-prefix-on-ecs-oneshots.sh
#   scripts/check-no-check-prefix-on-ecs-oneshots.sh --list
#   scripts/check-no-check-prefix-on-ecs-oneshots.sh --scripts-dir DIR
#
# Exit codes:
#   0 — every check-*.{sh,py} is either NOT shaped like an ECS oneshot,
#       OR is in the grandfathered allowlist; OR --list was passed.
#   1 — one or more guards match all three ECS-oneshot signals.
#   2 — script error (scripts/ dir missing, etc.).
#
# Tracking: issue #4563 (parent: #4558).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-no-check-prefix-on-ecs-oneshots.py" "$@"
