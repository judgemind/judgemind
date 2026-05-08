#!/usr/bin/env bash
# check-no-api-github-fetch.sh — Forbid GitHub API calls from the API container.
#
# The API container (packages/api/src/) must not call GitHub's REST API
# directly.  GitHub data enrichment belongs to the dispatcher daemon, which
# persists results to dispatcher.* tables so the API can read them without
# burning the shared PAT budget or having access to GITHUB_TOKEN.
#
# GITHUB_TOKEN has been removed from the API task-def (see
# infra/terraform/modules/api-service/main.tf) — its absence is load-bearing.
# If a future feature needs GitHub data, the daemon is the only place that
# should be fetching it.  See issue #2820 and
# docs/agent/unattended-patterns.md §No API-side GitHub fetches.
#
# Patterns scanned (each flagged independently; all matches collected before
# failing):
#   - Literal `api.github.com`      (fixed-string — direct GitHub REST host)
#   - Literal `github.com/repos/`   (fixed-string — alternate URL shape)
#   - Regex  `fetch\s*\(.*github`   (fetch call referencing github)
#   - Regex  `https[^'"\s]*github`  (https URL with github in the host)
#
# Comment lines (first non-whitespace is //, /*, or *) are skipped so
# JSDoc references to the removed github.ts module in parse-labels.ts and
# resolvers.ts do not trip the guard.
#
# Usage:
#   scripts/check-no-api-github-fetch.sh                # scan default dir
#   scripts/check-no-api-github-fetch.sh [dir]          # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more files contain a forbidden GitHub API reference.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/packages/api/src}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Build --exclude-dir arguments from the canonical list ───────────────
# (REPO_WALK_EXCLUSIONS lives in preflight.sh — single source of truth, #4308.)
exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Collect all matches across all patterns ─────────────────────────────
violations=0

run_check() {
    local label="$1"
    local flag="$2"     # -F or -E
    local pattern="$3"

    local raw_matches
    raw_matches=$(grep -rn "$flag" "$pattern" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

    if [[ -z "$raw_matches" ]]; then
        return
    fi

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
            echo "ERROR: Found forbidden GitHub API reference(s) in the API container source."
            echo ""
            echo "  packages/api/src/ must not call GitHub's REST API directly."
            echo "  GitHub data enrichment belongs to the dispatcher daemon, which"
            echo "  persists results to dispatcher.* tables for the API to read."
            echo "  GITHUB_TOKEN has been removed from the API task-def — its"
            echo "  absence is load-bearing.  See issue #2820."
            echo ""
        fi

        echo "    [$label] $line"
        violations=$((violations + 1))
    done <<< "$raw_matches"
}

run_check "api.github.com"    -F "api.github.com"
run_check "github.com/repos/" -F "github.com/repos/"
run_check "fetch(..github..)" -E "fetch[[:space:]]*\(.*github"
run_check "https://...github" -E "https[^'\"[:space:]]*github"

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of forbidden GitHub API reference(s)."
    echo ""
    echo "  Fix: move the GitHub fetch to the dispatcher daemon"
    echo "  (packages/dispatcher/src/) and expose the result via dispatcher.*"
    echo "  tables that the API reads.  If this is a comment referencing old code,"
    echo "  the comment-line filter covers //, /*, and * prefixes."
    echo ""
    echo "  See docs/agent/unattended-patterns.md §No API-side GitHub fetches."
    exit 1
fi

echo "All clean — no GitHub API references detected in packages/api/src/."
exit 0
