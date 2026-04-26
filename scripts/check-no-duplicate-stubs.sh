#!/usr/bin/env bash
# check-no-duplicate-stubs.sh — Forbid duplicate stub-generation blocks for
# the same binary in any test file under scripts/tests/.
#
# A stub definition looks like:
#   cat > "$STUB_BIN/<binary>" <<HEREDOC
#
# When the same binary appears more than once in a single test file, the
# second block silently overwrites the first — discarding any routes that
# were merged into the initial definition.  This caused a regression in
# test_agent_runner_entrypoint.sh where Stage-1 gh/psql routes were
# dropped when Stage-2 definitions re-declared the same binary (see #3415).
#
# Usage:
#   scripts/check-no-duplicate-stubs.sh          # scan scripts/tests/test_*.sh
#   scripts/check-no-duplicate-stubs.sh [dir]    # scan test_*.sh under [dir]
#
# Exit codes:
#   0 — No violations found.
#   1 — One or more test files redefine the same stub binary more than once.

# venv: none
# permanent: true

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT/scripts/tests}"

# ─── Files that legitimately contain duplicate stubs as test fixtures ─────────
# The peer test for this guard creates intentional duplicate stubs to verify
# that the guard detects them.  Exclude it from the scan.
EXCLUDE_FILES=(
    "scripts/tests/test_check_no_duplicate_stubs.sh"
)

violations=0

# ─── Scan each test file ─────────────────────────────────────────────────────
while IFS= read -r -d '' test_file; do
    # Skip explicitly-excluded files.
    skip_file=false
    for excl in "${EXCLUDE_FILES[@]}"; do
        if [[ "$test_file" == *"$excl" ]]; then
            skip_file=true
            break
        fi
    done
    if "$skip_file"; then
        continue
    fi

    # Collect all binary names and their line numbers in this file.
    # Format: "<lineno> <binary>"
    declare -A binary_first_line=()
    declare -A binary_count=()

    while IFS= read -r match_line; do
        # match_line format from grep -n: "<lineno>:<content>"
        lineno="${match_line%%:*}"
        content="${match_line#*:}"

        # Skip comment lines (first non-whitespace char is #).
        if [[ "$content" =~ ^[[:space:]]*# ]]; then
            continue
        fi

        # Extract the binary name via shell pattern match.
        # Pattern: cat > "$STUB_BIN/<binary>" <<
        if [[ "$content" =~ cat[[:space:]]+\>[[:space:]]+\"\$STUB_BIN/([^\"]*)\"[[:space:]]+\<\< ]]; then
            binary="${BASH_REMATCH[1]}"

            if [[ -z "${binary_first_line[$binary]:-}" ]]; then
                binary_first_line[$binary]="$lineno"
                binary_count[$binary]=1
            else
                binary_count[$binary]=$(( binary_count[$binary] + 1 ))
                if [[ ${binary_count[$binary]} -eq 2 ]]; then
                    # First time we detect a duplicate — emit the error.
                    if [[ $violations -eq 0 ]]; then
                        echo "ERROR: Test file(s) redefine the same stub binary more than once."
                        echo ""
                        echo "  The second cat > \"\$STUB_BIN/<binary>\" << block overwrites the"
                        echo "  first, silently discarding any routes merged into the initial"
                        echo "  definition.  Consolidate both definitions into the first block."
                        echo ""
                    fi
                    echo "    File:   $test_file"
                    echo "    Binary: $binary"
                    echo "    Line 1: ${binary_first_line[$binary]}"
                    echo "    Line 2: $lineno"
                    echo ""
                    violations=$(( violations + 1 ))
                fi
            fi
        fi
    done < <(grep -n 'cat > "\$STUB_BIN/' "$test_file" 2>/dev/null || true)

    unset binary_first_line
    unset binary_count
done < <(find "$SCAN_DIR" -maxdepth 1 -name 'test_*.sh' -print0 2>/dev/null)

if [[ $violations -gt 0 ]]; then
    echo "  Found $violations duplicate stub definition(s)."
    echo ""
    echo "  Fix: merge the routes from the second stub block into the first"
    echo "  definition, then delete the second cat > \"\$STUB_BIN/<binary>\" <<"
    echo "  block entirely."
    echo ""
    exit 1
fi

echo "All clean — no duplicate stub definitions detected."
exit 0
