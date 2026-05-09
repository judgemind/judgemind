#!/usr/bin/env bash
# check-no-ci-classifier-duplication.sh — Forbid spelling out the CI
# rollup-classifier conclusion vocabulary anywhere except the canonical
# implementation in scripts/dispatcher/phase_transitions.py and the
# CLI wrapper at scripts/dispatcher/ci_classifier_cli.py (#4417).
#
# Background
# ----------
#
# Pre-#4417 four sites duplicated the "which check conclusions count
# as red" rule:
#
#   1. scripts/dispatcher/phase_transitions.py — Python
#      _CI_FAILURE_CONCLUSIONS frozenset + _ci_rollup_state.
#   2. scripts/dispatcher/agent-runner-entrypoint.sh — inline jq
#      classify_pr_rollup program (Fargate per-agent task).
#   3. scripts/dispatcher/agent-runner-entrypoint.sh + daemon.py —
#      paired Python _extract_failing_jobs mirrors with their own
#      ``failure_conclusions`` set.
#   4. scripts/worker-status.sh — awk regex inside the operator
#      dashboard.
#
# Two fix-ci-class regressions (#4407 → PR #4411 for wait-for-ci.sh,
# #4414 → PR #4415 for the four sites above) had to touch every copy
# independently. #4417 unified them on phase_transitions._ci_rollup_state
# + ci_classifier_cli.py. This guard prevents new duplicates from
# sneaking back in: it greps for the verbatim conclusion vocabulary
# (frozenset({"FAILURE", ...}) in Python, or jq's
# ``$x == "FAILURE" or $x == "TIMED_OUT"`` shape) and fails if any file
# outside the allowlist matches.
#
# Note: ``scripts/wait-for-ci.sh`` is intentionally Bash-only for /task
# agents (no Python dependency required) and is allowlisted. A separate
# fixture-parity test (test_ci_classifier_cli.py) keeps it from drifting.
#
# Usage:
#   scripts/check-no-ci-classifier-duplication.sh          # scan repo
#   scripts/check-no-ci-classifier-duplication.sh [dir]    # scan a dir
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more files spell out the classifier vocabulary verbatim
#       outside the allowlist. Prints a Fix block per the
#       docs/dx/check-script-fix-block-coverage.md contract.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

# ─── Patterns ────────────────────────────────────────────────────────────
#
# Pattern 1 — Python frozenset literal that names FAILURE alongside
# at least two other failure conclusions. Catches the exact shape that
# was duplicated in daemon.py + agent-runner-entrypoint.sh.
PYTHON_FROZENSET_PATTERN='frozenset\(\s*\{[^}]*"FAILURE"[^}]*"TIMED_OUT"'

# Pattern 2 — Python set literal naming TIMED_OUT alongside FAILURE.
# Catches the bare ``failure_conclusions = {...}`` shape both
# entrypoint.sh's heredoc and daemon.py spelled out before #4417.
PYTHON_SET_PATTERN='\{[^}]*"FAILURE"[^}]*"TIMED_OUT"[^}]*\}'

# Pattern 3 — jq / shell-comparison vocabulary. Matches the exact
# shape inside the deleted agent-runner-entrypoint.sh classify_pr_rollup
# jq program.
JQ_PATTERN='==\s*"FAILURE"[[:space:]]*or[[:space:]]*[^=]*==\s*"TIMED_OUT"'

# Pattern 4 — shell awk/grep vocabulary that pairs FAILURE with
# TIMED_OUT in a quoted-or chain. Catches both the awk regex from
# worker-status.sh and any future grep -E '"FAILURE"|"TIMED_OUT"'
# style copies.
AWK_PATTERN='"FAILURE"[[:space:]]*\|\|[[:space:]]*[^|]*"TIMED_OUT"'

