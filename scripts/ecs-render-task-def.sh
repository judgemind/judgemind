#!/usr/bin/env bash
# ecs-render-task-def.sh — Build and register an ECS task-definition revision
# that combines a new container image with the rest of the desired-state
# task-definition fields.
#
# Background — #3765 / parent #2840:
#
#   The legacy `.github/actions/ecs-deploy/action.yml` always reads the
#   "current" task-definition via `aws ecs describe-task-definition`,
#   swaps the image, and re-registers. That has a silent failure mode:
#   if a previous deploy-* workflow registered a revision with stale
#   content (e.g. a secret terraform has since removed), the next deploy
#   re-registers the same stale content with just a new image tag —
#   silently undoing terraform's removal forever. This is the
#   GITHUB_TOKEN drift documented in #2840.
#
#   This script supports an opt-in alternative: an SSM-parameter source
#   for the desired `container_definitions` JSON. Terraform writes the
#   rendered container_definitions to that parameter on every apply, so
#   reading from SSM gives deploy-* workflows access to terraform's
#   intent without reaching back into terraform state. The image URI is
#   substituted into the named container at deploy time. Other
#   task-definition fields (cpu, memory, network mode, exec/task role
#   ARNs, requires-compatibilities) still come from
#   `describe-task-definition` because they are stable across image
#   bumps and live on the task-definition itself, not the container.
#
# Usage:
#
#   ecs-render-task-def.sh \
#     --task-family judgemind-api-dev \
#     --container-name api \
#     --image-uri 111.dkr.ecr.us-west-2.amazonaws.com/judgemind/api:abcd123 \
#     [--desired-container-definitions-ssm-parameter /judgemind/api/dev/container-definitions]
#
# Outputs:
#
#   The newly-registered task-definition ARN is written to stdout (one line).
#   Callers running inside a GitHub Action are expected to capture the ARN
#   from stdout and append `arn=<ARN>` to `$GITHUB_OUTPUT` themselves — that
#   contract keeps the script reusable from non-Actions contexts.
#
# Exit codes:
#
#   0 — Registered successfully.
#   1 — Argument validation error or AWS call failed.
#   2 — SSM parameter contained invalid JSON or did not include the named
#       container.

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────

TASK_FAMILY=""
CONTAINER_NAME=""
IMAGE_URI=""
SSM_PARAMETER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-family)
            TASK_FAMILY="$2"
            shift 2
            ;;
        --container-name)
            CONTAINER_NAME="$2"
            shift 2
            ;;
        --image-uri)
            IMAGE_URI="$2"
            shift 2
            ;;
        --desired-container-definitions-ssm-parameter)
            SSM_PARAMETER="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '1,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$TASK_FAMILY" ]]; then
    echo "ERROR: --task-family is required" >&2
    exit 1
fi
if [[ -z "$CONTAINER_NAME" ]]; then
    echo "ERROR: --container-name is required" >&2
    exit 1
fi
if [[ -z "$IMAGE_URI" ]]; then
    echo "ERROR: --image-uri is required" >&2
    exit 1
fi

# ── Step 1: fetch the current task-definition for family-level metadata ──
#
# Even on the SSM-source path, we still pull cpu / memory / execution-role /
# network-mode / requires-compatibilities from describe-task-definition.
# Those fields are stable across image bumps and aren't part of the
# container_definitions JSON. The SSM source only replaces
# `container_definitions`.
CURRENT_JSON=$(aws ecs describe-task-definition \
    --task-definition "$TASK_FAMILY" \
    --query 'taskDefinition' \
    --output json)

# ── Step 2: compute the desired container_definitions ────────────────────

