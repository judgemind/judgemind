#!/usr/bin/env bash
# check-ingestion-worker-task-def-fingerprint.sh — assert the running
# ingestion-worker service's task-def container_definitions match the
# terraform-rendered SSM parameter (modulo the per-deploy image tag).
#
# Background — #4044:
#
#   `aws_ecs_service.ingestion_worker` carries `lifecycle.ignore_changes =
#   [task_definition]`, so terraform-apply cannot roll the service forward
#   after registering a new task-def revision. When `terraform.yml dev-apply`
#   races against `deploy-scraper.yml`'s `deploy-ingestion-worker` job (both
#   triggered by the same merge commit, both touching ingestion-worker
#   inputs), deploy-scraper can win — read the *stale* SSM parameter, pin
#   the service to a pre-apply revision — leaving the running container
#   with the wrong env vars while terraform's intent lives in a strictly-
#   newer revision the service never picks up.
#
#   This script is the post-deploy gate that closes the silent-drift class:
#   compute a fingerprint of (a) the desired-state container_definitions
#   in the terraform-managed SSM parameter and (b) the *running* service's
#   primary deployment task-def, after stripping the per-deploy `image`
#   field. If they diverge, fail loudly with a recovery hint.
#
#   The script is idempotent and safe to run any time — the periodic
#   monitoring path uses the same exit codes as the post-deploy gate path.
#
# Usage:
#
#   check-ingestion-worker-task-def-fingerprint.sh \
#     [--cluster judgemind-dev] \
#     [--service judgemind-ingestion-worker-dev] \
#     [--ssm-parameter /judgemind/ingestion-worker/dev/container-definitions] \
#     [--container-name ingestion-worker]
#
#   All four args have sensible dev defaults.
#
# Exit codes:
#
#   0 — fingerprints match (no drift).
#   1 — fingerprints differ; a unified diff of the canonicalized
#       container_definitions is printed to stderr.
#   2 — usage error or AWS API failure.

set -euo pipefail

CLUSTER="judgemind-dev"
SERVICE="judgemind-ingestion-worker-dev"
SSM_PARAMETER="/judgemind/ingestion-worker/dev/container-definitions"
CONTAINER_NAME="ingestion-worker"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cluster)
            CLUSTER="$2"
            shift 2
            ;;
        --service)
            SERVICE="$2"
            shift 2
            ;;
        --ssm-parameter)
            SSM_PARAMETER="$2"
            shift 2
            ;;
        --container-name)
            CONTAINER_NAME="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '1,/^set -euo pipefail$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

# ── Step 1: fetch the desired-state container_definitions from SSM ────────
#
# This is what terraform last rendered. If it's missing or invalid JSON,
# the apply path is broken upstream — fail with exit 2 so the caller can
# distinguish "drift" (exit 1) from "tooling broken" (exit 2).
SSM_RAW=$(aws ssm get-parameter \
    --name "$SSM_PARAMETER" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null) || {
    echo "ERROR: failed to read SSM parameter '$SSM_PARAMETER'" >&2
    exit 2
}

if ! echo "$SSM_RAW" | jq empty >/dev/null 2>&1; then
    echo "ERROR: SSM parameter '$SSM_PARAMETER' did not contain valid JSON" >&2
    exit 2
fi

# ── Step 2: fetch the running service's primary deployment task-def ───────
#
# describe-services returns a list of deployments; the PRIMARY deployment
# is the one currently rolling (or the steady one). We compare against
# that — not against the family's latest revision — because the goal is
# to detect drift on the *running* container, not on whatever terraform
# most recently registered.
RUNNING_TD_ARN=$(aws ecs describe-services \
    --cluster "$CLUSTER" \
    --services "$SERVICE" \
    --query "services[0].deployments[?status=='PRIMARY']|[0].taskDefinition" \
    --output text 2>/dev/null) || {
    echo "ERROR: failed to describe service '$SERVICE' on cluster '$CLUSTER'" >&2
    exit 2
}

if [[ -z "$RUNNING_TD_ARN" || "$RUNNING_TD_ARN" == "None" ]]; then
    echo "ERROR: could not resolve PRIMARY deployment task-def for service '$SERVICE'" >&2
    exit 2
fi

RUNNING_DEFS=$(aws ecs describe-task-definition \
    --task-definition "$RUNNING_TD_ARN" \
    --query 'taskDefinition.containerDefinitions' \
    --output json 2>/dev/null) || {
    echo "ERROR: failed to describe-task-definition '$RUNNING_TD_ARN'" >&2
    exit 2
}

# ── Step 3: canonicalize and fingerprint ──────────────────────────────────
#
# Both sides go through the same normalization:
#   - drop `.image` from each container (varies on every CI build)
#   - filter to ONLY the named container (the SSM JSON may contain
#     sidecars; the running task-def may pick them up too, but for the
#     drift signal we care about the primary container's env vars and
#     secrets)
#   - sort keys recursively for deterministic JSON
#
# We use jq's `--sort-keys` (top-level) and a recursive walk for nested
# objects. The `walk` builtin is the canonical way to do this in jq.
canonicalize() {
    jq --arg name "$CONTAINER_NAME" '
        # walk recursively sorts keys at every depth; combined with
        # del(.image) on the named container we get a stable hashable
        # representation that ignores per-deploy image-tag changes.
        def normalize:
            if type == "object" then
                with_entries(.value |= normalize)
                | to_entries
                | sort_by(.key)
                | from_entries
            elif type == "array" then
                map(normalize)
            else
                .
            end;
        map(select(.name == $name))
        | map(del(.image))
        | normalize
    '
}

DESIRED_CANON=$(echo "$SSM_RAW" | canonicalize)
RUNNING_CANON=$(echo "$RUNNING_DEFS" | canonicalize)

DESIRED_FP=$(echo "$DESIRED_CANON" | shasum -a 256 | awk '{print $1}')
RUNNING_FP=$(echo "$RUNNING_CANON" | shasum -a 256 | awk '{print $1}')

if [[ "$DESIRED_FP" == "$RUNNING_FP" ]]; then
    echo "OK: ingestion-worker task-def matches terraform SSM parameter (fingerprint $DESIRED_FP)"
    exit 0
fi

# ── Step 4: drift — print a recovery hint and a unified diff ──────────────

echo "DRIFT: running task-def ($RUNNING_TD_ARN) does not match terraform SSM parameter '$SSM_PARAMETER'" >&2
echo "  desired fingerprint: $DESIRED_FP" >&2
echo "  running fingerprint: $RUNNING_FP" >&2
echo "" >&2
echo "Likely cause: race between terraform.yml dev-apply and deploy-scraper.yml's deploy-ingestion-worker job (#4044)." >&2
echo "" >&2
echo "Recovery: re-register from the latest task-def revision and force a deploy:" >&2
echo "  LATEST=\$(aws ecs describe-task-definition --task-definition judgemind-ingestion-worker-dev --query 'taskDefinition.taskDefinitionArn' --output text)" >&2
echo "  aws ecs update-service --cluster $CLUSTER --service $SERVICE --task-definition \"\$LATEST\" --force-new-deployment" >&2
echo "" >&2
echo "Diff (canonicalized, image stripped):" >&2
diff -u <(echo "$DESIRED_CANON") <(echo "$RUNNING_CANON") >&2 || true

exit 1
