#!/usr/bin/env bash
set -euo pipefail

# Policy-shape regression test for the iam_s3_raw_audit module
# (#4440, repurposed read-only by #4993).
#
# Asserts invariants that are easy to break with a stray string-replace
# and hard to spot in code review:
#
#   1. The module is dev-only — `environment` variable validation must
#      reject any value other than "dev".
#   2. The trust policy must use `Service: ecs-tasks.amazonaws.com`
#      only — no IAM users, no EC2, no cross-account principals.
#   3. The S3 resource ARNs must reference only `ca/*/*/raw/*` — never
#      the bucket root, `derived/`, `processed/`, `transcripts/`,
#      `staging/`, or `spotcheck/`.
#   4. The role is read-only: no `s3:Delete*` (in particular
#      `s3:DeleteObject` and `s3:DeleteObjectVersion`, #4993), no
#      `s3:Put*`, no wildcard action. No raw is deleted without a live
#      byte-identical copy (architecture-spec §3.3.3), and only
#      `framework.raw_delete_guard` may delete.
#   5. The audit's version actions are present (`s3:ListBucketVersions`,
#      `s3:GetObjectVersion`), so `scripts/audit_raw_delete_markers.py`
#      can run on ECS.
#   6. `s3:ListBucket` / `s3:ListBucketVersions` must be guarded by an
#      `s3:prefix` condition that restricts listing to the `ca/` subtree.
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

# ── Check 4: the role is read-only ─────────────────────────────────────────
#
# No delete, no write, no wildcard. `s3:DeleteObjectVersion` destroys a
# data version permanently and `s3:DeleteObject` hides a raw behind a
# delete marker; neither may be held by an ECS role (#4993). The patterns
# match quoted action strings in the policy JSON, so prose comments that
# name the actions in backticks do not trip them.
forbidden_actions=(
    '"s3:DeleteObject"'
    '"s3:DeleteObjectVersion"'
    '"s3:Delete'
    '"s3:Put'
    '"s3:Abort'
    '"s3:Replicate'
    '"s3:Bypass'
    '"s3:\*"'
    '"s3:[A-Za-z]*\*"'
    '"\*"'
)
for pat in "${forbidden_actions[@]}"; do
    if grep -qE "$pat" "$main_tf"; then
        check "policy grants no action matching '${pat}'" 1
    else
        check "policy grants no action matching '${pat}'" 0
    fi
done

# ── Check 5: the audit's version actions are granted ───────────────────────
#
# Without these, scripts/audit_raw_delete_markers.py cannot run on ECS and
# falls back to operator credentials (#4993).
for action in '"s3:ListBucketVersions"' '"s3:GetObjectVersion"'; do
    if grep -q "$action" "$main_tf"; then
        check "policy grants ${action}" 0
    else
        check "policy grants ${action}" 1
    fi
done

# ── Check 6: listing is prefix-conditioned ─────────────────────────────────
#
# `s3:ListBucket` / `s3:ListBucketVersions` against the bucket ARN
# without an `s3:prefix` condition would let the role enumerate every key
# in the bucket. The condition must restrict prefix so the role can only
# list under the `ca/` subtree. This is widened from `ca/*/*/raw/*` to
# `ca/*` because the audit lists from `ca/` and filters keys client-side
# (the narrower form rejected `Prefix='ca/'` at apply time, see #2661).
# Object reads stay scoped to `ca/*/*/raw/*`.
if grep -qE '"s3:ListBucket(Versions)?"' "$main_tf"; then
    if grep -q '"s3:prefix"' "$main_tf"; then
        check "s3:ListBucket is gated by s3:prefix condition" 0
    else
        check "s3:ListBucket is gated by s3:prefix condition" 1
    fi

    # The s3:prefix list must include at least one ca/-anchored pattern
    # so the policy is gated to the ca/ subtree (no listing of
    # derived/, processed/, transcripts/, staging/, or spotcheck/).
    if grep -qE '"(ca|ca/|ca/\*)"' "$main_tf"; then
        check "s3:prefix list includes a ca/-anchored pattern" 0
    else
        check "s3:prefix list includes a ca/-anchored pattern" 1
    fi

    # The s3:prefix list must NOT include patterns that escape ca/ —
    # e.g. a bare "*", "" (empty allow-anything), "derived/*",
    # "processed/*", "transcripts/*", "staging/*", or "spotcheck/*".
    forbidden_prefix_patterns=(
        '"\*"'
        '"derived'
        '"processed'
        '"transcripts'
        '"staging'
        '"spotcheck'
    )
    for pat in "${forbidden_prefix_patterns[@]}"; do
        if grep -qE "$pat" "$main_tf"; then
            check "s3:prefix list does not escape ca/ via '${pat}'" 1
        else
            check "s3:prefix list does not escape ca/ via '${pat}'" 0
        fi
    done
fi

# ── Summary ────────────────────────────────────────────────────────────────
if [ "$failures" -gt 0 ]; then
    echo ""
    echo "FAIL: ${failures} policy-shape regression check(s) failed for iam_s3_raw_audit" >&2
    exit 1
fi

echo ""
echo "PASS: all iam_s3_raw_audit policy-shape regression checks"
