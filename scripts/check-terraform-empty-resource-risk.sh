#!/usr/bin/env bash
# check-terraform-empty-resource-risk.sh — Detect Terraform resources with
# compact([var.*]) Resource lists that lack a count or for_each guard.
#
# An IAM policy resource with:
#   Resource = compact([var.some_arn, var.other_arn])
# and no top-level `count = ...` or `for_each = ...` will silently create an
# IAM policy with an empty Resource list when all input variables are "".
# This is the #2739 shape.  The fix (#2740) is to add a count guard like:
#   count = length(local.compacted_arns) > 0 ? 1 : 0
#
# Usage:
#   scripts/check-terraform-empty-resource-risk.sh            # scan infra/
#   scripts/check-terraform-empty-resource-risk.sh --list     # print scanned
#                                                               files, exit 0
#
# Exit codes:
#   0 — No violations found (or --list mode).
#   1 — One or more unallowlisted violations found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ALLOWLIST="$REPO_ROOT/scripts/check-terraform-empty-resource-allowlist.txt"

PYTHON="${PYTHON:-python3}"

# ─── Directories to skip ────────────────────────────────────────────────────
EXCLUDE_DIRS=(
    ".git"
    ".terraform"
    ".venv"
    "node_modules"
    "__pycache__"
)

# ─── Parse arguments ─────────────────────────────────────────────────────────
LIST_MODE=false
if [[ "${1:-}" == "--list" ]]; then
    LIST_MODE=true
fi

# ─── Collect .tf files to scan ───────────────────────────────────────────────
# Scan every main.tf under infra/terraform/modules/**/ and
# infra/terraform/environments/**/
TF_FILES=()

while IFS= read -r -d '' f; do
    # Check against exclusion list
    skip=false
    for excl_dir in "${EXCLUDE_DIRS[@]}"; do
        if [[ "$f" == *"/$excl_dir/"* ]]; then
            skip=true
            break
        fi
    done
    if ! "$skip"; then
        TF_FILES+=("$f")
    fi
done < <(find "$REPO_ROOT/infra/terraform" -name "main.tf" -print0 2>/dev/null)

if [[ "$LIST_MODE" == true ]]; then
    for f in "${TF_FILES[@]}"; do
        echo "$f"
    done
    exit 0
fi

# ─── Run the check ───────────────────────────────────────────────────────────
violations=0

for f in "${TF_FILES[@]}"; do
    # python3 exits 0 (clean) or 1 (violations found)
    if ! output=$("$PYTHON" "$REPO_ROOT/scripts/check_tf_empty_resource.py" "$f" "$ALLOWLIST" 2>&1); then
        echo "$output"
        violations=$((violations + 1))
    elif [[ -n "$output" ]]; then
        echo "$output"
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "ERROR: Found $violations file(s) with IAM resources that use"
    echo "       compact([var.*]) for a Resource list without a count or"
    echo "       for_each guard.  When all input vars are \"\", the policy"
    echo "       will be created with an empty Resource list — a silent"
    echo "       misconfiguration (see issue #2739)."
    echo ""
    echo "  Fix: add a count or for_each guard, e.g.:"
    echo "    locals {"
    echo "      compacted_arns = compact([var.a_arn, var.b_arn])"
    echo "    }"
    echo "    resource \"aws_iam_role_policy\" \"example\" {"
    echo "      count = length(local.compacted_arns) > 0 ? 1 : 0"
    echo "      ..."
    echo "      Resource = local.compacted_arns"
    echo "    }"
    echo ""
    echo "  To add a temporary waiver while a fix is pending, add a line to:"
    echo "    scripts/check-terraform-empty-resource-allowlist.txt"
    echo "  Format: <path>:<resource_type>:<resource_name>  # issue #NNNN"
    echo ""
    echo "  See issue #2739 for background and issue #2740 for the fix pattern."
    exit 1
fi

echo "All clean — no unguarded compact([var.*]) Resource lists found."
exit 0
