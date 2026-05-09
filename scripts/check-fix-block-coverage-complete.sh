#!/usr/bin/env bash
# permanent: true
# check-fix-block-coverage-complete.sh — Fail if any executable hygiene
# guard under `scripts/check-*.{sh,py}` is missing a row in
# `docs/dx/check-script-fix-block-coverage.md`.
#
# Why this check exists
# ---------------------
# `docs/dx/check-script-fix-block-coverage.md` is the per-guard health-check
# inventory for the Fix-block contract introduced in #4322 / #4345 / #4346.
# Without an automated guard, the doc silently drifts behind reality: a
# guard ships, the row is forgotten, and "the doc is the canonical
# inventory" stops being true. Issue #4367 motivated this check after the
# same drift surfaced while wiring `check-no-unbounded-timeouts.py` into
# pre-push (PR #4365).
#
# What gets flagged
# -----------------
# Any executable file matching `scripts/check-*.sh`, `scripts/check-*.py`,
# or `scripts/check_*.py` whose basename does not appear in the inventory
# table.
#
# Companion `.py` deduplication
# -----------------------------
# When both `scripts/check-foo.sh` AND `scripts/check-foo.py` exist, the
# `.py` is treated as the implementation and the `.sh` as the canonical
# wrapper — same convention `scripts/run-ci-guards.sh` uses. Documenting
# the `.sh` row is sufficient; the `.py` companion does not need its own
# row (the wrapper's Notes column already names it). This matches the
# existing rows like #51 `check-nullable-column-reads.sh` "Wrapper for
# `check-nullable-column-reads.py`".
#
# Opt-out marker (`# ci-guards: skip`)
# ------------------------------------
# Independent of this check. The marker only suppresses execution by the
# umbrella runner — it does NOT exempt a guard from the inventory. New
# guards must always carry a row in the inventory; the marker just lets
# the umbrella skip running them when they require external context
# (network, issue numbers, npm-installed deps, etc.).
#
# Usage
# -----
#   scripts/check-fix-block-coverage-complete.sh           # scan repo
#
# Exit codes
# ----------
#   0 — Every executable guard has a row in the inventory.
#   1 — One or more guards are missing rows. Each missing guard is listed;
#       a Fix block names the doc and the row insertion point.
#   2 — Script error (doc missing, scripts/ dir not found, etc.).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Test-override hooks (issue #4405). Production paths are derived from
# $REPO_ROOT; tests in scripts/tests/ point these at a synthesized
# directory + inventory so the per-guard Fix-block contract can be
# exercised end-to-end without touching the real tree. Unset in CI.
SCRIPTS_DIR="${SCRIPTS_DIR_OVERRIDE:-$REPO_ROOT/scripts}"
DOC="${DOC_OVERRIDE:-$REPO_ROOT/docs/dx/check-script-fix-block-coverage.md}"

if [ ! -d "$SCRIPTS_DIR" ]; then
    echo "ERROR: scripts/ dir not found: $SCRIPTS_DIR" >&2
    exit 2
fi

if [ ! -f "$DOC" ]; then
    echo "ERROR: inventory doc not found: $DOC" >&2
    exit 2
fi

# Discover every executable check file: check-*.sh, check-*.py, check_*.py.
# .py files invoked via `python3 …` from CI / pre-push do not require an
# executable bit (CI canonical — see scripts/run-ci-guards.sh comments) so
# we accept any regular file matching the glob. .sh files MUST be
# executable — a non-executable .sh is dead, not a guard. We mirror that
# semantic: only .sh files with +x get scanned; all .py files get scanned.
discovered=()
while IFS= read -r path; do
    [ -n "$path" ] && discovered+=("$path")
done < <(LC_ALL=C find "$SCRIPTS_DIR" -maxdepth 1 -type f \
            \( -name 'check-*.sh' -o -name 'check-*.py' -o -name 'check_*.py' \) \
         | LC_ALL=C sort)

if [ "${#discovered[@]}" -eq 0 ]; then
    echo "ERROR: no scripts/check-*.{sh,py} files found under $SCRIPTS_DIR" >&2
    exit 2
fi

# Filter: keep .sh files only if executable; keep all .py files; drop the
# umbrella self (we are not a guard).
SELF_BASENAME="$(basename "${BASH_SOURCE[0]}")"
keepers=()
for path in "${discovered[@]}"; do
    name="$(basename "$path")"
    [ "$name" = "$SELF_BASENAME" ] && continue
    case "$name" in
        check-*.sh)
            [ -x "$path" ] || continue
            ;;
        check-*.py|check_*.py)
            ;;
    esac
    keepers+=("$name")
