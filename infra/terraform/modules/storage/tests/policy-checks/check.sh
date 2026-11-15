#!/usr/bin/env bash
set -euo pipefail

# Policy-shape regression test for the storage (document archive) module
# and for every IAM policy under infra/terraform/ (#5013).
#
# Raw objects are the irreplaceable source of truth: no raw may be deleted
# unless a live byte-identical copy exists (architecture-spec §3.3.3). The
# bucket-level backstop is the archive's bucket policy. This script asserts:
#
#   1. The archive has an aws_s3_bucket_policy.
#   2. Its policy has an explicit Deny of s3:DeleteObject and
#      s3:DeleteObjectVersion on the raw prefixes (`*/raw/*`) and on
#      `directories/*`, for every principal (`*`).
#   3. The only exception is an exact ARN match (ArnNotEquals on
#      aws:PrincipalArn): no ArnNotLike / StringNotLike wildcard that could
#      widen the exception to a family of roles.
#   4. No lifecycle rule expires objects outside the throwaway prefixes
#      (spotcheck/, tmp/, migrations/). A lifecycle expiration deletes
#      without a DeleteObject call, so the bucket policy cannot stop it.
#   5. No IAM policy anywhere under infra/terraform/ grants an S3 wildcard
#      action (`s3:*`, `s3:Delete*`, ...), and only the storage module (in
#      its Deny) names s3:DeleteObjectVersion.
#
# Static analysis of the HCL: no AWS credentials needed.

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
MODULE_DIR="$(cd "$FIXTURE_DIR/../.." && pwd)"
TERRAFORM_DIR="$(cd "${TERRAFORM_DIR:-$MODULE_DIR/../..}" && pwd)"

main_tf="$MODULE_DIR/main.tf"

if [ ! -f "$main_tf" ]; then
    echo "FAIL: $main_tf not found" >&2
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

# Code lines only: HCL comments may name the forbidden strings as context.
code_only() {
    grep -vE '^[[:space:]]*(#|//)' "$1" || true
}

main_code=$(code_only "$main_tf")

# ── Check 1: the archive has a bucket policy ───────────────────────────────
if grep -q 'resource "aws_s3_bucket_policy" "document_archive"' <<< "$main_code"; then
    check "document archive has an aws_s3_bucket_policy" 0
else
    check "document archive has an aws_s3_bucket_policy" 1
fi

# ── Check 2: the Deny covers both delete actions on the raw prefixes ───────
deny_block=$(awk '
    /sid *= *"DenyRawDeleteExceptBreakGlass"/ {capture=1}
    capture {print}
    capture && /^  }$/ {capture=0}
' <<< "$main_code")

if [ -z "$deny_block" ]; then
    check "DenyRawDeleteExceptBreakGlass statement exists" 1
else
    check "DenyRawDeleteExceptBreakGlass statement exists" 0
    if grep -q 'effect *= *"Deny"' <<< "$deny_block"; then
        check "raw-delete statement is a Deny" 0
    else
        check "raw-delete statement is a Deny" 1
    fi
    for action in '"s3:DeleteObject"' '"s3:DeleteObjectVersion"'; do
        if grep -q "$action" <<< "$deny_block"; then
            check "raw-delete Deny covers ${action}" 0
        else
            check "raw-delete Deny covers ${action}" 1
        fi
    done
    if grep -q 'identifiers *= *\["\*"\]' <<< "$deny_block"; then
        check "raw-delete Deny applies to every principal" 0
    else
        check "raw-delete Deny applies to every principal" 1
    fi
fi

for resource in '/\*/raw/\*"' '/directories/\*"'; do
    if grep -qE "document_archive\.arn\}${resource}" <<< "$main_code"; then
        check "raw resources include ${resource}" 0
    else
        check "raw resources include ${resource}" 1
    fi
done

# ── Check 3: the exception is an exact ARN, never a wildcard match ─────────
if grep -qE 'test *= *"(ArnNotLike|StringNotLike|StringNotEquals)"' <<< "$main_code"; then
    check "policy exceptions use exact ArnNotEquals matches" 1
else
    check "policy exceptions use exact ArnNotEquals matches" 0
