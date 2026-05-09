#!/usr/bin/env bash
# permanent: true
# check-deploy-workflow-rollout.sh — Verify every deploy workflow that pushes
# an image to ECR also rolls the running ECS service / scheduler.
#
# Why this check exists
# ---------------------
# `.github/workflows/deploy-dispatcher.yml` shipped in dispatcher v2 Phase 1
# with only a `build-and-push` job — a TODO docstring noted "Phase 2 will add
# a deploy-dev job that flips the image on the existing task definition."
# Phase 2 (#2768) scaled the service's `desired_count` from 0 to 1 without
# noticing the TODO, so the live rollout came up on the wrong image (#2772).
# The fix was mechanical (mirror `deploy-api.yml`) — but nothing flagged that
# the workflow was incomplete.
#
# This is a recurring shape of bug: a `deploy-<service>.yml` workflow that
# builds + pushes an image to ECR but never calls a rollout. Once the service
# is at `desired_count > 0`, every subsequent merge silently drifts — the new
# image sits in ECR while the running task stays on the old one. Bug invisible
# until someone notices code changes aren't actually live in dev. See #2777.
#
# What gets flagged
# -----------------
# A `.github/workflows/deploy-*.yml` workflow that meets BOTH conditions:
#
#   1. The workflow has at least one `run:` step that calls `docker push` or
#      `aws ecr put-image` / `aws ecr batch-delete-image` style image-side
#      effects — i.e. ships an image to ECR.
#
#   2. NO step in the workflow rolls a downstream rollout target. A rollout
#      step is any of:
#        a. `aws ecs update-service` (force-new-deployment / new task-def)
#        b. `aws scheduler update-schedule` (EventBridge scheduler flavor —
#           used by deploy-scraper.yml for the per-court scraper schedule)
#        c. `aws ecs register-task-definition` (task-def-only families like
#           the agent-runner — re-registering the task-def keeps the
#           daemon's image-freshness invariant true; see #3863)
#        d. `uses: ./.github/actions/ecs-deploy` (composite action that
#           internally calls `update-service` or `update-schedule`)
#
# Opt-out marker
# --------------
# Workflows that build + push to ECR INTENTIONALLY without rolling (e.g.
# `deploy-dispatcher-v3.yml` — image goes to ECR, task-def re-registration
# lands in a follow-up issue) carry the magic comment:
#
#   # deploy-rollout-lint: build-only
#
# anywhere in the file. The marker pairs with a comment explaining why the
# workflow is intentionally build-only. This keeps the lint tight without
# punishing legitimate "image lands first, task-def lands later" sequencing.
#
# Conservatism — what is NOT flagged
# ----------------------------------
# - Workflows whose filename does NOT match `deploy-*.yml` — they are not
#   release pipelines and the rule does not apply.
# - Workflows that do not push to ECR at all — they are not in scope.
# - Workflows carrying the `# deploy-rollout-lint: build-only` opt-out.
#
# Usage
# -----
#   scripts/check-deploy-workflow-rollout.sh           # scan repo
#   scripts/check-deploy-workflow-rollout.sh DIR       # scan DIR/.github/workflows
#
# Exit codes
# ----------
#   0 — All clean: every deploy workflow that pushes to ECR also rolls.
#   1 — One or more deploy workflows push without a rollout step.
#   2 — Script error (cannot find workflows directory, etc.).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCAN_DIR="${1:-$REPO_ROOT}"

WORKFLOWS_DIR="$SCAN_DIR/.github/workflows"

if [ ! -d "$WORKFLOWS_DIR" ]; then
    # Tolerate scanning a subdir that has no workflows — exit 0 cleanly.
    if [ "$SCAN_DIR" != "$REPO_ROOT" ]; then
        exit 0
    fi
    echo "ERROR: $WORKFLOWS_DIR directory not found." >&2
    exit 2
fi