done

# Build the set of basenames present (for companion-pair dedup).
declare -a basenames_present=()
for name in "${keepers[@]}"; do
    basenames_present+=("$name")
done
basename_present() {
    local needle="$1"
    local entry
    for entry in "${basenames_present[@]}"; do
        [ "$entry" = "$needle" ] && return 0
    done
    return 1
}

# When both check-foo.sh and check-foo.py exist, drop the .py — the .sh
# wrapper's row covers it. Only the hyphen-named pair is recognised; the
# underscore-named .py files (check_split_…py, check_tf_…py, etc.) have
# no .sh sibling and are documented in their own right.
required=()
for name in "${keepers[@]}"; do
    case "$name" in
        check-*.py)
            stem="${name%.py}"
            sh_companion="${stem}.sh"
            if basename_present "$sh_companion"; then
                continue  # covered by .sh wrapper's row
            fi
            ;;
    esac
    required+=("$name")
done

# Required basenames — these MUST appear in the inventory.
required_sorted=$(printf '%s\n' "${required[@]}" | LC_ALL=C sort -u)

# Extract the set of guard basenames documented in the inventory. The doc
# references guards as "`scripts/check-foo.sh`" (or check_foo.py) inside
# table rows — strip the path prefix for comparison.
documented_basenames=$(grep -oE 'scripts/check[-_][a-zA-Z0-9_-]+\.(sh|py)' "$DOC" \
                       | sed 's|^scripts/||' | LC_ALL=C sort -u)

# Compute required-but-undocumented. Force LC_ALL=C on comm so the
# byte-order comparison matches the byte-order sort used to build both
# inputs above. Without this, locale-aware comm treats `-` and `_` as
# equivalent collation classes and reports underscored-name guards
# (`check_tf_ecs_entrypoint.py`, etc.) as missing even when they ARE
# documented.
missing=$(LC_ALL=C comm -23 \
    <(printf '%s\n' "$required_sorted") \
    <(printf '%s\n' "$documented_basenames"))

if [ -n "$missing" ]; then
    echo "FAIL: scripts/check-*.{sh,py} guard(s) missing from inventory:" >&2
    echo "" >&2
    while IFS= read -r name; do
        [ -n "$name" ] && echo "  - scripts/$name" >&2
    done <<< "$missing"
    echo "" >&2
    echo "Inventory: docs/dx/check-script-fix-block-coverage.md" >&2
    echo "" >&2
    # Per-guard Fix blocks (issue #4405) — name the exact letter-suffix
    # row number, a copy-pasteable row template with <guard> already
    # filled in, and the new "Total guards: N" count. Implemented in
    # scripts/check_fix_block_coverage_complete.py for testability.
    missing_args=()
    while IFS= read -r name; do
        [ -n "$name" ] && missing_args+=("$name")
    done <<< "$missing"
    if ! python3 "$REPO_ROOT/scripts/check_fix_block_coverage_complete.py" \
            --doc "$DOC" \
            "${missing_args[@]}"; then
        # Helper failed — fall through to the generic Fix block so CI
        # operators still get actionable guidance even if the per-guard
        # formatter regresses.
        echo "(Per-guard Fix-block formatter exited non-zero — falling back to generic Fix block.)" >&2
        echo "" >&2
        echo "Fix: add a row to the survey table for each missing guard. Use the" >&2
        echo "verdict vocabulary already in the doc:" >&2
        echo "" >&2
        echo "  | <N> | \`scripts/<guard>\` | <verdict> | <one-line note describing what the Fix block contains> |" >&2
        echo "" >&2
        echo "Verdict options: self-diagnosing (Fix block), self-diagnosing (actionable text)," >&2
        echo "wrapper (delegates to helper), operational health probe, decision flow, NEEDS UPGRADE." >&2
        echo "" >&2
        echo "To minimise renumbering churn, use letter-suffix row numbers (e.g. \`50a\`)" >&2
        echo "for the alphabetical insertion point — see the existing \`31a\` row for" >&2
        echo "\`check-issue-verify-sql.py\`." >&2
    fi
    echo "" >&2
    echo "Note: the \`# ci-guards: skip\` opt-out marker only suppresses umbrella" >&2
    echo "execution; it does NOT exempt a guard from the inventory. Even guards" >&2
    echo "that opt out of the umbrella must carry a row here." >&2
    exit 1
fi

echo "OK: every executable guard has a row in $DOC" >&2
exit 0
