#!/usr/bin/env bash
# wait-for-rollout.sh — Poll an ECS service deployment until a specific
# deployment reaches a terminal state (COMPLETED or FAILED) with
# runningCount == desiredCount, or until the configured timeout elapses.
#
# Shared between two callers:
#   1. .github/actions/ecs-deploy/action.yml — waits for a newly registered
#      task-definition ARN to roll out (CI deploy path, see #2519).
#   2. scripts/ecs-redeploy.sh — waits for a newly triggered
#      --force-new-deployment deployment to roll out (operator manual
#      redeploy path, see #2523). Same task-def ARN as before, but a new
#      deployment ID.
#
# IMPORTANT — paths-filter contract (see #2592):
#   Any deploy workflow that adopts this script (directly or via the
#   ecs-deploy composite action) MUST add `scripts/wait-for-rollout.sh`
#   to its `on.push.paths:` filter. Otherwise a PR that changes only this
#   shared script will merge without triggering any deploy workflow — so
#   the fix is not exercised against a real rollout on its own PR, and
#   regressions surface on the next unrelated deploy. Current callers:
#     - .github/workflows/deploy-scraper.yml
#     - .github/workflows/deploy-api.yml
#     - .github/workflows/terraform.yml (dev-apply force-deploy, #4044)
#
# Identifying the deployment:
#   - CI path knows the new task-def ARN up front.
#   - Operator redeploy path does not change the task-def ARN; only a new
#     deployment ID is created. So we support selecting the deployment by
#     either ARN or deployment ID — exactly one must be provided.
#
# Required environment variables:
#   ECS_CLUSTER          — ECS cluster name
#   ECS_SERVICE          — ECS service name
#
# One of:
#   NEW_TASK_DEF_ARN     — Task definition ARN to select the deployment by
#   NEW_DEPLOYMENT_ID    — Deployment ID to select the deployment by
#
# Optional environment variables:
#   ROLLOUT_TIMEOUT_SECS   — overall timeout in seconds (default: 900, sized
#                            for CI deploy jobs; scripts/ecs-redeploy.sh
#                            passes 480 so an agent's Bash call stays under
#                            the tool's 600s cap, #4835)
#   ROLLOUT_POLL_INTERVAL  — polling interval in seconds (default: 10)
#   AWS_CLI                — binary name for aws CLI (default: aws; used for
#                            mocking in tests)
#   ROLLOUT_SUPERSEDE_GRACE_POLLS — how many consecutive polls our deployment
#                            may be missing, while another PRIMARY exists,
#                            before we call it superseded (default: 3). Only
#                            used when we have no stronger signal (see below).
#
# Superseded deployments (#5073):
#   Another actor can replace our deployment before it finishes. The common
#   case is terraform.yml dev-apply force-deploying the family's newest
#   revision (#4044) while deploy-scraper.yml is still waiting on the
#   revision it registered. ECS then drops our deployment from
#   services[0].deployments and a newer PRIMARY takes over. Before #5073
#   this script read the missing deployment as "not yet visible" and polled
#   until the timeout, so the deploy job went red and the post-deploy drift
#   check was skipped.
#
#   Our deployment counts as superseded when it is missing, a PRIMARY
#   deployment exists, and one of these holds:
#     - we saw our deployment earlier in this call;
#     - the PRIMARY's task-def is a higher revision of our family; or
#     - it has been missing for ROLLOUT_SUPERSEDE_GRACE_POLLS polls in a row.
#   Then we compare images, container by container. Image refs that are
#   equal match. Otherwise both refs are resolved to ECR digests (terraform
#   registers `:latest`, deploy-scraper registers `:<sha7>`; they match when
#   both tags point at the same pushed image):
#     - Same image: print "superseded by ..." and wait on the PRIMARY instead.
#       Its completion is our success (exit 0).
#     - Different image: exit 3 with the reason. The service is not running
#       this run's image.
#   Re-running after exit 124 repeats the same detection, so the wait stays
#   resumable.
#
# Exit codes:
#   0 — Target deployment reached rolloutState=COMPLETED with runningCount==
#       desiredCount. This includes the scale-to-zero case (desired=0,
#       running=0): when a service is kept at desiredCount=0 (e.g. dev
#       ingestion worker runs only on-demand), ECS marks the new deployment
#       COMPLETED immediately with no tasks to launch, and we treat that as
#       a successful rollout (#2585).
#       Also exit 0 when our deployment was superseded by a deployment that
#       carries the same image and that deployment completed (#5073).
#   1 — rolloutState=FAILED, or the env-var contract was violated.
#   3 — Our deployment was superseded by a deployment that does NOT carry
#       this run's image (or whose image could not be matched), so the
#       service is not running what this run deployed (#5073).
#   124 — The timeout elapsed while the deployment was still rolling out
#       (#4835). Not a failure verdict. Re-running with the same env vars
#       resumes the wait on the same deployment; it never redeploys.
#       CI callers treat any non-zero exit as a failed deploy step.
#   125 — aws ecs describe-services failed (or hung past its per-call
#       limit 3 times in a row), so the rollout state is unknown (same
#       convention as scripts/ecs-run-task.sh, #4791).
#
# Each describe-services call runs under a per-call timeout sized to the
# time left in ROLLOUT_TIMEOUT_SECS, and calls slower than 30s are logged as
# "slow call: ..." (#4850). This script sources scripts/_wait_budget_lib.sh
# and scripts/_ecs_exec_lib.sh, so both are part of the paths-filter
# contract above.

