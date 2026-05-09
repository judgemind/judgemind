#!/usr/bin/env bash
# Fixture for #4479: conditional ``LABELS+=`` inside an arg-parse
# ``case`` arm + unguarded ``for label in "${LABELS[@]}"; do``
# iteration. Mirrors the bug shape from #4051 /
# ``scripts/block-on-new-issue.sh`` (pre-#4476).
#
# Expected behavior: ``scripts/check-bash-set-u-empty-array.sh
# tests/fixtures/conditional_array_assign/`` exits 1 and the
# violation report names the iteration line (line 27 of this file).
#
# When the user invokes this script with no ``--label`` argument,
# the conditional ``LABELS+=`` inside the ``case`` arm never runs,
# the array stays empty, and the iteration trips ``unbound
# variable`` on bash 3.2.

set -euo pipefail

LABELS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --label)
            LABELS+=("${2:-}")
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

# This is the iteration the static check must flag.
for label in "${LABELS[@]}"; do
    echo "$label"
done
