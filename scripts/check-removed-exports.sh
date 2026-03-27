#!/usr/bin/env bash
# check-removed-exports.sh — Detect broken imports from removed/renamed exports.
#
# Diffs the current branch against origin/main to identify removed or renamed
# Python function, class, and constant definitions. Then greps the codebase for
# import sites that reference those names. Fails if any stale imports are found.
#
# This catches the class of bug where a refactor removes a function from a module
# but forgets to update all the files that import it — the kind of bug that caused
# broken tests in #1993 (removed SplitRuling/split_rulings from riverside but
# test_reingest_from_s3.py still imported them).
#
# Usage:
#   scripts/check-removed-exports.sh              # diff against origin/main
#   scripts/check-removed-exports.sh <base-ref>   # diff against a specific ref
#
# Exit codes:
#   0 — No stale imports found (or no removed exports to check).
#   1 — Stale imports detected.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_REF="${1:-origin/main}"

# Ensure the base ref exists locally
if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
    git fetch origin main --quiet 2>/dev/null || true
    if ! git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
        echo "check-removed-exports: cannot resolve $BASE_REF — skipping."
        exit 0
    fi
fi

# Get diff between base and HEAD for Python files only.
# We look at removed lines (starting with -) that define functions, classes,
# or __all__ entries.
DIFF_OUTPUT=$(git diff "$BASE_REF"...HEAD -- '*.py' 2>/dev/null || true)

if [[ -z "$DIFF_OUTPUT" ]]; then
    echo "check-removed-exports: no Python changes — all clean."
    exit 0
fi

# Collect removed names. We look for:
#   1. Lines starting with "-def <name>(" (removed function definitions)
#   2. Lines starting with "-class <name>" (removed class definitions)
#   3. Lines starting with "-<NAME> = " where NAME is UPPER_CASE (removed constants)
#   4. Removed entries from __all__ lists (e.g., -    "FunctionName",)
#
# We exclude lines starting with "---" (diff file headers).

REMOVED_NAMES=""

# Extract removed function names
FUNCS=$(echo "$DIFF_OUTPUT" \
    | grep -E '^\-def [a-zA-Z_][a-zA-Z0-9_]*\(' \
    | sed -E 's/^\-def ([a-zA-Z_][a-zA-Z0-9_]*)\(.*/\1/' \
    | sort -u || true)

# Extract removed class names
CLASSES=$(echo "$DIFF_OUTPUT" \
    | grep -E '^\-class [a-zA-Z_][a-zA-Z0-9_]*' \
    | sed -E 's/^\-class ([a-zA-Z_][a-zA-Z0-9_]*).*/\1/' \
    | sort -u || true)

# Extract removed UPPER_CASE constants (e.g., -MY_CONSTANT = ...)
# Only match names that are fully uppercase with underscores.
CONSTANTS=$(echo "$DIFF_OUTPUT" \
    | grep -E '^\-[A-Z][A-Z0-9_]+ =' \
    | sed -E 's/^\-([A-Z][A-Z0-9_]+) =.*/\1/' \
    | sort -u || true)

# Combine all removed names
for name in $FUNCS $CLASSES $CONSTANTS; do
    # Skip private names (starting with _) — they are internal and less likely
    # to be imported cross-module. But do include them since cross-file imports
    # of private names do exist (e.g., test files importing _internal_helper).
    REMOVED_NAMES="${REMOVED_NAMES}${name}\n"
done

# Deduplicate
REMOVED_NAMES=$(printf '%b' "$REMOVED_NAMES" | sort -u | grep -v '^$' || true)

if [[ -z "$REMOVED_NAMES" ]]; then
    echo "check-removed-exports: no removed exports detected — all clean."
    exit 0
fi

# Now check which names are still imported somewhere in the codebase.
# Only check names that were REMOVED from the diff — if a name was both
# removed and added back (i.e., renamed/moved), we still check because
# the import sites may reference the old module path.

# Get the list of files that were MODIFIED in this branch (so we can
# exclude them — the author presumably updated imports in files they touched).
CHANGED_FILES=$(git diff --name-only "$BASE_REF"...HEAD -- '*.py' 2>/dev/null || true)

STALE_IMPORTS=""
STALE_COUNT=0

while IFS= read -r name; do
    [[ -z "$name" ]] && continue

    # Check if this name was also ADDED (meaning it was moved/renamed, not just removed).
    # If it still exists in the codebase as a definition, it's not a removed export.
    ADDED_BACK=$(echo "$DIFF_OUTPUT" \
        | grep -E '^\+def '"$name"'\(|^\+class '"$name"'[^a-zA-Z0-9_]|^\+'"$name"' =' \
        | head -1 || true)
    if [[ -n "$ADDED_BACK" ]]; then
        # Name was re-added (moved/renamed) — skip
        continue
    fi

    # Search for imports of this name across the codebase.
    # Patterns: "from ... import name" or "from ... import (...name...)"
    # or "import module.name"
    IMPORT_HITS=$(grep -rnl \
        --include='*.py' \
        -E "(from\s+\S+\s+import\s+.*\b${name}\b|import\s+\S+\.${name}\b)" \
        "$REPO_ROOT/packages/" "$REPO_ROOT/scripts/" 2>/dev/null || true)

    if [[ -z "$IMPORT_HITS" ]]; then
        continue
    fi

    # Filter out files that were changed in this branch (author should have
    # updated those).
    while IFS= read -r hit_file; do
        [[ -z "$hit_file" ]] && continue
        # Convert to relative path for comparison
        relative_path="${hit_file#"$REPO_ROOT/"}"
        is_changed=false
        while IFS= read -r changed; do
            [[ -z "$changed" ]] && continue
            if [[ "$relative_path" == "$changed" ]]; then
                is_changed=true
                break
            fi
        done <<< "$CHANGED_FILES"

        if [[ "$is_changed" == "false" ]]; then
            # This file imports the removed name but was not touched by the branch
            STALE_IMPORTS="${STALE_IMPORTS}  ${relative_path}: imports removed name '${name}'\n"
            STALE_COUNT=$((STALE_COUNT + 1))
        fi
    done <<< "$IMPORT_HITS"

done <<< "$REMOVED_NAMES"

if [[ $STALE_COUNT -gt 0 ]]; then
    echo "ERROR: Stale imports detected — ${STALE_COUNT} file(s) import removed exports."
    echo ""
    echo "The following files import names that were removed in this branch"
    echo "but the files themselves were not updated:"
    echo ""
    printf '%b' "$STALE_IMPORTS"
    echo ""
    echo "Fix: Update or remove the stale imports in the listed files."
    echo "     If the function/class was moved (not deleted), update the import path."
    echo "     If it was deleted, remove the import and update the calling code."
    echo ""
    echo "See https://github.com/judgemind/judgemind/issues/2001 for context."
    exit 1
fi

echo "check-removed-exports: checked $(echo "$REMOVED_NAMES" | wc -l | tr -d ' ') removed name(s) — no stale imports found."
exit 0
