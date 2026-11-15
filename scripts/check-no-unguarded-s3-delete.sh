#!/usr/bin/env bash
# check-no-unguarded-s3-delete.sh — S3 deletes must not bypass the raw guard (#4991).
#
# Raw S3 objects are the source of truth: a tentative ruling disappears from
# the court site within days, so a raw deleted without a live copy can never
# be rebuilt.  The rule (docs/specs/architecture-spec-v1.md §3.3.3) is that no
# raw is deleted unless a live, byte-identical copy exists.  Cleanups guarded
# only by "no DB row references the key" broke it: #4988 found 746 unique
# dated captures that scripts/archive/cleanup_legacy_date_partitioned_s3.py
# would have deleted.
#
# The only code allowed to delete from the raw archive is
# packages/scraper-framework/src/framework/raw_delete_guard.py, which
# SHA-256s the bytes and refuses unless another live key has them.  This
# check fails when any other file under scripts/ or packages/ (tests
# excluded) calls an S3 delete, unless the file carries a marker comment
# saying what it deletes and why that is not a raw:
#
#     # s3-delete-not-raw: <what is deleted, e.g. llm-cache/ delete markers>
#
# Usage:
#   scripts/check-no-unguarded-s3-delete.sh          # scan scripts/ and packages/
#   scripts/check-no-unguarded-s3-delete.sh DIR...   # scan the given directories
#
# Exit codes:
#   0 — no unguarded S3 delete.
#   1 — at least one file deletes from S3 without the guard or a marker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

if [[ $# -gt 0 ]]; then
    SCAN_DIRS=("$@")
else
    SCAN_DIRS=("$REPO_ROOT/scripts" "$REPO_ROOT/packages")
fi

# boto3 (delete_object / delete_objects), the AWS CLI (s3 rm, s3api
# delete-object[s]) and the JS SDK (DeleteObject[s]Command).
PATTERN='delete_objects?\(|aws[[:space:]]+s3[[:space:]]+rm|s3api[[:space:]]+delete-objects?|DeleteObjects?Command'
MARKER='s3-delete-not-raw:[[:space:]]*[^[:space:]]'
GUARD_MODULE="packages/scraper-framework/src/framework/raw_delete_guard.py"

exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done

matches=$(grep -rnE "$PATTERN" "${SCAN_DIRS[@]}" "${exclude_args[@]}" \
    --exclude-dir=tests --exclude='test_*' 2>/dev/null || true)

violations=0
reported=""
if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        file_path="${line%%:*}"
        rest="${line#*:}"
        content="${rest#*:}"

        case "$file_path" in
            *"$GUARD_MODULE" | *scripts/check-no-unguarded-s3-delete.sh) continue ;;
        esac
        # Comment lines may name the calls as context.
        if [[ "$content" =~ ^[[:space:]]*(#|//|\*) ]]; then
            continue
        fi
        if grep -qE "$MARKER" "$file_path"; then
            continue
        fi

        if [[ $violations -eq 0 ]]; then
            echo "ERROR: S3 delete outside framework.raw_delete_guard, with no marker:"
            echo ""
        fi
        echo "    $line"
        violations=$((violations + 1))
        reported="$file_path"
    done <<< "$matches"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations unguarded S3 delete call(s) (last file: $reported)."
    echo "  No raw may be deleted unless a live byte-identical copy exists"
    echo "  (docs/specs/architecture-spec-v1.md §3.3.3, #4991)."
    echo ""
    echo "  Fix: delete raws only through framework.raw_delete_guard:"
    echo ""
    echo "    from framework.raw_delete_guard import ("
    echo "        delete_raw_with_live_copy,  # refuses unless another live key has the bytes"
    echo "        index_live_by_hash,"
    echo "        list_live_keys,"
    echo "        remove_delete_marker,  # restore: removes only the latest delete marker"
    echo "    )"
    echo "    live_by_hash = index_live_by_hash(list_live_keys(s3, bucket, \"ca/\"))"
    echo "    delete_raw_with_live_copy(s3, bucket, key, live_by_hash)"
    echo ""
    echo "  If the file deletes something that is not a raw (a temp upload, an"
    echo "  llm-cache/ delete marker), add a marker comment naming it:"
    echo ""
    echo "    # s3-delete-not-raw: <what is deleted and why it is not a raw>"
    exit 1
fi

echo "All clean: every S3 delete goes through framework.raw_delete_guard or is marked not-raw."
exit 0