set -euo pipefail

# shellcheck source=scripts/_wait_budget_lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_wait_budget_lib.sh"

: "${ECS_CLUSTER:?ECS_CLUSTER is required}"
: "${ECS_SERVICE:?ECS_SERVICE is required}"

NEW_TASK_DEF_ARN="${NEW_TASK_DEF_ARN:-}"
NEW_DEPLOYMENT_ID="${NEW_DEPLOYMENT_ID:-}"

if [ -z "$NEW_TASK_DEF_ARN" ] && [ -z "$NEW_DEPLOYMENT_ID" ]; then
  echo "ERROR: one of NEW_TASK_DEF_ARN or NEW_DEPLOYMENT_ID is required." >&2
  exit 1
fi
if [ -n "$NEW_TASK_DEF_ARN" ] && [ -n "$NEW_DEPLOYMENT_ID" ]; then
  echo "ERROR: set only one of NEW_TASK_DEF_ARN or NEW_DEPLOYMENT_ID, not both." >&2
  exit 1
fi

ROLLOUT_TIMEOUT_SECS="${ROLLOUT_TIMEOUT_SECS:-900}"
ROLLOUT_POLL_INTERVAL="${ROLLOUT_POLL_INTERVAL:-10}"
AWS_CLI="${AWS_CLI:-aws}"
ROLLOUT_SUPERSEDE_GRACE_POLLS="${ROLLOUT_SUPERSEDE_GRACE_POLLS:-3}"

# Selection picks one deployment object. When several share a task-def ARN
# (a force-new-deployment of the same revision), prefer the PRIMARY one.
JQ_SELECT_BY_ARN='[.services[0].deployments[] | select(.taskDefinition == $sel)] | (map(select(.status == "PRIMARY")) + .) | .[0] // empty'
JQ_SELECT_BY_ID='[.services[0].deployments[] | select(.id == $sel)] | .[0] // empty'
JQ_PRIMARY='[.services[0].deployments[] | select(.status == "PRIMARY")] | .[0] // empty'

if [ -n "$NEW_TASK_DEF_ARN" ]; then
  SELECTOR_DESC="task definition $NEW_TASK_DEF_ARN"
  JQ_SELECT_FILTER="$JQ_SELECT_BY_ARN"
  JQ_SELECT_ARG="$NEW_TASK_DEF_ARN"
  # The task-def whose image this run deployed. In deployment-id mode we
  # learn it the first time we see our deployment.
  OUR_TASK_DEF="$NEW_TASK_DEF_ARN"
else
  SELECTOR_DESC="deployment $NEW_DEPLOYMENT_ID"
  JQ_SELECT_FILTER="$JQ_SELECT_BY_ID"
  JQ_SELECT_ARG="$NEW_DEPLOYMENT_ID"
  OUR_TASK_DEF=""
fi
ORIGINAL_SELECTOR_DESC="$SELECTOR_DESC"

# ── Superseded-deployment helpers (#5073) ──────────────────────────────────

# td_family / td_revision — split ".../task-definition/<family>:<rev>".
# td_revision prints nothing when the ARN has no numeric revision.
td_family() {
  local tail="${1##*/}"
  echo "${tail%:*}"
}
td_revision() {
  local tail="${1##*/}"
  case "$tail" in
    *:*)
      local rev="${tail##*:}"
      if [[ "$rev" =~ ^[0-9]+$ ]]; then
        echo "$rev"
      fi
      ;;
  esac
}

