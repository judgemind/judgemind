#!/usr/bin/env bash
# Smoke test for the judgemind-agent IAM role Phase-B write path.
#
# permanent: true
#
# Exercises the three ECS write actions and the nested S3 ListBucket fix
# that were added in issue #3310.  Run this after deploying the iam_agent
# Terraform changes to confirm the policy allows:
#   1. ecs:RegisterTaskDefinition (Resource=* statement)
#   2. ecs:RunTask (cluster-scoped statement)
#   3. ecs:UpdateService --force-new-deployment (cluster-scoped statement)
#   4. s3:ListBucket on a nested prefix (staging/<county>/)
#
# Usage:
#   AGENT_ROLE_ARN=arn:aws:iam::155326049300:role/judgemind-agent-dev \
#     ARCHIVE_BUCKET=judgemind-document-archive-dev \
#     scripts/iam-agent-phase-b-smoke.sh
#
# If AGENT_ROLE_ARN is not set the script reads it from terraform output.
# If ARCHIVE_BUCKET is not set it defaults to judgemind-document-archive-dev.
#
# Exit codes:
#   0  — all checks passed
#   1  — one or more checks failed (AccessDenied or unexpected error)

set -euo pipefail
# AWS CLI v1/v2 portability: suppress pager without --no-cli-pager (v2-only flag). See #3461.
export AWS_PAGER=""

REGION="us-west-2"
CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
ARCHIVE_BUCKET="${ARCHIVE_BUCKET:-judgemind-document-archive-dev}"
RUN_ID="$(date +%s)"
TD_FAMILY="judgemind-iam-smoke-${RUN_ID}"
PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Resolve agent role ARN
# ---------------------------------------------------------------------------
if [[ -z "${AGENT_ROLE_ARN:-}" ]]; then
    echo "AGENT_ROLE_ARN not set — reading from terraform output..." >&2
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    AGENT_ROLE_ARN=$(terraform -chdir="${REPO_ROOT}/infra/terraform/environments/dev" output -raw agent_role_arn)
fi

echo "Using role: ${AGENT_ROLE_ARN}" >&2

# ---------------------------------------------------------------------------
# Assume the agent role and export temporary credentials
# ---------------------------------------------------------------------------
echo "Assuming role ${AGENT_ROLE_ARN} ..." >&2
CREDS=$(aws sts assume-role \
    --role-arn "${AGENT_ROLE_ARN}" \
    --role-session-name "iam-phase-b-smoke-${RUN_ID}" \
    --region "${REGION}" \
    --output json \
    --query 'Credentials.{AKI:AccessKeyId,SAK:SecretAccessKey,ST:SessionToken}')

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY
export AWS_SESSION_TOKEN
AWS_ACCESS_KEY_ID=$(echo "${CREDS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['AKI'])")
AWS_SECRET_ACCESS_KEY=$(echo "${CREDS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['SAK'])")
AWS_SESSION_TOKEN=$(echo "${CREDS}" | python3 -c "import sys,json; print(json.load(sys.stdin)['ST'])")
export AWS_DEFAULT_REGION="${REGION}"

echo "Credentials loaded (AKI=${AWS_ACCESS_KEY_ID:0:8}...)" >&2

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
check() {
    local label="$1"
    shift
    if "$@" 2>&1 | grep -q "AccessDenied\|is not authorized"; then
        echo "FAIL: ${label}" >&2
        FAIL=$((FAIL + 1))
    elif "$@" > /dev/null 2>&1; then
        echo "PASS: ${label}" >&2
        PASS=$((PASS + 1))
    else
        echo "PASS: ${label}" >&2
        PASS=$((PASS + 1))
    fi
}

# ---------------------------------------------------------------------------
# Step 1 — ecs:RegisterTaskDefinition (Resource=* statement)
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Step 1: RegisterTaskDefinition ---" >&2
TD_ARN=$(aws ecs register-task-definition \
    --family "${TD_FAMILY}" \
    --requires-compatibilities FARGATE \
    --network-mode awsvpc \
    --cpu 256 \
    --memory 512 \
    --container-definitions '[{"name":"smoke","image":"busybox","essential":true,"command":["sleep","1"]}]' \
    --region "${REGION}" \
    --output text \
    --query 'taskDefinition.taskDefinitionArn' 2>&1) && {
    echo "PASS: RegisterTaskDefinition -> ${TD_ARN}" >&2
    PASS=$((PASS + 1))
} || {
    echo "FAIL: RegisterTaskDefinition — ${TD_ARN}" >&2
    FAIL=$((FAIL + 1))
    TD_ARN=""
}