# ─── Collect candidate deploy-*.yml workflows ─────────────────────────────
# `find -maxdepth 1` keeps this from descending into per-workflow subdirs
# (e.g. `.github/workflows/composite-action-fixtures/`). The shell pattern
# `deploy-*.yml` matches both `.yml` and not `.yaml`; if any future deploy
# workflow lands as `.yaml`, extend this glob.
#
# Bash 3.2 doesn't have `mapfile`; use the read-loop form instead. See
# docs/agent/code-standards.md §macOS bash 3.2 compatibility.
deploy_files=()
while IFS= read -r line; do
    deploy_files+=("$line")
done < <(find "$WORKFLOWS_DIR" -maxdepth 1 -type f -name 'deploy-*.yml' | sort)

if [ ${#deploy_files[@]} -eq 0 ]; then
    # No deploy workflows present — nothing to lint. Exit 0 cleanly.
    exit 0
fi

# ─── Scan each candidate ──────────────────────────────────────────────────
violations=()

# Image-push detection — a `docker push` invocation OR an `aws ecr` write
# verb on a `run:` line. We accept either form because some workflows use
# `aws ecr put-image` (rare) and others use `docker push` (typical).
PUSH_PATTERN='docker push|aws ecr put-image|aws ecr batch-delete-image|aws ecr create-repository'

# Rollout-signal detection — any of the four canonical signals.
# The ecs-deploy composite is matched by its `uses:` line; the others by
# the `aws` CLI verb on a `run:` line.
ROLLOUT_PATTERN='aws ecs update-service|aws scheduler update-schedule|aws ecs register-task-definition|uses:[[:space:]]*\./\.github/actions/ecs-deploy'

# Magic-comment opt-out marker.
OPT_OUT_MARKER='# deploy-rollout-lint: build-only'

for f in "${deploy_files[@]}"; do
    # Quick check: does this workflow opt out?
    if grep -qF "$OPT_OUT_MARKER" "$f"; then
        continue
    fi

    # Does this workflow push to ECR?
    if ! grep -qE "$PUSH_PATTERN" "$f"; then
        continue
    fi

    # Does this workflow roll a downstream rollout target?
    if grep -qE "$ROLLOUT_PATTERN" "$f"; then
        continue
    fi

    # Push without rollout — flag it.
    violations+=("$f")
done

# ─── Report ───────────────────────────────────────────────────────────────
if [ ${#violations[@]} -eq 0 ]; then
    echo "All clean — every deploy workflow that pushes to ECR also rolls."
    exit 0
fi

echo "ERROR: deploy workflow(s) push an image to ECR but never roll the running service or scheduler." >&2
echo "" >&2
for v in "${violations[@]}"; do
    rel="${v#"$REPO_ROOT/"}"
    echo "  $rel" >&2
done
echo "" >&2
echo "Each file above contains a step that calls \`docker push\` (or \`aws ecr put-image\`)" >&2
echo "but no step that calls any of:" >&2
echo "" >&2
echo "  - aws ecs update-service          (service rollout)" >&2
echo "  - aws scheduler update-schedule   (EventBridge scheduler rollout)" >&2
echo "  - aws ecs register-task-definition (task-def-only rollout, e.g. agent-runner)" >&2
echo "  - uses: ./.github/actions/ecs-deploy (composite action wrapping the above)" >&2
echo "" >&2
echo "Fix: add a deploy step that calls one of the above. See deploy-api.yml's" >&2
echo "deploy-dev job and deploy-scraper.yml's deploy-staging job for canonical" >&2
echo "examples; deploy-dispatcher.yml's deploy-to-dev job for the" >&2
echo "force-new-deployment shape." >&2
echo "" >&2
echo "If the workflow is intentionally build-only (image goes to ECR, task-def" >&2
echo "rollout lands in a follow-up issue), add the opt-out marker:" >&2
echo "" >&2
echo "  # deploy-rollout-lint: build-only" >&2
echo "" >&2
echo "anywhere in the file (typically in the header docstring) and pair it with" >&2
echo "a comment explaining why. See deploy-dispatcher-v3.yml for the canonical" >&2
echo "build-only example. Background: issues #2772 / #2777." >&2
exit 1
