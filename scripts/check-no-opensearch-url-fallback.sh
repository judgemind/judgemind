#!/usr/bin/env bash
# check-no-opensearch-url-fallback.sh — Forbid `process.env.OPENSEARCH_URL`
# in api test files.
#
# API tests must read the test-specific env var `TEST_OPENSEARCH_URL` (exposed
# by packages/api/tests/setup-opensearch.ts), never the operational
# `OPENSEARCH_URL`. Using `OPENSEARCH_URL` in a test context means the test
# silently hits the production (or dev) OpenSearch cluster rather than the
# isolated test instance, corrupting real data and producing non-repeatable
# results.
#
# The canonical accessor is `getTestOpenSearchUrl()` via
# packages/api/tests/setup-opensearch.ts.  If you need to reference the raw
# env var name in a test file as a string (e.g. for documentation), use the
# split-concatenation escape hatch `'OPEN' + 'SEARCH_URL'` as demonstrated
# in packages/api/tests/setup-opensearch.unit.test.ts.
#
# Usage:
#   scripts/check-no-opensearch-url-fallback.sh                 # scan default dir
#   scripts/check-no-opensearch-url-fallback.sh [dir]           # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more files reference `process.env.OPENSEARCH_URL`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/packages/api/tests}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Fixed-string match for the exact literal `process.env.OPENSEARCH_URL`.
# We use -F (fixed-string) because the dot in `.env` and the dot-separator
# are literal, not regex metacharacters.  This also avoids accidentally
# matching `process.env.TEST_OPENSEARCH_URL` or any other superset variant.
PATTERN='process.env.OPENSEARCH_URL'

# ─── Build --exclude-dir arguments from the canonical list ───────────────
# (REPO_WALK_EXCLUSIONS lives in preflight.sh — single source of truth, #4308.)
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan the codebase ───────────────────────────────────────────────────

violations=0

matches=$(grep -rnF "$PATTERN" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip TS/JS comment lines (first non-whitespace is //, /*, or *).
        if [[ "$content" =~ ^[[:space:]]*(//|\*|/\*) ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found forbidden 'process.env.OPENSEARCH_URL' usage in api tests."
            echo ""
            echo "  API tests must use TEST_OPENSEARCH_URL, not the operational OPENSEARCH_URL."
            echo "  Reading OPENSEARCH_URL in a test silently targets the dev/prod OpenSearch"
            echo "  cluster instead of the isolated test instance, corrupting real data."
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of 'process.env.OPENSEARCH_URL' in api tests."
    echo ""
    echo "  Fix: use the TEST_OPENSEARCH_URL env var instead."
    echo "  The canonical accessor is defined in packages/api/tests/setup-opensearch.ts."
    echo ""
    echo "  If you must reference the env var name as a string literal (e.g. in a"
    echo "  test assertion), use the split-concatenation escape hatch:"
    echo "    'OPEN' + 'SEARCH_URL'   →  packages/api/tests/setup-opensearch.unit.test.ts"
    echo ""
    exit 1
fi

echo "All clean — no 'process.env.OPENSEARCH_URL' usage detected in api tests."
exit 0
