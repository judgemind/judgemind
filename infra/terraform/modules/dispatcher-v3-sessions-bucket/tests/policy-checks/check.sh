#!/usr/bin/env bash
set -euo pipefail

# Policy-shape regression test for the dispatcher-v3-sessions-bucket module.
#
# Asserts the spec section 4.1 / section 10 invariants that are easy to
# break with a stray edit and hard to spot in code review:
#
#   1. The bucket policy grants ONLY s3:PutObject and s3:GetObject. No
#      s3:ListBucket, no s3:DeleteObject, no s3:* wildcard. Adding
#      another action would silently widen scope.
#   2. The bucket policy Principal is `var.agent_task_role_arn` only;
#      no cross-account principal, no wildcard, no IAM-user principal.
#   3. The public access block sets all four flags to true. Any flag
#      flipping to false would silently expose the bucket.
#   4. Versioning is enabled. SSE is configured.
#   5. The lifecycle rule transitions to GLACIER_IR at exactly 30 days
#      and expires at `var.session_retention_days`. A typo like
#      `STANDARD_IA` or `GLACIER` (deep archive) would silently
#      change cost or retrieval characteristics.
#
# This script runs against the rendered HCL only, no AWS credentials
# are needed and no actual resources are created. It is a static lint
# of the module's main.tf to defend against the regression class
# where a future edit accidentally widens the bucket policy or
# disables a public-access-block flag.

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
MODULE_DIR="$FIXTURE_DIR/../.."

main_tf="$MODULE_DIR/main.tf"
variables_tf="$MODULE_DIR/variables.tf"

if [ ! -f "$main_tf" ]; then
    echo "FAIL: $main_tf not found" >&2
    exit 2
fi

if [ ! -f "$variables_tf" ]; then
    echo "FAIL: $variables_tf not found" >&2
    exit 2
fi

failures=0

check() {
    local description="$1"
    local condition_result="$2"
    if [ "$condition_result" = "0" ]; then
        echo "PASS: $description"
    else
        echo "FAIL: $description" >&2
        failures=$((failures + 1))
    fi
}

# 1. Bucket policy actions: ONLY PutObject + GetObject.
grep -q '"s3:PutObject"' "$main_tf" && pp=0 || pp=1
check "bucket policy grants s3:PutObject" "$pp"

grep -q '"s3:GetObject"' "$main_tf" && gp=0 || gp=1
check "bucket policy grants s3:GetObject" "$gp"

# Reject any other s3:Action verb in the bucket policy. We use a tight
# allowlist instead of a denylist so future S3 actions are caught by
# default. The policy block is the `aws_s3_bucket_policy` resource;
# scan its action list.
disallowed=$(grep -oE '"s3:[A-Za-z]+"' "$main_tf" | sort -u | grep -vE '^"(s3:PutObject|s3:GetObject)"$' || true)
if [ -z "$disallowed" ]; then
    check "bucket policy has no actions beyond PutObject + GetObject" 0
else
    echo "FAIL: bucket policy contains disallowed s3 actions: $disallowed" >&2
    failures=$((failures + 1))
fi

# 2. Principal scoping: must be var.agent_task_role_arn, not a wildcard.
grep -q 'AWS = var.agent_task_role_arn' "$main_tf" && pa=0 || pa=1
check "bucket policy Principal scoped to var.agent_task_role_arn" "$pa"

# Defensive: no Principal = "*" anywhere in the file.
if grep -qE 'Principal[[:space:]]*=[[:space:]]*"\*"' "$main_tf"; then
    echo "FAIL: bucket policy contains wildcard Principal" >&2
    failures=$((failures + 1))
else
    check "no wildcard Principal in bucket policy" 0
fi

# 3. Public access block: all four flags must be true.
for flag in block_public_acls block_public_policy ignore_public_acls restrict_public_buckets; do
    if grep -qE "${flag}[[:space:]]+= true" "$main_tf"; then
        check "public access block: $flag = true" 0
    else
        echo "FAIL: public access block: $flag is not set to true" >&2
        failures=$((failures + 1))
    fi
done

# 4. Versioning enabled, SSE configured.
grep -qE 'status[[:space:]]+= "Enabled"' "$main_tf" && ve=0 || ve=1
check "versioning enabled" "$ve"

grep -q 'sse_algorithm = "AES256"' "$main_tf" && se=0 || se=1
check "SSE-S3 (AES256) configured" "$se"

# 5. Lifecycle: GLACIER_IR transition at 30 days.
if grep -qE 'storage_class[[:space:]]+= "GLACIER_IR"' "$main_tf"; then
    check "lifecycle transition uses GLACIER_IR" 0
else
    echo "FAIL: lifecycle transition does not reference GLACIER_IR" >&2
    failures=$((failures + 1))
fi

# Reject GLACIER (deep archive) and DEEP_ARCHIVE -- accidental
# substitution would change retrieval latency dramatically.
if grep -qE 'storage_class[[:space:]]+= "GLACIER"[^_]' "$main_tf"; then
    echo "FAIL: lifecycle uses GLACIER (deep archive) instead of GLACIER_IR" >&2
    failures=$((failures + 1))
else
    check "lifecycle does not use GLACIER (deep archive)" 0
fi

if grep -qE 'storage_class[[:space:]]+= "DEEP_ARCHIVE"' "$main_tf"; then
    echo "FAIL: lifecycle uses DEEP_ARCHIVE instead of GLACIER_IR" >&2
    failures=$((failures + 1))
else
    check "lifecycle does not use DEEP_ARCHIVE" 0
fi

# Verify the 30-day transition floor is preserved. A future edit that
# bumps this to 7d (or drops it to 90d) would change the diagnoser's
# read-window cost characteristics; pin the value.
if grep -qE 'days[[:space:]]+= 30' "$main_tf"; then
    check "lifecycle transition pinned at 30 days" 0
else
    echo "FAIL: lifecycle transition is not pinned at 30 days" >&2
    failures=$((failures + 1))
fi

# 6. session_retention_days variable: default 365, lower bound > 30.
if grep -qE 'default[[:space:]]+= 365' "$variables_tf"; then
    check "session_retention_days default is 365" 0
else
    echo "FAIL: session_retention_days default is not 365" >&2
    failures=$((failures + 1))
fi

if grep -qE 'session_retention_days[[:space:]]+>=[[:space:]]+31' "$variables_tf"; then
    check "session_retention_days lower-bound validation enforces >= 31" 0
else
    echo "FAIL: session_retention_days lower bound is not enforced (must be >= 31)" >&2
    failures=$((failures + 1))
fi

# 7. ASCII-only descriptions (the AWS provider rejects non-ASCII bytes
# in description fields per #3321 / #3923). The repo-level check
# (scripts/check-no-nonascii-tf-descriptions.sh) is the source of
# truth -- this module-local check is a fast smoke test so a developer
# editing the module catches a stray em-dash before pushing.
if LC_ALL=C grep -P '[^\x00-\x7F]' "$main_tf" "$variables_tf" "$MODULE_DIR/outputs.tf" >/dev/null 2>&1; then
    echo "FAIL: module contains non-ASCII bytes" >&2
    failures=$((failures + 1))
else
    check "module is ASCII-only" 0
fi

if [ "$failures" -gt 0 ]; then
    echo "" >&2
    echo "$failures policy-shape check(s) failed." >&2
    exit 1
fi

echo ""
echo "All policy-shape checks passed."
