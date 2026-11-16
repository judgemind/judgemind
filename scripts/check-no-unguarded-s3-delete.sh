#!/usr/bin/env bash
# scans: packages/*/src/ infra/ .github/ scripts/
# (#5088: paths this guard reads; check-guard-path-filters.sh checks its CI job runs when they change.)
# check-no-unguarded-s3-delete.sh — S3 deletes must not bypass the raw guard (#4991, #5013).
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
# check fails when any other file under scripts/, packages/, .github/ or
# infra/ (tests excluded) deletes from S3:
#
#   - boto3 client:   delete_object( / delete_objects(
#   - boto3 resource: s3.Object(...).delete(), bucket.objects.filter(...).delete(),
#                     object_versions...delete(), and any .delete( call in a
#                     file that builds a resource("s3") /
#                     resource(service_name="s3") or calls .Object( /
#                     .ObjectVersion(
#   - AWS CLI:        aws s3 rm / mv / rb, aws s3 sync ... --delete,
#                     aws s3api delete-object[s]; global options may come
#                     first (aws --region us-west-2 s3 rm), and a command
#                     continued with trailing backslashes is joined first
#   - JS SDK:         DeleteObject[s]Command
#   - lifecycle:      put_bucket_lifecycle* / put-bucket-lifecycle* /
#                     PutBucketLifecycle* (an expiration rule deletes too)
#
# unless the call carries a marker comment, on the same line or on one of the
# three lines above it, saying what it deletes and why that is not a raw:
#
#     # s3-delete-not-raw: <what is deleted, e.g. llm-cache/ delete markers>
#
# The marker covers only the call(s) right below it, not the whole file
# (#5013).
#
# It also fails on a Terraform aws_s3_bucket_lifecycle_configuration, in any
# *.tf file, whose bucket is the archive and which has a rule (any indent,
# `dynamic "rule"` included) that expires objects outside spotcheck/, tmp/
# and migrations/ (scripts/_tf_archive_lifecycle_expiry.awk, #5089).  No
# marker excuses that.
#
# The archive's bucket policy denies raw deletes regardless; this check
# keeps new code from relying on that backstop.
#
# Usage:
#   scripts/check-no-unguarded-s3-delete.sh          # scan scripts/ packages/ .github/ infra/
#   scripts/check-no-unguarded-s3-delete.sh DIR...   # scan the given directories
#
# Exit codes:
#   0 — no unguarded S3 delete.
#   1 — at least one S3 delete without the guard or a marker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# Tests point the default scan roots at a fixture tree.
REPO_ROOT="${S3_DELETE_CHECK_REPO_ROOT:-$REPO_ROOT}"

# shellcheck source=./preflight.sh
source "$SCRIPT_DIR/preflight.sh"

