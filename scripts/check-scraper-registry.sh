#!/usr/bin/env bash
# check-scraper-registry.sh — Verify all scraper modules are registered in runner.py.
#
# Every Python module under courts/ that exports a default_config function
# must be imported inside _build_registry() in framework/runner.py.
# Unregistered scrapers silently produce zero data because the runner never
# instantiates them.
#
# Usage:
#   scripts/check-scraper-registry.sh          # exits 0 if clean, 1 if violations
#   scripts/check-scraper-registry.sh --verbose # list all modules and their status
#
# See https://github.com/judgemind/judgemind/issues/2132 for context.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check-scraper-registry.py" "$@"