if [[ -n "$SSM_PARAMETER" ]]; then
    # SSM-source path (#3765): terraform is the single source of truth.
    SSM_JSON=$(aws ssm get-parameter \
        --name "$SSM_PARAMETER" \
        --query 'Parameter.Value' \
        --output text)

    # The SSM parameter stores a JSON-array string. Validate before use; bail
    # if it's malformed so a corrupt SSM write never produces a bad revision.
    if ! echo "$SSM_JSON" | jq empty >/dev/null 2>&1; then
        echo "ERROR: SSM parameter '$SSM_PARAMETER' did not contain valid JSON" >&2
        exit 2
    fi

    # Substitute the new image URI into the named container.
    CONTAINER_DEFS=$(echo "$SSM_JSON" | jq --arg image "$IMAGE_URI" --arg container "$CONTAINER_NAME" \
        'map(if .name == $container then .image = $image else . end)')

    # Verify the named container exists in the SSM JSON; otherwise the image
    # swap silently no-ops and we'd register a revision with the placeholder
    # image. Fail loudly instead.
    MATCHED=$(echo "$CONTAINER_DEFS" | jq --arg container "$CONTAINER_NAME" \
        '[.[] | select(.name == $container)] | length')
    if [[ "$MATCHED" -lt 1 ]]; then
        echo "ERROR: container '$CONTAINER_NAME' not found in SSM parameter '$SSM_PARAMETER'" >&2
        exit 2
    fi
else
    # Legacy path: take container_definitions from the running task-def.
    # Preserves the historical (potentially stale) content. Acceptable for
    # services that have not opted in to SSM-source mode yet.
    CONTAINER_DEFS=$(echo "$CURRENT_JSON" | jq --arg image "$IMAGE_URI" --arg container "$CONTAINER_NAME" \
        '.containerDefinitions | map(if .name == $container then .image = $image else . end)')
fi

# ── Step 3: resolve task role (with self-heal for legacy revisions) ──────
#
# Older revisions registered before the IAM role landed lack a taskRoleArn.
# Look up the role by Terraform's naming convention so the next deploy
# attaches it. (Same logic as the legacy inline action; preserved here so
# behaviour is unchanged on non-SSM consumers.)
TASK_ROLE=$(echo "$CURRENT_JSON" | jq -r '.taskRoleArn // empty')
if [[ -z "$TASK_ROLE" ]]; then
    ENV_SUFFIX="${TASK_FAMILY##*-}"
    SVC_PREFIX="${TASK_FAMILY%-*}"
    ROLE_NAME="${SVC_PREFIX}-task-${ENV_SUFFIX}"
    echo "Task role ARN missing from current revision — looking up role '$ROLE_NAME'..." >&2
    TASK_ROLE=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null || true)
    if [[ -n "$TASK_ROLE" && "$TASK_ROLE" != "None" ]]; then
        echo "Resolved task role: $TASK_ROLE" >&2
    else
        echo "WARNING: could not resolve task role '$ROLE_NAME' — proceeding without it." >&2
        TASK_ROLE=""
    fi
fi

# ── Step 4: register the new revision ────────────────────────────────────

REQUIRES_COMPAT=$(echo "$CURRENT_JSON" | jq -r '.requiresCompatibilities[]')
NETWORK_MODE=$(echo "$CURRENT_JSON" | jq -r '.networkMode')
CPU=$(echo "$CURRENT_JSON" | jq -r '.cpu')
MEMORY=$(echo "$CURRENT_JSON" | jq -r '.memory')
EXEC_ROLE=$(echo "$CURRENT_JSON" | jq -r '.executionRoleArn')

REGISTER_ARGS=(
    --family "$TASK_FAMILY"
    --network-mode "$NETWORK_MODE"
    --cpu "$CPU"
    --memory "$MEMORY"
    --execution-role-arn "$EXEC_ROLE"
    --container-definitions "$(echo "$CONTAINER_DEFS" | jq -c .)"
)

# `--requires-compatibilities` is a list; expand each value as a separate arg.
while IFS= read -r compat; do
    if [[ -n "$compat" ]]; then
        REGISTER_ARGS+=(--requires-compatibilities "$compat")
    fi
done <<< "$REQUIRES_COMPAT"

if [[ -n "$TASK_ROLE" ]]; then
    REGISTER_ARGS+=(--task-role-arn "$TASK_ROLE")
fi

NEW_TASK_DEF_ARN=$(aws ecs register-task-definition \
    "${REGISTER_ARGS[@]}" \
    --query 'taskDefinition.taskDefinitionArn' \
    --output text)

echo "$NEW_TASK_DEF_ARN"
