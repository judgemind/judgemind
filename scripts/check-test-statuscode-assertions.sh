#!/usr/bin/env bash
# check-test-statuscode-assertions.sh — Forbid HTTP-status-naming test titles
# that lack a corresponding `statusCode` assertion in the test body.
#
# See scripts/check-test-statuscode-assertions.py for the full behaviour
# description, the recognised title patterns, and the
# `// status-assertion-noqa` escape hatch.
#
# Usage:
#   scripts/check-test-statuscode-assertions.sh             # scans default API test dirs
#   scripts/check-test-statuscode-assertions.sh PATH ...    # scans specific paths
#   scripts/check-test-statuscode-assertions.sh --selftest  # runs embedded fixture self-test
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more violations found.
#
# Ref: https://github.com/judgemind/judgemind/issues/4220
# permanent: true

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "$SCRIPT_DIR/check-test-statuscode-assertions.py" "$@"
