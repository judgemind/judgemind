#!/usr/bin/env bash
# check-no-rebuild-db-skip-reset.sh — Forbid `rebuild_db.py ... --skip-reset`
# references in docs and CLAUDE.md.
#
# The `--skip-reset` flag does NOT exist on the Python script
# `scripts/rebuild_db.py` — it only exists on the shell wrapper
# `scripts/rebuild_db.sh`.  The Python script's argparse uses the opposite
# convention: `--reset` is opt-in (default is to keep existing data).
#
# Docs that instruct agents to run `scripts/ecs-run-task.sh scripts/rebuild_db.py
# -- --county X --skip-reset` cause failed ECS runs with
# `argparse: unrecognized arguments: --skip-reset` — wasting time and ECS
# capacity.  See issue #2591.
#
# The shell wrapper `scripts/rebuild_db.sh --skip-reset` is valid and is NOT
# flagged by this check — the pattern requires the `.py` extension directly
# attached to `rebuild_db`.
#
# Usage:
#   scripts/check-no-rebuild-db-skip-reset.sh          # scan repo root
#   scripts/check-no-rebuild-db-skip-reset.sh [dir]    # scan a specific directory
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more tracked files reference `rebuild_db.py ... --skip-reset`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Pattern ─────────────────────────────────────────────────────────────
# Match `rebuild_db.py` followed (somewhere on the same line) by
# `--skip-reset`.  We intentionally require the `.py` extension so the
# valid `rebuild_db.sh --skip-reset` shell wrapper is not flagged.
#
# `grep -E` works line-by-line, so `.*` is already bounded to a single
# line — no need for an explicit negated class.
PATTERN='rebuild_db\.py.*--skip-reset'

# ─── Directories to exclude from the scan ────────────────────────────────
# Repo-walk dir exclusions come from REPO_WALK_EXCLUSIONS in preflight.sh
# (canonical list — single source of truth, see #4308). No per-check
# extras needed here.

# ─── Files that legitimately mention the pattern as context ──────────────
# The check script and its test must spell the pattern literally.  Past
# incident post-mortems under docs/investigations/ may also reference it
# as historical context.
EXCLUDE_FILES=(
    "scripts/check-no-rebuild-db-skip-reset.sh"
    "scripts/tests/test_check_no_rebuild_db_skip_reset.sh"
)

# ─── Build --exclude-dir arguments from the canonical list ──────────────

exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan the codebase ──────────────────────────────────────────────────

violations=0

matches=$(grep -rnE "$PATTERN" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip matches in the explicitly-excluded files.
        skip=false
        for excl in "${EXCLUDE_FILES[@]}"; do
            if [[ "$file_path" == *"$excl" ]]; then
                skip=true
                break
            fi
        done
        if "$skip"; then
            continue
        fi

        # Skip docs/investigations/* — post-mortems may reference the
        # pattern as historical context.
        if [[ "$file_path" == *"docs/investigations/"* ]]; then
            continue
        fi

        # Skip comment lines (first non-whitespace character is `#`).
        # Covers shell, YAML, Python, and Dockerfile comments.
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: Found forbidden 'rebuild_db.py ... --skip-reset' usage."
            echo ""
            echo "  The Python script scripts/rebuild_db.py does NOT implement"
            echo "  a --skip-reset flag — only the shell wrapper"
            echo "  scripts/rebuild_db.sh does.  Agents running these commands"
            echo "  via ecs-run-task.sh get argparse errors (see #2591)."
            echo ""
        fi

        echo "    $line"
        violations=$((violations + 1))
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations occurrence(s) of 'rebuild_db.py ... --skip-reset'."
    echo ""
    echo "  Fix: remove the --skip-reset flag.  The Python script's default"
    echo "  already preserves existing data — no flag is needed to \"skip reset\"."
    echo "  To truncate derived tables before rebuilding, use --reset (opt-in)."
    echo ""
    echo "  Example rewrites:"
    echo "    scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county X --skip-reset"
    echo "    →  scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county X"
    echo ""
    echo "  The shell wrapper scripts/rebuild_db.sh --skip-reset is still valid"
    echo "  and is NOT flagged by this check."
    echo ""
    echo "  If the pattern must appear as explanatory context, put it in a"
    echo "  comment (lines starting with '#') or under docs/investigations/."
    exit 1
fi

echo "All clean — no 'rebuild_db.py ... --skip-reset' usage detected."
exit 0