# ─── Allowlist — files that legitimately spell the vocabulary ───────────
# These are the canonical sources of truth (and this check itself).
ALLOWLIST=(
    "scripts/dispatcher/phase_transitions.py"
    "scripts/dispatcher/ci_classifier_cli.py"
    # Tests must be allowed to assert the vocabulary verbatim; the
    # fixture data is exactly the conclusion strings.
    "scripts/dispatcher/tests/test_ci_classifier_cli.py"
    "scripts/dispatcher/tests/test_ci_classifier_consistency.py"
    "scripts/dispatcher/tests/test_daemon_phase3b.py"
    "scripts/tests/test_worker_status.sh"
    "scripts/tests/test_check_no_ci_classifier_duplication.sh"
    # The check itself spells the patterns literally.
    "scripts/check-no-ci-classifier-duplication.sh"
    # wait-for-ci.sh is intentionally Bash-only (no Python dep needed
    # for /task agents); a fixture-parity test in
    # test_ci_classifier_cli.py keeps it from drifting.
    "scripts/wait-for-ci.sh"
    # Tests for wait-for-ci.sh assert the same vocabulary by design.
    "scripts/tests/test_wait_for_ci.sh"
    # Inventory doc that documents the guard's rationale — the row
    # quotes the forbidden vocabulary as part of its prose.
    "docs/dx/check-script-fix-block-coverage.md"
)

# ─── Build --exclude-dir args from the canonical list ───────────────────

exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

# ─── Scan ───────────────────────────────────────────────────────────────

violations=0
PATTERNS=(
    "$PYTHON_FROZENSET_PATTERN"
    "$PYTHON_SET_PATTERN"
    "$JQ_PATTERN"
    "$AWK_PATTERN"
)

is_allowlisted() {
    local path="$1"
    local rel="${path#"$REPO_ROOT/"}"
    for entry in "${ALLOWLIST[@]}"; do
        if [[ "$rel" == "$entry" ]]; then
            return 0
        fi
    done
    return 1
}

declare -a violation_lines=()

for pat in "${PATTERNS[@]}"; do
    matches=$(grep -rnE "$pat" "$SCAN_DIR" "${exclude_args[@]}" 2>/dev/null || true)
    if [[ -z "$matches" ]]; then
        continue
    fi
    while IFS= read -r line; do
        # grep -rn output: <path>:<lineno>:<content>
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        # Skip allowlisted files.
        if is_allowlisted "$file_path"; then
            continue
        fi

        # Skip docs/investigations/* — historical context.
        if [[ "$file_path" == *"docs/investigations/"* ]]; then
            continue
        fi

        # Skip comment-only lines (first non-whitespace is # or //).
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi
        if [[ "$content" =~ ^[[:space:]]*// ]]; then
            continue
        fi

        violation_lines+=("$line")
        violations=$((violations + 1))
    done <<< "$matches"
done

if [[ $violations -gt 0 ]]; then
    echo "ERROR: Found CI rollup-classifier vocabulary spelled out outside the allowlist."
    echo ""
    echo "Pre-#4417 the rule was duplicated across four sites and the same bug"
    echo "class — \"treat CANCELLED as non-blocking\" — surfaced twice (#4407, #4414)"
    echo "and required parallel fixes in every copy. The canonical implementation"
    echo "now lives in:"
    echo ""
    echo "  - scripts/dispatcher/phase_transitions.py (_ci_rollup_state +"
    echo "    extract_failing_jobs + the _CI_*_CONCLUSIONS frozensets)"
    echo "  - scripts/dispatcher/ci_classifier_cli.py (CLI wrapper for"
    echo "    non-Python callers — reads JSON from stdin, prints"
    echo "    green/red/pending/error)"
    echo ""
    echo "Violations:"
    for line in "${violation_lines[@]}"; do
        echo "    $line"
    done
    echo ""
    echo "Fix:"
    echo "  - Python callers: import phase_transitions.extract_failing_jobs"
    echo "    or phase_transitions._ci_rollup_state instead of spelling out a"
    echo "    new ``failure_conclusions = {...}`` set."
    echo "  - Bash callers: pipe the rollup JSON into"
    echo "    ``python3 scripts/dispatcher/ci_classifier_cli.py`` and branch"
    echo "    on green/red/pending/error."
    echo "  - jq programs: same as Bash — replace the inline jq classifier"
    echo "    with a CLI invocation."
    echo ""
    echo "If a new file legitimately needs the vocabulary (e.g. a new test"
    echo "that asserts the canonical fixture set), add the path to the"
    echo "ALLOWLIST array in this script."
    exit 1
fi

echo "All clean — CI rollup-classifier vocabulary is centralized."
exit 0
