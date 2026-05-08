#!/usr/bin/env bash
# check-repo-walk-exclusions-canonical.sh — Forbid hand-rolled
# `--exclude-dir=` lists in `scripts/check-*.sh` and require consumers
# to source the canonical REPO_WALK_EXCLUSIONS list from preflight.sh
# instead.
#
# permanent: true
#
# Why this guard exists (#4308 root cause):
#
#   Every repo-walking hygiene check (`scripts/check-*.sh` that runs
#   `grep -rEn` over the repo) needs the same list of `--exclude-dir`
#   flags: `.git`, `.venv`, `node_modules`, `__pycache__`, `.next`,
#   `.claude`, `.vite`, `tmp`, `dist`, `build`. Before #4308 each new
#   check re-derived the list from scratch and the lists drifted —
#   missing `.claude` in #4296 caused #4300 (CI red on main for ~30
#   minutes). The exclusion contract is repo-wide policy and must not
#   live as duplicated literals across 10+ scripts.
#
#   The canonical list now lives in `scripts/preflight.sh` as the
#   `REPO_WALK_EXCLUSIONS` array. Consumers must source preflight.sh
#   and iterate that array. This guard rejects any new check script
#   that hand-rolls a `--exclude-dir=` list instead.
#
# What it checks
# ──────────────
# 1. Greps `scripts/check-*.sh` for lines containing `--exclude-dir=`.
# 2. For each script that has at least one such line, asserts that
#    the script also sources `scripts/preflight.sh` AND iterates over
#    `REPO_WALK_EXCLUSIONS` to build its `--exclude-dir` arguments.
# 3. The check itself, the canonical list source (preflight.sh), and
#    the test fixtures are exempt by path.
#
# Allowed shapes
# ──────────────
# OK (canonical-only):
#     source "$SCRIPT_DIR/preflight.sh"
#     for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
#         exclude_args+=("--exclude-dir=$dir")
#     done
#
# OK (canonical + per-check extras):
#     source "$SCRIPT_DIR/preflight.sh"
#     EXTRA_EXCLUDE_DIRS=(tests test)
#     for dir in "${REPO_WALK_EXCLUSIONS[@]}" \
#                ${EXTRA_EXCLUDE_DIRS[@]+"${EXTRA_EXCLUDE_DIRS[@]}"}; do
#         exclude_args+=("--exclude-dir=$dir")
#     done
#
# REJECTED (hand-rolled literals):
#     EXCLUDE_DIRS=( ".git" ".venv" "node_modules" )
#     for dir in "${EXCLUDE_DIRS[@]}"; do
#         exclude_args+=("--exclude-dir=$dir")
#     done
#
# REJECTED (inline flags):
#     grep -r "$P" "$D" \
#         --exclude-dir='.git' \
#         --exclude-dir='.venv'
#
# Usage
# ─────
#   scripts/check-repo-walk-exclusions-canonical.sh           # exits 0 if clean, 1 if violations
#   scripts/check-repo-walk-exclusions-canonical.sh [dir]     # scan a specific scripts dir
#
# Exit codes
# ──────────
#   0 — All check-*.sh scripts that use `--exclude-dir=` consume
#       REPO_WALK_EXCLUSIONS from preflight.sh.
#   1 — One or more scripts hand-roll a `--exclude-dir=` list.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$SCRIPT_DIR}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Files exempt from the canonical-consumer requirement ───────────
# Each entry is a repo-relative path. The canonical list source
# (preflight.sh), this guard itself, and its test fixtures must spell
# `--exclude-dir=` literally and so cannot also satisfy the consumer
# rule.
EXEMPT_FILES=(
    "scripts/preflight.sh"
    "scripts/check-repo-walk-exclusions-canonical.sh"
    "scripts/tests/test_check_repo_walk_exclusions_canonical.sh"
)

is_exempt() {
    local path="$1"
    local exempt
    for exempt in "${EXEMPT_FILES[@]}"; do
        if [[ "$path" == *"$exempt" ]]; then
            return 0
        fi
    done
    return 1
}

# ─── Find every check-*.sh script that mentions --exclude-dir= ──────
violations=0
violation_details=()

# Iterate over check-*.sh scripts in the scan directory. Use a shell
# glob with nullglob to handle the no-match case cleanly. Bash 3.2 +
# set -u: an empty array's `${arr[@]}` expansion is unbound, so guard
# with the standard `${arr[@]+"${arr[@]}"}` idiom (see #3082).
shopt -s nullglob
candidates=("$SCAN_DIR"/check-*.sh)
shopt -u nullglob

for script in ${candidates[@]+"${candidates[@]}"}; do
    if is_exempt "$script"; then
        continue
    fi

    # Skip scripts that don't reference --exclude-dir= at all.
    if ! grep -qF -- '--exclude-dir=' "$script" 2>/dev/null; then
        continue
    fi

    # The script uses --exclude-dir=. Verify it consumes the canonical
    # list:
    #   1. Sources preflight.sh
    #   2. References REPO_WALK_EXCLUSIONS (the array name)
    sources_preflight=false
    references_canonical=false

    if grep -qE '(^|[^a-zA-Z_])(\.|source)[[:space:]]+[^[:space:]]*preflight\.sh' "$script" 2>/dev/null; then
        sources_preflight=true
    fi

    if grep -qF 'REPO_WALK_EXCLUSIONS' "$script" 2>/dev/null; then
        references_canonical=true
    fi

    if "$sources_preflight" && "$references_canonical"; then
        # Script is canonical-aware. Good.
        continue
    fi

    # Violation. Record what's missing.
    rel_path="${script#$REPO_ROOT/}"
    if [[ "$violations" -eq 0 ]]; then
        echo "ERROR: One or more check-*.sh scripts hand-roll --exclude-dir= lists" >&2
        echo "       instead of consuming the canonical REPO_WALK_EXCLUSIONS list." >&2
        echo "" >&2
        echo "  See #4308 for the root cause and migration pattern." >&2
        echo "" >&2
        echo "  Required shape:" >&2
        echo "    source \"\$SCRIPT_DIR/preflight.sh\"" >&2
        echo "    for dir in \"\${REPO_WALK_EXCLUSIONS[@]}\"; do" >&2
        echo "        exclude_args+=(\"--exclude-dir=\$dir\")" >&2
        echo "    done" >&2
        echo "" >&2
        echo "  Per-check extras (rare) go in EXTRA_EXCLUDE_DIRS=(...)" >&2
        echo "  and are appended to the canonical list. See" >&2
        echo "  scripts/preflight.sh REPO_WALK_EXCLUSIONS docstring." >&2
        echo "" >&2
        echo "  Violations:" >&2
    fi

    missing=()
    "$sources_preflight" || missing+=("does not source scripts/preflight.sh")
    "$references_canonical" || missing+=("does not reference REPO_WALK_EXCLUSIONS")

    detail="    - $rel_path"
    for note in "${missing[@]}"; do
        detail+=$'\n'"      · $note"
    done
    violation_details+=("$detail")
    violations=$((violations + 1))
done

# ─── Report ─────────────────────────────────────────────────────────
if [[ $violations -gt 0 ]]; then
    for d in "${violation_details[@]}"; do
        echo "$d" >&2
    done
    echo "" >&2
    echo "  Found $violations script(s) with hand-rolled --exclude-dir lists." >&2
    echo "  Migrate by sourcing preflight.sh and iterating REPO_WALK_EXCLUSIONS." >&2
    exit 1
fi

echo "All clean — every check-*.sh that uses --exclude-dir= consumes REPO_WALK_EXCLUSIONS."
exit 0
