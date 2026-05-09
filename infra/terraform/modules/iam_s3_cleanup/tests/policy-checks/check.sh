#!/usr/bin/env bash
set -euo pipefail

# Policy-shape regression test for the iam_s3_cleanup module (#4440).
#
# Asserts the invariants from #4440's scope exclusions that are easy to
# break with a stray string-replace and hard to spot in code review:
#
#   1. The module is dev-only — `environment` variable validation must
#      reject any value other than "dev".
#   2. The trust policy must use `Service: ecs-tasks.amazonaws.com`
#      only — no IAM users, no EC2, no cross-account principals.
#   3. The S3 resource ARNs must reference only `ca/*/*/raw/*` — never
#      the bucket root, `derived/`, `processed/`, `transcripts/`,
#      `staging/`, or `spotcheck/`.
#   4. `s3:PutObject` must NOT appear — cleanup scripts should never
#      write to the raw prefix.
#   5. `s3:ListBucket` must be guarded by an `s3:prefix` condition that
#      restricts list visibility to `ca/*/*/raw/*` subtrees.
#
# This script runs against the rendered HCL — no AWS credentials are
# needed, no actual roles are created. Mirror of
# dispatcher-v3-iam/tests/policy-checks/check.sh.

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

# ── Check 1: environment validation rejects non-dev values ─────────────────
#
# The validation block must contain `contains(["dev"], var.environment)` —
# any future edit that adds "staging" or "production" to the allowed list
# without explicit operator review is a regression per #4440 scope
# exclusions ("Does NOT touch production. Production raw-prefix cleanup
# is human-only").
if grep -q 'contains(\["dev"\], var\.environment)' "$variables_tf"; then
    check "environment validation pins to dev only" 0
else
    check "environment validation pins to dev only" 1
fi

# ── Check 2: trust policy is ecs-tasks service principal only ──────────────
#
# `Principal = { Service = "ecs-tasks.amazonaws.com" }` must be the ONLY
# Principal in the trust policy. No `Principal = { AWS = ... }` (which
# would indicate IAM-user / cross-account assume), no `Principal =
# { Service = "ec2.amazonaws.com" }` (which would indicate EC2 instance-
# profile assume).
if grep -q 'Principal = { Service = "ecs-tasks.amazonaws.com" }' "$main_tf"; then
    check "trust policy uses ecs-tasks service principal" 0
else
    check "trust policy uses ecs-tasks service principal" 1
fi

if grep -q 'Principal = { AWS' "$main_tf"; then
    check "trust policy has no AWS-account principal (cross-account guard)" 1
else
    check "trust policy has no AWS-account principal (cross-account guard)" 0
fi

if grep -q 'Principal = { Service = "ec2\.amazonaws\.com" }' "$main_tf"; then
    check "trust policy has no EC2 service principal" 1
else
    check "trust policy has no EC2 service principal" 0
fi

# ── Check 3: S3 resource ARNs reference ca/*/*/raw/* only ──────────────────
#
# The Resource list must contain `ca/*/*/raw/*` and MUST NOT contain
# any other prefix. The grep below extracts every `${var.document_archive_bucket_arn}/...`
# Resource entry and checks each one.
forbidden_prefixes=(
    "/derived/"
    "/processed/"
    "/transcripts/"
    "/staging/"
    "/spotcheck/"
)
for prefix in "${forbidden_prefixes[@]}"; do
    if grep -q "document_archive_bucket_arn}${prefix}" "$main_tf"; then
        check "Resource ARN does not reference ${prefix} prefix" 1
    else
        check "Resource ARN does not reference ${prefix} prefix" 0
    fi
done

# Bucket root is also forbidden — scope must always include a path
# segment after the bucket ARN.
if grep -qE '"\$\{var\.document_archive_bucket_arn\}/?"' "$main_tf"; then
    check "Resource ARN does not reference the bucket root" 1
else
    check "Resource ARN does not reference the bucket root" 0
fi

# Positive presence check: ca/*/*/raw/* must appear at least once.
if grep -q 'ca/\*/\*/raw/\*' "$main_tf"; then
    check "Resource ARN includes ca/*/*/raw/* prefix" 0
else
    check "Resource ARN includes ca/*/*/raw/* prefix" 1
fi

# ── Check 4: s3:PutObject must NOT appear ──────────────────────────────────
#
# Cleanup scripts should never write to the raw prefix. PutObject is
# explicitly excluded per #4440 implementation notes.
if grep -q '"s3:PutObject"' "$main_tf"; then
    check "policy does NOT grant s3:PutObject" 1
else
    check "policy does NOT grant s3:PutObject" 0
fi

# ── Check 5: ListBucket is prefix-conditioned ──────────────────────────────
#
# `s3:ListBucket` against the bucket ARN without an `s3:prefix` condition
# would let the role enumerate every key in the bucket. The condition
# must restrict prefix to `ca/*/*/raw/*`.
if grep -q '"s3:ListBucket"' "$main_tf"; then
    if grep -q '"s3:prefix"' "$main_tf"; then
        check "s3:ListBucket is gated by s3:prefix condition" 0
    else
        check "s3:ListBucket is gated by s3:prefix condition" 1
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
if [ "$failures" -gt 0 ]; then
    echo ""
    echo "FAIL: ${failures} policy-shape regression check(s) failed for iam_s3_cleanup" >&2
    exit 1
fi

echo ""
echo "PASS: all iam_s3_cleanup policy-shape regression checks"