if [[ $# -gt 0 ]]; then
    SCAN_DIRS=("$@")
else
    SCAN_DIRS=("$REPO_ROOT/scripts" "$REPO_ROOT/packages" "$REPO_ROOT/.github" "$REPO_ROOT/infra")
fi

# Global options may sit between aws and s3: aws --region us-west-2 s3 rm
# (#5089).  An option's value is a word that does not start with "-".
S3CLI='aws([[:space:]]+--[^[:space:]]+([[:space:]]+[^-[:space:]][^[:space:]]*)?)*[[:space:]]+s3[[:space:]]+'
PATTERN="delete_objects?\("
PATTERN+="|${S3CLI}(rm|mv|rb)([[:space:]]|$)"
PATTERN+="|${S3CLI}sync[[:space:]].*--delete"
PATTERN+='|s3api[[:space:]]+delete-objects?'
PATTERN+='|DeleteObjects?Command'
PATTERN+='|\.(Object|ObjectVersion)\(.*\)\.delete\('
PATTERN+='|\.(objects|object_versions)\.([a-z_]+\(.*\)\.)?delete\('
PATTERN+='|put_bucket_lifecycle|put-bucket-lifecycle|PutBucketLifecycle'
# A file that builds a boto3 S3 resource (resource("s3") or
# resource(service_name="s3")) or uses its Object / ObjectVersion classes:
# any .delete( in it may be an object delete held in a variable
# (obj = bucket.Object(k); obj.delete()), #5089.
RESOURCE_PATTERN="resource\([[:space:]]*(service_name[[:space:]]*=[[:space:]]*)?['\"]s3['\"]"
RESOURCE_PATTERN+='|\.(Object|ObjectVersion)\('
RESOURCE_DELETE='\.delete\('
# Terraform lifecycle expirations on the archive may only target these
# throwaway prefixes (the same list as the storage module's policy check).
LIFECYCLE_ALLOWED_PREFIXES="spotcheck/ tmp/ migrations/"
LIFECYCLE_AWK="$SCRIPT_DIR/_tf_archive_lifecycle_expiry.awk"
MARKER='s3-delete-not-raw:[[:space:]]*[^[:space:]]'
MARKER_LOOKBACK=3
GUARD_MODULE="packages/scraper-framework/src/framework/raw_delete_guard.py"

exclude_args=()
for dir in "${REPO_WALK_EXCLUSIONS[@]}"; do
    exclude_args+=("--exclude-dir=$dir")
done
exclude_args+=("--exclude-dir=tests" "--exclude=test_*")

existing_dirs=()
for dir in "${SCAN_DIRS[@]}"; do
    [[ -e "$dir" ]] && existing_dirs+=("$dir")
done
if [[ ${#existing_dirs[@]} -eq 0 ]]; then
    echo "All clean: no directory to scan."
    exit 0
fi

matches=$(grep -rnIE "$PATTERN" "${existing_dirs[@]}" "${exclude_args[@]}" 2>/dev/null || true)

resource_files=$(grep -rlIE "$RESOURCE_PATTERN" "${existing_dirs[@]}" "${exclude_args[@]}" 2>/dev/null || true)
if [[ -n "$resource_files" ]]; then
    while IFS= read -r f; do
        hits=$(grep -nE "$RESOURCE_DELETE" "$f" 2>/dev/null || true)
        [[ -z "$hits" ]] && continue
        while IFS= read -r hit; do
            matches+=$'\n'"$f:$hit"
        done <<< "$hits"
    done <<< "$resource_files"
fi

# True when a marker sits on line $2 of file $1, on one of the lines above,
# or (for a command continued over several lines) on any line up to $3.
has_marker() {
    local file="$1" lineno="$2" end="${3:-$2}"
    local start=$((lineno - MARKER_LOOKBACK))
    [[ $start -lt 1 ]] && start=1
    sed -n "${start},${end}p" "$file" | grep -qE "$MARKER"
}

# A command split with trailing backslashes (aws s3 \ / rm ...) matches no
# single line (#5089).  Join each continued command and print
# file:first:last:joined for those spanning more than one line.
JOIN_AWK='
FNR == 1 { open = 0 }
{
    line = $0
    cont = (line ~ /\\[ \t]*$/)
    if (cont) sub(/\\[ \t]*$/, " ", line)
    if (!open) { first = FNR; buf = line; open = 1 } else { buf = buf line }
    if (!cont) {
        if (FNR > first) print FILENAME ":" first ":" FNR ":" buf
        open = 0
    }
}'
continued=""
continued_files=$(grep -rlIE '\\[[:space:]]*$' "${existing_dirs[@]}" "${exclude_args[@]}" 2>/dev/null || true)
if [[ -n "$continued_files" ]]; then
    while IFS= read -r f; do
        joined=$(awk "$JOIN_AWK" "$f" | grep -E "^[^:]*:[0-9]+:[0-9]+:.*($PATTERN)" || true)
        [[ -n "$joined" ]] && continued+="$joined"$'\n'
    done <<< "$continued_files"
fi

# Terraform lifecycle rules that expire archive objects outside the
# throwaway prefixes (#5089).  Every *.tf file, any indent, dynamic "rule"
# blocks included.  No marker excuses these: a raw-bearing prefix must not
# expire at all.
lifecycle_hits=""
lifecycle_files=$(grep -rlE 'aws_s3_bucket_lifecycle_configuration' "${existing_dirs[@]}" \
    --include='*.tf' "${exclude_args[@]}" 2>/dev/null || true)
if [[ -n "$lifecycle_files" ]]; then
    while IFS= read -r f; do
        hits=$(awk -v allowed="$LIFECYCLE_ALLOWED_PREFIXES" -f "$LIFECYCLE_AWK" "$f")
        [[ -n "$hits" ]] && lifecycle_hits+="$hits"$'\n'
    done <<< "$lifecycle_files"
fi

violations=0
reported=""
seen=""
# Report $1 (file:line:content) unless it is exempt.  $2 = last line of a
# continued command (marker search range); $3 = "no-marker" when a marker
# does not excuse the hit.
check_hit() {
    local line="$1" end="${2:-}" mode="${3:-}" file_path rest lineno content
    file_path="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    content="${rest#*:}"

    case "$file_path" in
        *"$GUARD_MODULE" | *scripts/check-no-unguarded-s3-delete.sh) return 0 ;;
    esac
    # A line hit by more than one scan is reported once.
    case "$seen" in
        *"|$file_path:$lineno|"*) return 0 ;;
    esac
    seen+="|$file_path:$lineno|"
    # Comment lines may name the calls as context.
    if [[ "$content" =~ ^[[:space:]]*(#|//|\*) ]]; then
        return 0
    fi
    if [[ "$mode" != "no-marker" ]] && has_marker "$file_path" "$lineno" "${end:-$lineno}"; then
        return 0
    fi

    if [[ $violations -eq 0 ]]; then
        echo "ERROR: S3 delete outside framework.raw_delete_guard, with no marker:"
        echo ""
    fi
    echo "    $line"
    violations=$((violations + 1))
    reported="$file_path"
}

if [[ -n "$matches" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        check_hit "$line"
    done <<< "$matches"
fi
if [[ -n "$continued" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        # file:first:last:joined
        c_file="${line%%:*}"
        c_rest="${line#*:}"
        c_first="${c_rest%%:*}"
        c_rest="${c_rest#*:}"
        c_last="${c_rest%%:*}"
        c_joined="${c_rest#*:}"
        # A delete already on one physical line was reported above.
        if sed -n "${c_first},${c_last}p" "$c_file" | grep -qE "$PATTERN"; then
            continue
        fi
        check_hit "$c_file:$c_first:$c_joined" "$c_last"
    done <<< "$continued"
fi
if [[ -n "$lifecycle_hits" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        check_hit "$line" "" no-marker
    done <<< "$lifecycle_hits"
fi

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "  Found $violations unguarded S3 delete call(s) (last file: $reported)."
    echo "  No raw may be deleted unless a live byte-identical copy exists"
    echo "  (docs/specs/architecture-spec-v1.md §3.3.3, #4991, #5013)."
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
    echo "  If the call deletes something that is not a raw (a temp upload, an"
    echo "  llm-cache/ delete marker), put a marker comment on the line above it"
    echo "  (it covers that call only, up to $MARKER_LOOKBACK lines below):"
    echo ""
    echo "    # s3-delete-not-raw: <what is deleted and why it is not a raw>"
    echo ""
    echo "  A Terraform lifecycle rule on the archive that expires objects takes"
    echo "  no marker: filter it on a throwaway prefix ($LIFECYCLE_ALLOWED_PREFIXES):"
    echo ""
    echo "    rule {"
    echo "      id     = \"tmp-ttl\""
    echo "      status = \"Enabled\""
    echo "      filter {"
    echo "        prefix = \"tmp/\""
    echo "      }"
    echo "      expiration {"
    echo "        days = 30"
    echo "      }"
    echo "    }"
    exit 1
fi

echo "All clean: every S3 delete goes through framework.raw_delete_guard or is marked not-raw."
exit 0
