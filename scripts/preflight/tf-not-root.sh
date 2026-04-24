#!/usr/bin/env bash
# scripts/preflight/tf-not-root.sh — Standalone wrapper around preflight_tf_not_root.
#
# Verifies that a terraform apply/destroy is not being run from the root
# infra/terraform/ directory. The root config has its own state backend and
# running apply from it creates duplicate resources that collide with the real
# ones in environments/<env>/.
#
# This wrapper exists because calling the underlying function in a single Bash
# tool invocation requires `source scripts/preflight.sh && preflight_tf_not_root`,
# which the preflight hook blocks (quoted strings combined with `&&`).
# A standalone script under scripts/preflight/ is covered by the `Bash(scripts/*)`
# permission and runs without prompts.
#
# Usage:
#   scripts/preflight/tf-not-root.sh                                  # checks pwd
#   scripts/preflight/tf-not-root.sh infra/terraform/environments/dev # checks path
#
# Exit codes (pass-through from preflight_tf_not_root):
#   0 — Path is an environment-specific directory. Safe to proceed.
#   1 — Path is the root infra/terraform/ directory. PREFLIGHT FAIL is printed
#       to stderr with guidance on which environment path to use instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../preflight.sh
source "$SCRIPT_DIR/preflight.sh"

exit_code=0
preflight_tf_not_root "$@" || exit_code=$?

exit "$exit_code"