# fetch_task_def_images <arn> <out-var> — sets <out-var> to "name<TAB>image"
# lines for each container. Returns the aws call's exit status.
fetch_task_def_images() {
  local arn="$1" out_var="$2" json rc=0
  json=$(wait_budget_call "aws ecs describe-task-definition" \
        "$AWS_CLI" ecs describe-task-definition \
        --task-definition "$arn" \
        --output json) || rc=$?
  if [ "$rc" -ne 0 ]; then
    return "$rc"
  fi
  local lines
  lines=$(echo "$json" | jq -r '.taskDefinition.containerDefinitions[]? | "\(.name)\t\(.image)"') || return 1
  printf -v "$out_var" '%s' "$lines"
}

# resolve_image_digest <image-ref> <out-var> — sets <out-var> to the image's
# sha256 digest, or to "" when the ref cannot be resolved (not an ECR ref, or
# the tag no longer exists). Returns non-zero only when the aws call failed.
resolve_image_digest() {
  local ref="$1" out_var="$2"
  printf -v "$out_var" '%s' ""
  case "$ref" in
    *@sha256:*)
      printf -v "$out_var" '%s' "${ref##*@}"
      return 0
      ;;
  esac
  local ecr_re='^([0-9]+)\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/([^:@]+):([^:@/]+)$'
  if ! [[ "$ref" =~ $ecr_re ]]; then
    return 0
  fi
  local registry="${BASH_REMATCH[1]}" region="${BASH_REMATCH[2]}"
  local repo="${BASH_REMATCH[3]}" tag="${BASH_REMATCH[4]}"
  local json rc=0 err_file
  err_file=$(mktemp)
  json=$(wait_budget_call "aws ecr describe-images" \
        "$AWS_CLI" ecr describe-images \
        --registry-id "$registry" \
        --region "$region" \
        --repository-name "$repo" \
        --image-ids "imageTag=$tag" \
        --output json 2>"$err_file") || rc=$?
  if [ "$rc" -ne 0 ]; then
    # A deleted tag is an answer ("unresolvable"), not an unknown state.
    if grep -q "ImageNotFoundException" "$err_file"; then
      rm -f "$err_file"
      return 0
    fi
    cat "$err_file" >&2
    rm -f "$err_file"
    return "$rc"
  fi
  rm -f "$err_file"
  local digest
  digest=$(echo "$json" | jq -r '.imageDetails[0].imageDigest // ""') || digest=""
  printf -v "$out_var" '%s' "$digest"
}