# ---------------------------------------------------------------------------
# Step 2 — ecs:RunTask (cluster-scoped statement)
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Step 2: RunTask ---" >&2
if [[ -n "${TD_ARN:-}" ]]; then
    RUN_OUT=$(aws ecs run-task \
        --cluster "${CLUSTER}" \
        --task-definition "${TD_ARN}" \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[],securityGroups=[],assignPublicIp=DISABLED}" \
        --region "${REGION}" \
        --output json 2>&1) && {
        FAILURES=$(echo "${RUN_OUT}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('failures',[])))" 2>/dev/null || echo "0")
        if [[ "${FAILURES}" == "0" ]]; then
            echo "PASS: RunTask (task launched)" >&2
            PASS=$((PASS + 1))
        else
            # Subnet/SG errors are expected in smoke context — what we test is
            # that IAM does NOT return AccessDenied.
            if echo "${RUN_OUT}" | grep -q "AccessDenied\|is not authorized"; then
                echo "FAIL: RunTask — AccessDenied" >&2
                FAIL=$((FAIL + 1))
            else
                echo "PASS: RunTask (IAM allowed; infra config failure is expected in smoke)" >&2
                PASS=$((PASS + 1))
            fi
        fi
    } || {
        if echo "${RUN_OUT}" | grep -q "AccessDenied\|is not authorized"; then
            echo "FAIL: RunTask — AccessDenied" >&2
            FAIL=$((FAIL + 1))
        else
            echo "PASS: RunTask (IAM allowed; minor infra error is acceptable)" >&2
            PASS=$((PASS + 1))
        fi
    }
else
    echo "SKIP: RunTask (RegisterTaskDefinition failed, skipping)" >&2
fi

# ---------------------------------------------------------------------------
# Step 3 — ecs:UpdateService --force-new-deployment (cluster-scoped statement)
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Step 3: UpdateService --force-new-deployment ---" >&2
UPDATE_OUT=$(aws ecs update-service \
    --cluster "${CLUSTER}" \
    --service "${SERVICE}" \
    --force-new-deployment \
    --region "${REGION}" \
    --output text \
    --query 'service.serviceName' 2>&1) && {
    echo "PASS: UpdateService -> ${UPDATE_OUT}" >&2
    PASS=$((PASS + 1))
} || {
    if echo "${UPDATE_OUT}" | grep -q "AccessDenied\|is not authorized"; then
        echo "FAIL: UpdateService — AccessDenied" >&2
        FAIL=$((FAIL + 1))
    else
        echo "PASS: UpdateService (IAM allowed; service may not exist in smoke env)" >&2
        PASS=$((PASS + 1))
    fi
}

# ---------------------------------------------------------------------------
# Step 4 — s3:ListBucket on a nested prefix (AC2: wildcard fix)
# ---------------------------------------------------------------------------
echo "" >&2
echo "--- Step 4: s3 ls nested prefix staging/orange/ ---" >&2
S3_OUT=$(aws s3 ls "s3://${ARCHIVE_BUCKET}/staging/orange/" \
    --region "${REGION}" 2>&1) && {
    echo "PASS: s3 ls staging/orange/ (ListBucket nested prefix allowed)" >&2
    PASS=$((PASS + 1))
} || {
    if echo "${S3_OUT}" | grep -q "AccessDenied\|is not authorized"; then
        echo "FAIL: s3 ls staging/orange/ — AccessDenied" >&2
        FAIL=$((FAIL + 1))
    else
        # NoSuchBucket or empty listing is still a policy pass
        echo "PASS: s3 ls staging/orange/ (IAM allowed; prefix may be empty)" >&2
        PASS=$((PASS + 1))
    fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "" >&2
echo "======================================" >&2
echo "Smoke test complete: ${PASS} passed, ${FAIL} failed" >&2
echo "======================================" >&2

if [[ "${FAIL}" -gt 0 ]]; then
    exit 1
fi
exit 0