fi
if grep -qE 'test *= *"ArnNotEquals"' <<< "$deny_block"; then
    check "raw-delete exception is ArnNotEquals on a named principal" 0
else
    check "raw-delete exception is ArnNotEquals on a named principal" 1
fi

# ── Check 4: lifecycle expirations only on throwaway prefixes ──────────────
#
# Walk each `rule {` block of the lifecycle configuration. A rule that
# expires anything (expiration, noncurrent_version_expiration) must filter
# on one of the allowed prefixes; `filter {}` covers the whole bucket,
# raws included.
allowed_expiry_prefixes="spotcheck/ tmp/ migrations/"
bad_rules=$(awk -v allowed="$allowed_expiry_prefixes" '
    BEGIN { n = split(allowed, list, " "); for (i = 1; i <= n; i++) ok[list[i]] = 1 }
    /resource "aws_s3_bucket_lifecycle_configuration"/ {inlc=1}
    inlc && /^}$/ {inlc=0}
    inlc && /^  rule \{/ {inrule=1; id=""; prefix=""; expires=0}
    inrule && /id *=/ {gsub(/.*= *"|".*/, ""); id=$0}
    inrule && /prefix *=/ {gsub(/.*= *"|".*/, ""); prefix=$0}
    inrule && /(^|[^_])expiration *\{|noncurrent_version_expiration *\{/ {expires=1}
    inrule && /^  }$/ {
        inrule=0
        if (expires && !(prefix in ok)) print id " (prefix=\"" prefix "\")"
    }
' <<< "$main_code")
if [ -z "$bad_rules" ]; then
    check "no lifecycle rule expires objects outside spotcheck/, tmp/, migrations/" 0
else
    echo "  lifecycle rule(s) that expire raw-bearing prefixes: $bad_rules" >&2
    check "no lifecycle rule expires objects outside spotcheck/, tmp/, migrations/" 1
fi

# ── Check 5: no S3 wildcard grant anywhere under infra/terraform/ ──────────
wildcard_hits=""
version_delete_hits=""
while IFS= read -r tf; do
    code=$(code_only "$tf")
    if grep -qE '"s3:[A-Za-z]*\*' <<< "$code"; then
        wildcard_hits="$wildcard_hits $tf"
    fi
    case "$tf" in
        "$MODULE_DIR"/*) ;;
        *)
            if grep -q '"s3:DeleteObjectVersion"' <<< "$code"; then
                version_delete_hits="$version_delete_hits $tf"
            fi
            ;;
    esac
done < <(find "$TERRAFORM_DIR" -name '*.tf' -not -path '*/.terraform/*' | sort)

if [ -z "$wildcard_hits" ]; then
    check "no IAM policy under infra/terraform/ grants an s3 wildcard action" 0
else
    echo "  s3 wildcard action in:$wildcard_hits" >&2
    check "no IAM policy under infra/terraform/ grants an s3 wildcard action" 1
fi
if [ -z "$version_delete_hits" ]; then
    check "no IAM policy outside the storage module names s3:DeleteObjectVersion" 0
else
    echo "  s3:DeleteObjectVersion in:$version_delete_hits" >&2
    check "no IAM policy outside the storage module names s3:DeleteObjectVersion" 1
fi

# ── Summary ────────────────────────────────────────────────────────────────
if [ "$failures" -gt 0 ]; then
    echo ""
    echo "FAIL: ${failures} raw-archive policy check(s) failed" >&2
    echo "" >&2
    echo "  Fix: keep raws undeletable (architecture-spec §3.3.3, #5013):" >&2
    echo "" >&2
    echo "    # grant S3 actions by name on the buckets/prefixes the code uses" >&2
    echo "    Action   = [\"s3:GetObject\", \"s3:PutObject\"]" >&2
    echo "    Resource = \"\${var.document_archive_bucket_arn}/spotcheck/*\"" >&2
    echo "" >&2
    echo "  and keep the storage module's DenyRawDeleteExceptBreakGlass" >&2
    echo "  statement and lifecycle expirations on spotcheck/, tmp/ or" >&2
    echo "  migrations/ only." >&2
    exit 1
fi

echo ""
echo "PASS: all raw-archive policy checks"