# compare_task_def_images <ours> <theirs> — sets IMAGE_VERDICT to "match",
# "mismatch" or "retry" (an aws call was killed by its per-call timeout; try
# again next poll) and IMAGE_DETAIL to a one-line explanation. Exits 125
# when an aws call failed outright (state unknown).
compare_task_def_images() {
  local ours="$1" theirs="$2" rc=0
  IMAGE_VERDICT="mismatch"
  IMAGE_DETAIL=""
  if [ "$ours" = "$theirs" ]; then
    IMAGE_VERDICT="match"
    IMAGE_DETAIL="same task definition"
    return 0
  fi

  local our_lines="" their_lines=""
  fetch_task_def_images "$ours" our_lines || rc=$?
  if [ "$rc" -eq 0 ]; then
    fetch_task_def_images "$theirs" their_lines || rc=$?
  fi
  if [ "$rc" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
    IMAGE_VERDICT="retry"
    return 0
  elif [ "$rc" -ne 0 ]; then
    echo "ERROR: aws ecs describe-task-definition failed while checking the superseding deployment's image; state unknown."
    exit 125
  fi
  if [ -z "$our_lines" ]; then
    IMAGE_DETAIL="could not read the containers of $ours"
    return 0
  fi

  local name our_image their_image our_digest their_digest details=""
  while IFS=$'\t' read -r name our_image; do
    [ -n "$name" ] || continue
    their_image=$(printf '%s\n' "$their_lines" | awk -F'\t' -v n="$name" '$1 == n { print $2; exit }')
    if [ -z "$their_image" ]; then
      IMAGE_DETAIL="container $name is missing from $theirs"
      return 0
    fi
    if [ "$our_image" = "$their_image" ]; then
      details="${details}${details:+; }$name: $our_image"
      continue
    fi
    rc=0
    resolve_image_digest "$our_image" our_digest || rc=$?
    if [ "$rc" -eq 0 ]; then
      resolve_image_digest "$their_image" their_digest || rc=$?
    fi
    if [ "$rc" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
      IMAGE_VERDICT="retry"
      return 0
    elif [ "$rc" -ne 0 ]; then
      echo "ERROR: aws ecr describe-images failed while checking the superseding deployment's image; state unknown."
      exit 125
    fi
    if [ -z "$our_digest" ] || [ -z "$their_digest" ]; then
      IMAGE_DETAIL="container $name: could not resolve both images to digests (ours $our_image -> ${our_digest:-?}, theirs $their_image -> ${their_digest:-?})"
      return 0
    fi
    if [ "$our_digest" != "$their_digest" ]; then
      IMAGE_DETAIL="container $name: ours $our_image ($our_digest), theirs $their_image ($their_digest)"
      return 0
    fi
    details="${details}${details:+; }$name: $our_image = $their_image ($our_digest)"
  done <<< "$our_lines"
  IMAGE_VERDICT="match"
  IMAGE_DETAIL="$details"
}

echo "Waiting for $SELECTOR_DESC to roll out on ${ECS_CLUSTER}/${ECS_SERVICE} (timeout: ${ROLLOUT_TIMEOUT_SECS}s, poll every ${ROLLOUT_POLL_INTERVAL}s)..."

START_TS=$SECONDS
DEADLINE=$((START_TS + ROLLOUT_TIMEOUT_SECS))
wait_budget_init "$ROLLOUT_TIMEOUT_SECS" "$START_TS"
LAST_STATE=""
LAST_COUNTS=""
SERVICE_JSON='{}'
CONSECUTIVE_TIMEOUTS=0
MAX_CONSECUTIVE_TIMEOUTS=3
SEEN_OURS=0
MISSING_POLLS=0
FOLLOWED_DESC=""

while true; do
  NOW_TS=$SECONDS
  ELAPSED=$((NOW_TS - START_TS))

  DESCRIBE_RC=0
  DESCRIBE_OUT=$(wait_budget_call "aws ecs describe-services" \
        "$AWS_CLI" ecs describe-services \
        --cluster "$ECS_CLUSTER" \
        --services "$ECS_SERVICE" \
        --output json) || DESCRIBE_RC=$?
  if [ "$DESCRIBE_RC" -eq "$WAIT_CALL_TIMEOUT_RC" ]; then
    # A hung call is a failed poll, not a verdict (#4850).
    CONSECUTIVE_TIMEOUTS=$((CONSECUTIVE_TIMEOUTS + 1))
    if [ "$CONSECUTIVE_TIMEOUTS" -ge "$MAX_CONSECUTIVE_TIMEOUTS" ] && ! wait_budget_exhausted; then
      echo "ERROR: aws ecs describe-services hung ${CONSECUTIVE_TIMEOUTS} times in a row after ${ELAPSED}s; rollout state unknown."
      exit 125
    fi
    DEPLOYMENT=""
  elif [ "$DESCRIBE_RC" -ne 0 ]; then
    echo "ERROR: aws ecs describe-services failed after ${ELAPSED}s; rollout state unknown."
    exit 125
  else
    CONSECUTIVE_TIMEOUTS=0
    SERVICE_JSON="$DESCRIBE_OUT"
    DEPLOYMENT=$(echo "$SERVICE_JSON" | jq --arg sel "$JQ_SELECT_ARG" \
      "$JQ_SELECT_FILTER")
  fi

  # Our deployment is still listed but no longer PRIMARY: a newer deployment
  # replaced it and it is draining (#5073). Its counts can read
  # COMPLETED 0/0, which must not pass for a scale-to-zero success.
  DEMOTED_STATUS=""
  IMAGE_CHECK_PENDING=0
  if [ "$DESCRIBE_RC" -eq 0 ] && [ -n "$DEPLOYMENT" ]; then
    DEMOTED_STATUS=$(echo "$DEPLOYMENT" | jq -r 'if (.status // "PRIMARY") == "PRIMARY" then "" else .status end')
    if [ -n "$DEMOTED_STATUS" ] && [ -z "$(echo "$SERVICE_JSON" | jq -c "$JQ_PRIMARY")" ]; then
      DEMOTED_STATUS=""
    fi
    if [ -n "$DEMOTED_STATUS" ] && [ -z "$OUR_TASK_DEF" ]; then
      OUR_TASK_DEF=$(echo "$DEPLOYMENT" | jq -r '.taskDefinition // ""')
    fi
  fi

  if [ "$DESCRIBE_RC" -eq 0 ] && { [ -z "$DEPLOYMENT" ] || [ -n "$DEMOTED_STATUS" ]; }; then
    # Our deployment is missing or demoted. Either ECS has not listed it yet
    # (the first polls after update-service), or a newer deployment replaced
    # it (#5073).
    if [ -z "$DEPLOYMENT" ]; then
      MISSING_POLLS=$((MISSING_POLLS + 1))
    fi
    PRIMARY=$(echo "$SERVICE_JSON" | jq -c "$JQ_PRIMARY")
    if [ -n "$PRIMARY" ]; then
      PRIMARY_ID=$(echo "$PRIMARY" | jq -r '.id // ""')
      PRIMARY_TD=$(echo "$PRIMARY" | jq -r '.taskDefinition // ""')
      SUPERSEDE_WHY=""
      if [ -n "$DEMOTED_STATUS" ]; then
        SUPERSEDE_WHY="our deployment is now $DEMOTED_STATUS"
      elif [ "$SEEN_OURS" -eq 1 ]; then
        SUPERSEDE_WHY="our deployment was listed earlier and is now gone"
      elif [ -n "$OUR_TASK_DEF" ] \
          && [ "$(td_family "$PRIMARY_TD")" = "$(td_family "$OUR_TASK_DEF")" ] \
          && [ -n "$(td_revision "$PRIMARY_TD")" ] && [ -n "$(td_revision "$OUR_TASK_DEF")" ] \
          && [ "$(td_revision "$PRIMARY_TD")" -gt "$(td_revision "$OUR_TASK_DEF")" ]; then
        SUPERSEDE_WHY="the PRIMARY runs a newer revision of our family"
      elif [ "$MISSING_POLLS" -ge "$ROLLOUT_SUPERSEDE_GRACE_POLLS" ]; then
        SUPERSEDE_WHY="our deployment has been missing for ${MISSING_POLLS} polls"
      fi

      if [ -n "$SUPERSEDE_WHY" ]; then
        echo "[${ELAPSED}s] $SELECTOR_DESC was superseded by deployment $PRIMARY_ID ($PRIMARY_TD): $SUPERSEDE_WHY."
        if [ -z "$OUR_TASK_DEF" ]; then
          echo "ERROR: SUPERSEDED: $SELECTOR_DESC was replaced by deployment $PRIMARY_ID ($PRIMARY_TD) before this call saw it, so its image cannot be compared."
          echo "Check which image the service runs now: aws ecs describe-services --cluster $ECS_CLUSTER --services $ECS_SERVICE"
          exit 3
        fi
        compare_task_def_images "$OUR_TASK_DEF" "$PRIMARY_TD"
        case "$IMAGE_VERDICT" in
          match)
            echo "[${ELAPSED}s] The superseding deployment carries this run's image ($IMAGE_DETAIL). Waiting on it instead."
            FOLLOWED_DESC="$PRIMARY_ID ($PRIMARY_TD)"
            SELECTOR_DESC="deployment $PRIMARY_ID (superseded $ORIGINAL_SELECTOR_DESC)"
            JQ_SELECT_FILTER="$JQ_SELECT_BY_ID"
            JQ_SELECT_ARG="$PRIMARY_ID"
            OUR_TASK_DEF="$PRIMARY_TD"
            SEEN_OURS=1
            MISSING_POLLS=0
            LAST_COUNTS=""
            DEPLOYMENT=$(echo "$SERVICE_JSON" | jq --arg sel "$JQ_SELECT_ARG" "$JQ_SELECT_FILTER")
            ;;
          retry)
            echo "[${ELAPSED}s] Image check for the superseding deployment timed out; retrying next poll."
            # Never judge a demoted deployment's own counts.
            DEPLOYMENT=""
            IMAGE_CHECK_PENDING=1
            ;;
          *)
            echo "ERROR: SUPERSEDED: $SELECTOR_DESC was replaced by deployment $PRIMARY_ID ($PRIMARY_TD), which does not carry this run's image."
            echo "Reason: ${IMAGE_DETAIL:-image mismatch}"
            echo "The service is not running the image this run deployed. Re-run the deploy, or find out who deployed $PRIMARY_TD."
            exit 3
            ;;
        esac
      fi
    fi
  fi

  if [ "$DESCRIBE_RC" -ne 0 ]; then
    echo "[${ELAPSED}s] describe-services timed out, retrying..."
  elif [ "$IMAGE_CHECK_PENDING" -eq 1 ]; then
    : # already logged above
  elif [ -z "$DEPLOYMENT" ]; then
    # Our deployment has not yet been registered in the deployments list.
    # Give ECS a moment — retry.
    echo "[${ELAPSED}s] Deployment for $SELECTOR_DESC not yet visible, retrying..."
  else
    SEEN_OURS=1
    MISSING_POLLS=0
    if [ -z "$OUR_TASK_DEF" ]; then
      OUR_TASK_DEF=$(echo "$DEPLOYMENT" | jq -r '.taskDefinition // ""')
    fi
    ROLLOUT_STATE=$(echo "$DEPLOYMENT" | jq -r '.rolloutState // "UNKNOWN"')
    ROLLOUT_REASON=$(echo "$DEPLOYMENT" | jq -r '.rolloutStateReason // ""')
    RUNNING_COUNT=$(echo "$DEPLOYMENT" | jq -r '.runningCount')
    DESIRED_COUNT=$(echo "$DEPLOYMENT" | jq -r '.desiredCount')
    PENDING_COUNT=$(echo "$DEPLOYMENT" | jq -r '.pendingCount')
    STATE_SIG="${ROLLOUT_STATE}|running=${RUNNING_COUNT}|desired=${DESIRED_COUNT}|pending=${PENDING_COUNT}"
    if [ "$STATE_SIG" != "$LAST_COUNTS" ]; then
      echo "[${ELAPSED}s] rolloutState=${ROLLOUT_STATE} running=${RUNNING_COUNT} desired=${DESIRED_COUNT} pending=${PENDING_COUNT} ${ROLLOUT_REASON:+(${ROLLOUT_REASON})}"
      LAST_COUNTS="$STATE_SIG"
    fi

    case "$ROLLOUT_STATE" in
      COMPLETED)
        # Treat COMPLETED as success when runningCount == desiredCount. This
        # includes the scale-to-zero case (desired=0, running=0) — see #2585.
        # Earlier revisions required runningCount!=0 to guard against a race
        # where COMPLETED is briefly reported with running=0 during a normal
        # rollout (desired>0); the `running==desired` check preserves that
        # guard for desired>0 while accepting the legitimate desired=0
        # terminal state.
        if [ "$RUNNING_COUNT" = "$DESIRED_COUNT" ]; then
          if [ -n "$FOLLOWED_DESC" ]; then
            echo "Superseding deployment $FOLLOWED_DESC completed; it carries this run's image."
          fi
          if [ "$DESIRED_COUNT" = "0" ]; then
            echo "Service updated successfully after ${ELAPSED}s (scale-to-zero: running=0 desired=0)."
          else
            echo "Service updated successfully after ${ELAPSED}s."
          fi
          exit 0
        fi
        ;;
      FAILED)
        echo "ERROR: ECS reported rolloutState=FAILED for $SELECTOR_DESC after ${ELAPSED}s."
        echo "Reason: $ROLLOUT_REASON"
        echo "Full deployment JSON:"
        echo "$DEPLOYMENT" | jq '.'
        exit 1
        ;;
      IN_PROGRESS|UNKNOWN)
        : # keep polling
        ;;
    esac
    LAST_STATE="$ROLLOUT_STATE"
  fi

  # Re-read the clock (the call above may have been slow) and stop when too
  # little is left for another call to finish inside the budget (#4850).
  NOW_TS=$SECONDS
  if [ "$NOW_TS" -ge "$DEADLINE" ] || wait_budget_exhausted; then
    echo "ERROR: Timed out after ${ROLLOUT_TIMEOUT_SECS}s waiting for $SELECTOR_DESC to stabilize (still rolling out)."
    echo "Last observed state: ${LAST_STATE:-<none>} (${LAST_COUNTS:-<no counts>})"
    echo "Full service JSON snapshot:"
    echo "$SERVICE_JSON" | jq '.services[0] | {serviceName, desiredCount, runningCount, pendingCount, deployments, events: (.events[:5])}'
    echo "Re-run with the same env vars to keep waiting on the same deployment (nothing is redeployed)."
    if [ -n "$FOLLOWED_DESC" ]; then
      echo "This call was following superseding deployment $FOLLOWED_DESC; the re-run detects the supersession again, or wait on it directly with NEW_DEPLOYMENT_ID=$JQ_SELECT_ARG."
    fi
    exit 124
  fi

  wait_budget_sleep "$ROLLOUT_POLL_INTERVAL"
done
