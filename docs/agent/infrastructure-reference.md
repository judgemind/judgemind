# Infrastructure — Agent Reference

> **When to read this:** only when working on deployed infrastructure, Vercel/frontend deploys, or AWS resources.

## Accounts

**GitHub:** org `judgemind/judgemind`, active account `judgemind-agent` (scopes: gist, project, read:org, repo, workflow).

**AWS:** account `155326049300`, user `admin`, region `us-west-2`. This is the Judgemind AWS account, not a personal account.

**Deployed resources (dev):**
- Terraform state: S3 bucket `judgemind-terraform-state`, DynamoDB lock table `judgemind-terraform-locks`
- Document archive: S3 bucket `judgemind-document-archive-dev`
- Assets: S3 bucket `judgemind-assets-dev`

## Web Frontend (Vercel)

The Next.js web app (`packages/web/`) is deployed on **Vercel** with automatic Git-based deployments. Vercel watches the `judgemind/judgemind` repo and deploys when `packages/web/` changes on push to `main` (production) or any PR branch (preview). Non-web commits (scrapers, infra, docs) are automatically skipped via the Vercel `ignore_command` in Terraform.

**Infrastructure:** managed by Terraform module `vercel-web` in `infra/terraform/environments/hosting/`. The Vercel API token is stored in Secrets Manager at `judgemind/vercel/api-token`.

**Environments:**

| Environment | URL | Vercel project | Trigger |
|---|---|---|---|
| Dev | `dev.judgemind.org` | `judgemind-web-dev` | Push to `main` (only when `packages/web/` changed) |
| Preview | `*.vercel.app` (auto-generated) | `judgemind-web-dev` | Push to any PR branch (only when `packages/web/` changed) |

**Environment variables** (set in Vercel project, managed by Terraform):
- `NEXT_PUBLIC_GRAPHQL_URL` = `https://dev.api.judgemind.org/graphql`

**Checking deploy status (preferred — use `gh run watch`):**
```
# Watch the Vercel deploy status workflow (standard agent pattern)
gh run list --repo judgemind/judgemind --workflow vercel-deploy-status.yml --branch main --limit 1 --json databaseId -q '.[0].databaseId'
gh run watch <run-id> --repo judgemind/judgemind --interval 60 --exit-status --compact
```

The `vercel-deploy-status.yml` GitHub Action runs on every push to `main`. It detects whether `packages/web/` changed:
- **Web changed:** polls the Vercel Deployments API until the deploy completes, then exits with success/failure. It first queries by exact commit SHA; if the deployment is not found after 5 attempts (handles squash merges where Vercel stores the branch SHA, not the merge commit SHA), it falls back to querying recent production deployments by timestamp.
- **No web changes:** exits immediately with success (so the workflow stays green).

This lets agents use the standard `gh run watch` pattern instead of polling the Vercel API in a loop.

**Fallback (manual check):**
```
# List recent deployments (requires Vercel CLI: npm i -g vercel)
vercel list judgemind-web-dev --token "$VERCEL_API_TOKEN"

# Or check from the Vercel dashboard:
# https://vercel.com/judgemind2026-7926s-projects/judgemind-web-dev/deployments
```

## Terraform

### Terraform apply after merge

**Dev apply is automated by the `Terraform / dev-apply` GitHub Actions job** (`.github/workflows/terraform.yml`). When a PR that touches `infra/terraform/**` merges to `main`, the `terraform` job runs validate + plan first; if that passes, the `dev-apply` job runs `terraform -chdir=infra/terraform/environments/dev apply -auto-approve -input=false -lock-timeout=120s` against the merge commit. Apply output (including the "Apply complete! N added, M changed, K destroyed" summary) is captured as a step summary on the workflow run for audit. The `dev-apply` job is gated on `push:main` + `paths: infra/terraform/**`, so PR events never trigger an apply.

**Production applies are NOT automated.** `environments/production/` is human-only and has no apply job in the workflow.

**If the `dev-apply` workflow fails:**

1. Pull the latest `main` and re-run apply locally against the dev environment:
   ```
   terraform -chdir=infra/terraform/environments/dev init
   terraform -chdir=infra/terraform/environments/dev plan
   terraform -chdir=infra/terraform/environments/dev apply -auto-approve -input=false -lock-timeout=120s
   ```
2. If the failure is a state lock timeout, check for a concurrent apply (prior failed run still holding the lock) via the DynamoDB lock table `judgemind-terraform-locks`. The local apply with `-lock-timeout=120s` is typically enough to ride out transient locks.
3. If the failure is a legitimate apply error (IAM, resource conflict, module bug), fix the root cause and land a follow-up PR.
4. File a `priority/p1` issue if the failure looks like a workflow regression (e.g., the job no longer fires, creds expired).

**Targeted module applies** (e.g., while debugging an isolated module):
```
terraform -chdir=infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
```

**For DNS/hosting environments** that require the Cloudflare API token (not yet wired into the workflow — these stay manual):
```
scripts/with-secret.sh -e CLOUDFLARE_API_TOKEN=judgemind/cloudflare/api-token -- terraform -chdir=infra/terraform/environments/dns apply -auto-approve
```

**Important:** The root `infra/terraform/` directory does not track deployed resources. Each environment has its own state backend under `infra/terraform/environments/<env>/`. Running apply from the root creates duplicate resources that collide with the real ones. Always use the environment-specific path. Production applies (`environments/production/`) are human-only. **The PreToolUse hook (`preflight-bash.sh`) blocks `terraform apply` and `terraform destroy` commands that target the root path.** The `preflight_tf_not_root` function in `scripts/preflight.sh` provides the same check for scripts.

### Dev maintenance-window hazard

AWS resources that have a weekly maintenance window default to deferring some configuration changes (instance class, engine version, parameter groups) to that window rather than applying them on the next terraform apply. When the dispatcher auto-applies infra PRs on dev, this silent deferral causes the expected diff to show "applied successfully" but the actual change to land days later — forcing a manual reboot or `aws` CLI workaround.

**Rule:** dev modules with maintenance windows should set `apply_immediately = true` (or the module's equivalent) so dispatcher-driven applies land changes on the next apply. Production keeps the default (`false`) so reboots happen during the scheduled window, not during business hours.

Current coverage:

| Resource | Terraform arg | Dev override | Notes |
|---|---|---|---|
| RDS (`aws_db_instance`) | `apply_immediately` | `true` (#2573) | `modules/database` exposes `var.apply_immediately` |
| ElastiCache (`aws_elasticache_cluster`) | `apply_immediately` | `true` (#2581) | `modules/cache` exposes `var.apply_immediately` |
| OpenSearch (`aws_opensearch_domain`) | _(no equivalent)_ | n/a | User-initiated changes run via blue/green deploy that starts immediately; `software_update_options` / Auto-Tune `maintenance_schedule` only govern AWS-initiated updates, not terraform changes. See the comment in `modules/search/main.tf`. |

When adding a new module that wraps a resource with a maintenance window, check the provider docs for `apply_immediately` (or the equivalent) and wire it through with a dev override. See `modules/database/main.tf` and `modules/cache/main.tf` for the canonical pattern.

### Pre-PR Checklist for Terraform Tasks

See `docs/terraform-checklist.md` for the full checklist.

### Terraform task-def edits — silent-drop gotcha

**Background.** On 2026-04-19 (PR #2836 / parent issue #2840), `terraform apply` silently produced a `judgemind-dispatcher-dev` task-definition revision **without** the `ANTHROPIC_API_KEY` entry in its `secrets` array, despite:

- The HCL wiring being correct (`anthropic_api_key_secret_arn = data.aws_secretsmanager_secret.anthropic_api_key.arn` in `environments/dev/main.tf`).
- `terraform state show data.aws_secretsmanager_secret.anthropic_api_key` returning the populated ARN.
- A `-replace='module.dispatcher_daemon.aws_ecs_task_definition.dispatcher'` apply running cleanly with `Apply complete! Resources: 2 added, 0 changed, 1 destroyed`.
- A subsequent `terraform plan` reporting "No changes."

The conditional `concat()` block in `modules/dispatcher-daemon/main.tf` (lines 737-775) appends each secret entry only when its ARN variable is non-empty:

```hcl
secrets = concat(
  var.anthropic_api_key_secret_arn != "" ? [
    { name = "ANTHROPIC_API_KEY", valueFrom = var.anthropic_api_key_secret_arn }
  ] : [],
  ...
)
```

That pattern is correct in principle, but it has a silent-failure mode: if the variable evaluates to `""` for *any reason* (stale data-source evaluation, provider content-hash dedup against a previous revision, refresh quirk), the conditional drops the entry and the rendered JSON is missing the secret. The variable-level `precondition` block (#2838 / PR #3233) catches the case where the ARN is empty *at the variable level* — but it does NOT catch the case where the ARN is non-empty yet the rendered JSON ends up without the secret entry.

**Prevention mechanism (#3764).** The `aws_ecs_task_definition.dispatcher` resource carries `lifecycle.postcondition` blocks that check the rendered `container_definitions` JSON against `self.container_definitions` for each required secret name:

```hcl
postcondition {
  condition = (
    var.anthropic_api_key_secret_arn == "" ||
    strcontains(self.container_definitions, "ANTHROPIC_API_KEY")
  )
  error_message = "dispatcher-daemon: rendered container_definitions is missing ANTHROPIC_API_KEY despite anthropic_api_key_secret_arn being set."
}
```

These postconditions evaluate at plan time (when the rendered JSON is statically knowable) or at apply time (otherwise). Either way, an apply that would produce a task-def revision without a required secret fails loudly instead of silently registering a broken revision. Coverage: ANTHROPIC_API_KEY, DATABASE_URL, GITHUB_TOKEN, TELEGRAM_BOT_TOKEN, GEMINI_API_KEY.

A regression test fixture lives in `infra/terraform/modules/dispatcher-daemon/tests/postconditions/` and is wired into the `Terraform / Validate and Plan` CI job.

**Operator gotcha.** When editing `modules/dispatcher-daemon/main.tf` to add a new secret, mirror the pattern: add the conditional `concat()` branch, the variable-level `precondition` (if `desired_count > 0` requires it), AND the content-level `postcondition` that asserts the secret name appears in the rendered JSON. Without the postcondition, a future regression that drops the `concat()` branch (or any other path that produces empty rendered JSON despite a non-empty ARN var) will silently ship a broken task-def revision.

**Workaround for past silent drops.** If you encounter a deployed task-def revision that's missing a secret despite the HCL being correct: register a corrected revision out-of-band via `aws ecs register-task-definition` with the missing entry injected, then `aws ecs update-service --task-definition <family>:<rev> --force-new-deployment`. The next terraform apply will reconcile to a clean state once the postcondition catches any remaining drift.

### Terraform task-def edits — deploy-* preserve-secrets gotcha

**Background.** Parent issue #2840 surfaced a sibling failure mode to the silent-drop above: `.github/actions/ecs-deploy/action.yml` (consumed by `deploy-api.yml`, `deploy-scraper.yml`, `deploy-production.yml`) historically read the *currently registered* task-definition revision via `aws ecs describe-task-definition`, swapped the image, and re-registered. That implicitly preserved every other field — including secrets, env vars, and IAM role ARNs that terraform may have *removed* in the interim.

The concrete symptom: PR #2820 removed `GITHUB_TOKEN` from the API task-def in terraform. Subsequent `deploy-api` runs re-registered task-def revisions that still contained `GITHUB_TOKEN` — sourced from the *previous* revision deploy-api itself had registered before #2820 merged. Terraform's removal never propagated end-to-end because every merge to `main` re-ran deploy-api with the previous revision as the base.

**Architectural decision (#3765 — chunk B of #2840): Option 1 — Terraform writes the rendered `container_definitions` JSON to an SSM parameter; `ecs-deploy` reads the desired state from there.** Terraform becomes the single source of truth for non-image task-definition fields (secrets, env vars, log config, port mappings). The deploy-* workflows substitute the new image URI into the named container at deploy time and register a new revision based on terraform's intent — not on whatever stale content the running revision happens to carry.

The hybrid pragmatism: family-level metadata (`cpu`, `memory`, `network_mode`, `execution_role_arn`, `requires_compatibilities`) still comes from `describe-task-definition`. Those fields rarely change and live on the task-definition itself, not the container_definitions array, so reading them from the running revision is harmless. Only the `container_definitions` array — where the silent-preserve bug manifests — switches to the SSM source.

**Why not Option 2** (terraform manages only the initial revision, deploy-* owns subsequent edits via template files in the repo)? It splits desired-state ownership across two systems with two versioning models — terraform's HCL conditionals + provider rendering, versus deploy-* templates that are not exercised on every infra apply. The SSM-source approach (Option 1) keeps the desired state computed once, by terraform, and shipped to the deploy job at read time.

**Mechanism.**

1. Each terraform module that owns a task-definition publishes `aws_ssm_parameter "container_definitions"` with `value = aws_ecs_task_definition.<svc>.container_definitions`. Currently wired:
   - `infra/terraform/modules/api-service/main.tf` → `/judgemind/api/<env>/container-definitions`
   - `infra/terraform/modules/compute/main.tf` (per-court scraper) → `/judgemind/scraper/<env>/container-definitions`
   - `infra/terraform/modules/compute/main.tf` (ingestion-worker) → `/judgemind/ingestion-worker/<env>/container-definitions`
2. `dev-apply` in `terraform.yml` runs on every `push:main` that touches `infra/terraform/**`, so the SSM parameter is always in sync with the latest committed terraform render.
3. `.github/actions/ecs-deploy/action.yml` accepts an optional input `desired-container-definitions-ssm-parameter`. When set, the action invokes `scripts/ecs-render-task-def.sh` with that parameter name; the script reads the JSON from SSM, substitutes the new image into the named container, and registers the new revision.
4. When the input is empty, the action falls back to the legacy `describe-task-definition` source — preserving behaviour for any deploy-* workflow that has not yet opted in.

**Coverage and follow-ups.** Wired today: `deploy-api.yml` (#3769), `deploy-scraper.yml` (#3770 — both the scraper job and the ingestion-worker job), `deploy-production.yml` (#3770 — production scraper). Every workflow that consumes `.github/actions/ecs-deploy/action.yml` is now on the SSM-source path. The `infra/terraform/modules/scraper-zero-record-check/` task-def is launched directly by EventBridge Scheduler (and one-shot via `scripts/ecs-run-task.sh`), not through the composite action, so it does not need migration. New deploy-* workflows that adopt `ecs-deploy/action.yml` should opt in to SSM-source mode as part of their first PR — add the `aws_ssm_parameter` to the owning module and pass the name to the composite action via `desired-container-definitions-ssm-parameter`.

**Operator gotcha.** When adding a new env var or secret to an SSM-source-mode task-definition, the change must land via terraform — direct edits to `register-task-definition` calls or one-off CLI tweaks will be silently overwritten by the next deploy. The SSM parameter is rendered from `aws_ecs_task_definition.<svc>.container_definitions`, so any change to that resource flows through automatically.

**Test fixture.** `scripts/tests/test_ecs_render_task_def.sh` exercises the SSM-source path with a mock AWS CLI: it stages a "running" task-def that contains a secret terraform has since removed, points the script at an SSM-parameter response that omits the secret, and asserts the registered revision does **not** include the removed secret. This is the regression test for the #3765 preserve-bug class — it would have failed against the pre-fix action.

## Dispatcher v2 Cutover

The dispatcher v2 daemon is an opt-in production replacement for the laptop-dispatcher's `/dispatcher` skill. It runs on Fargate, claims `agent/ready` issues from GitHub, and drives each one end-to-end through `claude -p '/task-v2-*'` subprocesses (plan → ralph → summary → push → CI watch → merge → deploy watch → verify → retro → cleanup). Phase 3 (#2782) shipped all the orchestration code; Phase 3E (#2798) was the final code piece. **The cutover from `concurrency_cap=0` (cold) to `concurrency_cap=1` (one in-flight agent) is an explicit operator action — not part of any PR.**

Spec: `docs/specs/dispatcher-v2-spec.md` §6 (state machine), §8 (failure taxonomy + diagnoser), §15 (Phase 3 gate).

### How dispatcher-v2 skill changes propagate

Skill files are read from the daemon's baseline clone at worktree-creation time — they are **never baked into the agent-runner image**. Merging a PR that touches only `.claude/skills/**` does not trigger an image rebuild; the new skill content is picked up automatically when the next worktree is created.

**Propagation path:**

1. PR merges to `main`.
2. Before every `git worktree add`, the daemon calls `_baseline_fetch_origin_main` in `scripts/dispatcher/daemon.py` (~line 5562). This fast-forwards the baseline clone to `origin/main`.
3. The new worktree is created from the freshly-fetched baseline, so its `.claude/skills/` tree contains the latest committed skill files.
4. `scripts/dispatcher/agent-runner-entrypoint.sh` runs `cd "$REPO_ROOT"` (line 190, plus the per-phase wrapper at line 1785) before launching each `claude -p /task-v2-<phase>` subprocess — so the skill is resolved from the worktree filesystem, not from anywhere in the container image.

**Why `.claude/skills/**` is absent from the deploy trigger:**

`.github/workflows/deploy-agent-runner.yml` has an explicit `paths:` filter. It deliberately **excludes** `.claude/skills/**` because skill files are not `COPY`'d into the image. Inspecting `Dockerfile.dispatcher-agent-runner` (the COPY block at lines 147–151) confirms only these paths land in the image:

- `scripts/dispatcher/`
- `scripts/check-issue-author.sh`
- `scripts/preflight.sh`
- `scripts/preflight-bash-fargate.sh`
- `.claude/hooks/preflight_cross_worktree.py`

**What DOES trigger an image rebuild:**

Changes to any of the paths listed above — plus `Dockerfile.dispatcher-agent-runner` or `deploy-agent-runner.yml` itself — fire the deploy workflow. Those changes are picked up by the next `ecs:RunTask` via the `:latest` tag. Changes to `scripts/dispatcher/helpers/`, `scripts/dispatcher/tests/`, or `*.md` files within `scripts/dispatcher/` do **not** trigger a rebuild (excluded by the paths filter).

**Authoritative grep targets:**

- `Dockerfile.dispatcher-agent-runner` — COPY block shows exactly what the image contains.
- `.github/workflows/deploy-agent-runner.yml` — `paths:` block shows what triggers a rebuild.
- `scripts/dispatcher/daemon.py` — search `_baseline_fetch_origin_main` for the fetch-before-worktree call.
- `scripts/dispatcher/agent-runner-entrypoint.sh` — search `cd "$REPO_ROOT"` for the per-phase directory wrapper.

### Phase 3 cut-over: `concurrency_cap=0` → `1`

**Prerequisites (all required):**

- All Phase 3 sub-tasks merged: #2783 (3A), #2787 (3B), #2792 (3C), #2796 (3D), #2798 (3E).
- `deploy-dispatcher.yml` workflow is green for the most recent commit on `main`.
- Daemon stable on dev for ≥ 1 hour at `cap=0` with the new code (check the CloudWatch log group `/ecs/judgemind-dispatcher-dev` — only `daemon.scheduler_tick` / `daemon.supervisor_tick` events; no `daemon.advance_failed` / `daemon.subprocess_*_failed` events at the new code revision).
- No active `dispatcher.agents` rows: the cutover assumes the next agent claimed will be the first new one to exercise the orchestration path end-to-end.
  ```
  scripts/dev-db-query.sh "SELECT count(*) FROM dispatcher.agents WHERE status IN ('running', 'retrying');"
  ```
  Expected: `0`.

**Cut-over procedure:**

1. Flip the cap (single-row UPDATE):
   ```
   scripts/dev-db-query.sh --rw "UPDATE dispatcher.config SET value = '1', updated_by = '<your-handle>', updated_at = now() WHERE key = 'concurrency_cap';"
   ```
   `updated_by` lets the next operator distinguish migration-seeded rows (`init`) from a manual cap flip.
2. Watch daemon logs for the next ~1 hour. The first `daemon.candidate_picked` event indicates the daemon claimed an issue.
   ```
   scripts/ecs-logs.sh /ecs/judgemind-dispatcher-dev --follow
   ```
   Or a CloudWatch Logs Insights query: filter on `event = "candidate_picked"` to spot the first claim immediately.
3. Watch the agent through every phase: plan → ralph → summary → PR open → CI watch → merge → deploy watch → verify → retro → cleanup. The expected event sequence in CloudWatch is:
   - `daemon.candidate_picked` → `daemon.atomic_claim_inserted` → `daemon.phase_started phase=plan` → `daemon.phase_started phase=ralph` → `daemon.phase_started phase=summary` → `daemon.pr_opened` → `daemon.ci_poll` (one or more) → `daemon.pr_merged` → `daemon.deploy_poll` (one or more) → `daemon.verify_started` → `daemon.evidence_comment_posted` → `daemon.agent_completed` → `daemon.retro_started` → `daemon.retro_done` → `daemon.cleanup_done`.
   - Any failure the diagnoser doesn't recover from = pause cutover by flipping `cap` back to `0` (see Rollback below).
4. If the first agent reaches `phase='cleanup_done'` cleanly, the cutover is validated. Leave `cap=1` and let the daemon continue claiming one issue at a time. **Phase 4 (#2782 follow-up)** raises `cap` to `5` after enough single-agent runs accumulate to satisfy the gate criteria below.

### Phase 3 gate (per spec §15 post-#2758 wording)

Phase 3 is considered "done" once the daemon has:

- ≥ 10 successful task completions (`dispatcher.agents.status='succeeded' AND PR merged AND phase IN ('cleanup_done', 'cleanup_blocked')`).
- Zero stuck agents at the gate-check moment (`status='running' AND phase NOT IN ('done', 'retro_done', 'retro_failed', 'cleanup_done', 'cleanup_blocked')` for ≥ 30 min).
- All retries resolved correctly — every `dispatcher.diagnoses` row from the gate window has a non-NULL `outcome` consistent with the agent's final status. The Phase 3E (#2798) effectiveness-tracking write-back guarantees this populates automatically; the operator only needs to confirm coverage.

Quick gate-check SQL:

```
scripts/dev-db-query.sh "
SELECT
  count(*) FILTER (WHERE status = 'succeeded' AND phase IN ('cleanup_done', 'cleanup_blocked')) AS successes,
  count(*) FILTER (WHERE status = 'running' AND phase NOT IN ('done', 'retro_done', 'retro_failed', 'cleanup_done', 'cleanup_blocked')
                   AND started_at < now() - interval '30 minutes')                                AS stuck_running,
  (SELECT count(*) FROM dispatcher.diagnoses WHERE outcome IS NULL AND completed_at IS NOT NULL) AS unresolved_diagnoses
FROM dispatcher.agents
WHERE started_at >= now() - interval '7 days';
"
```

`successes ≥ 10`, `stuck_running = 0`, `unresolved_diagnoses = 0` ⇒ proceed to Phase 4. Otherwise stay at `cap=1` and investigate the gap.

### Rollback (any time)

```
scripts/dev-db-query.sh --rw "UPDATE dispatcher.config SET value = '0', updated_by = '<your-handle>', updated_at = now() WHERE key = 'concurrency_cap';"
```

The daemon stops claiming new candidates on the next scheduler tick (≤ 30 s). Any in-flight agent finishes naturally — supervisor ticks continue advancing it through CI watch / merge / deploy / verify / retro / cleanup until it lands in a terminal phase. If the in-flight agent is stuck, the supervisor's `_check_stuck_agents` (Phase 3C, 30-min window) flips it to `crashed` and the retry-marker processor takes over.

### Overnight-safety circuit breaker (#2860)

The daemon auto-pauses (`concurrency_cap` → 0) when a streak of bad terminal outcomes hits the threshold — currently 5 of the last 10 agent terminals in a rolling 30-minute window. "Bad" is the complement of the `OVERNIGHT_CB_GOOD_OUTCOME_STATUSES` set in `scripts/dispatcher/daemon.py` (only `succeeded` is good today; `failed`, `crashed`, `plan_blocked`, `needs_review`, and unknown future terminals all count as bad).

When the breaker opens:

1. `UPDATE dispatcher.config SET value = '0' WHERE key = 'concurrency_cap'` with `updated_by = 'circuit_breaker'`.
2. `UPDATE dispatcher.config SET value = '"circuit_breaker"' WHERE key = 'cap_flipped_by'` — diagnostic trail.
3. Structured CloudWatch event `daemon.circuit_breaker_opened` with the agent list, bad count, threshold, and window.
4. Telegram alert via `scripts/notify-telegram.sh` (best-effort — a missing secret is a silent no-op).
5. Admin cockpit renders the red "Circuit breaker open" banner above the two-column deck.

**Recovering from an open breaker.** The breaker does not auto-close. Investigate the underlying cascade first — run `scripts/dev-db-query.sh "SELECT * FROM dispatcher.terminal_outcomes ORDER BY ended_at DESC LIMIT 20"` to see the recent outcomes and `scripts/ecs-logs.sh /ecs/judgemind-dispatcher-dev --lines 200` for the open event context. Common triggers:

- Gemini API rate limit → every ralph review SKIPPED → summary flags unmet → N `needs_review` in a row.
- Upstream `main` CI red → every PR fails CI → fix-ci retries exhausted → N `failed` in a row.
- Skill regression on plan-phase → every plan returns go=false → N `plan_blocked` in a row.

After addressing the root cause, flip cap back up from the admin cockpit (preferred) or via SQL:

```
scripts/dev-db-query.sh --rw "UPDATE dispatcher.config SET value = '1', updated_by = '<your-handle>', updated_at = now() WHERE key = 'concurrency_cap';"
```

The next scheduler tick observes `cap_flipped_by = 'circuit_breaker' AND concurrency_cap >= 1`, logs `daemon.circuit_breaker_closed`, and clears `cap_flipped_by` back to `null`. New agents start claiming on the following tick.

**Tuning the knobs.** All four thresholds live in `dispatcher.config` and are live-editable:

| Key | Default | Meaning |
|---|---|---|
| `circuit_breaker_enabled` | `true` | Kill switch. Flip to `false` only for controlled chaos testing. |
| `circuit_breaker_window_minutes` | `30` | Rolling window the M-of-N scan considers. |
| `circuit_breaker_window_size` | `10` | N (how many most-recent terminals). |
| `circuit_breaker_bad_outcome_threshold` | `5` | M (bad-outcome count needed to trip). |

Nonsensical values (threshold ≤ 0, window_size ≤ 0) are treated as "breaker disabled" so the rail fails-open rather than tripping on an empty window.

### Per-agent phase enumeration (Phase 3E)

`dispatcher.agents.phase` is free-form `text` per migration 21 — no schema enum to maintain. The daemon-side enumeration (centralised in constants `PHASE_*` in `scripts/dispatcher/daemon.py`):

| Phase | Status | Meaning |
|---|---|---|
| `claiming` | `running` | Initial state at INSERT. |
| `planning` / `ralph` / `summary` | `running` | Mid-orchestration, `claude -p` subprocess in flight. |
| `awaiting_ci` | `running` | Push + PR done; waiting on CI. |
| `awaiting_deploy` | `running` | PR merged; waiting on deploy workflow. |
| `done` | `succeeded` | Verify posted evidence; ready for retro. |
| `retro_done` | `succeeded` | Retro phase ran cleanly; ready for cleanup. |
| `retro_failed` | `succeeded` | Retro skill failed but agent itself succeeded; cleanup still runs. |
| `cleanup_done` | `succeeded` | Final terminal state — worktree removed. |
| `cleanup_blocked` | `succeeded` | Final terminal state — `cleanup_worktree.sh` refused (locked / no session log); operator sweep needed. |
| `awaiting_*` / `claiming` | `crashed` / `failed` | Supervisor flipped status; phase preserved for diagnostics. |

The daemon validates phase strings via the `PHASE_*` constants — adding a new phase value requires a code change but no migration. The **canonical list of phase constants and all phase-transition logic** lives in `scripts/dispatcher/phase_transitions.py`. For the step-by-step procedure to add a new phase (constant + transition function + daemon handler + tests), see the `## Adding a new phase` section in that module's docstring.

### Dispatcher v2 — per-phase token + cost telemetry (#2869)

Migration 31 added `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write`, `cost_usd`, and `model_used` columns to `dispatcher.phase_outputs`. The daemon runs each phase as `claude -p /task-v2-<phase> --output-format json ...`, parses the JSON envelope that ships on stdout (`{usage: {...}, total_cost_usd: ..., model: ...}`), and writes those six fields alongside each `phase_outputs` row.

> **Caveat — list-price, not Max plan.** `total_cost_usd` emitted by Claude Code is the list-price cost estimated against posted Anthropic rates. It does NOT reflect Max plan discounts (which are what the operator is actually billed under). Numbers here are useful for *relative* run-to-run comparison ("is ralph on sonnet cheaper than on haiku?") but NOT for absolute spend accounting.

Two standard dashboards live below. Run them via `scripts/dev-db-query.sh` (or copy into any psql session that has `dispatcher.*` access).

**Last 10 PRs — cost + token totals.** Answers "what did the last day of PR-landing agents cost?" For drilling into why a single agent was expensive, switch to the per-phase breakdown from `DispatcherAgent.phaseCostBreakdown` in the admin cockpit.

```sql
-- Last-day completed PRs, ordered by cost (most expensive first).
-- Pre-migration-31 agents appear with pr_cost_usd = NULL (no signal) —
-- COALESCE would misleadingly sort them as "$0" ahead of real cheap runs.
SELECT a.pr_number,
       a.issue_number,
       SUM(po.cost_usd)                                      AS pr_cost_usd,
       SUM(COALESCE(po.tokens_input, 0) + COALESCE(po.tokens_output, 0)) AS pr_tokens
  FROM dispatcher.agents a
  JOIN dispatcher.phase_outputs po ON po.agent_id = a.agent_id
 WHERE a.pr_number IS NOT NULL
   AND a.ended_at > now() - interval '1 day'
 GROUP BY a.pr_number, a.issue_number
 ORDER BY pr_cost_usd DESC NULLS LAST
 LIMIT 10;
```

**Cost-by-phase, last 24h.** Answers "which phase is the cost bottleneck?" — the canonical tuning question. A consistently-expensive ralph means "swap ralph model" is a meaningful lever; a consistently-expensive plan means "opus on plan is priced out of proportion to the triage quality it provides."

```sql
-- Per-phase cost rollup — total spend + average per-run + runs-so-far.
-- Sorted by total_cost DESC so the biggest line item is on top.
SELECT phase,
       SUM(cost_usd) AS total_cost,
       AVG(cost_usd) AS avg_cost,
       COUNT(*)      AS n_runs
  FROM dispatcher.phase_outputs
 WHERE ts > now() - interval '1 day'
   AND cost_usd IS NOT NULL
 GROUP BY phase
 ORDER BY total_cost DESC;
```

Both queries filter `cost_usd IS NOT NULL` (or sort NULLS LAST) so pre-migration-31 rows don't corrupt the averages or take up slots in the top-10 view.

## Deploy Pipeline Invariants

### Migrations always run before code (#2915)

The API deploy pipeline (`.github/workflows/deploy-api.yml`) enforces a strict ordering: **dev database migrations complete before the new API image is rolled onto the `judgemind-api-dev` service.** The job graph is:

```
build-and-push
  ├─> run-migrations     (needs: build-and-push)                  <-- apply first
  └─> deploy-dev         (needs: [build-and-push, run-migrations]) <-- then ship code
post-deploy-health-check (needs: [deploy-dev, run-migrations])
```

This closes the race window observed in PR #2907 where a new resolver shipped onto dev before its backing migration applied, leaving `/admin/dispatcher` briefly serving `UndefinedColumn` errors. The enforcement is the `needs:` dependency on `deploy-dev` — if migrations fail, the deploy is cancelled and the old image keeps serving traffic.

**Scope.** This covers forward-compatible migrations (add nullable column, add table, add index) — the overwhelming majority of Judgemind schema changes. Migrations-first plus rolling deploy is safe for those regardless of whether the old or new code is running when the new column appears.

**Out of scope (backward-incompatible migrations).** Renames, drops, and type changes are still unsafe under a single-phase rolling deploy and require an explicit two-phase cadence (ship tolerant code → migrate → ship code that uses the new shape). Not enforced by this workflow — plan those PRs manually.

**Other deploy workflows.**
- `deploy-dispatcher.yml` — no migration step. The dispatcher image does not run `scripts/seed-and-migrate.mjs`; the API image owns schema. Nothing to enforce here.
- `deploy-scraper.yml`, `deploy-production.yml` — no migration step. Same reason.

## ECS Script Execution

> **Important:** The dev database is in a private VPC and is not reachable from localhost. Do not attempt to connect to it locally using `scripts/with-secret.sh` with `DATABASE_URL` — the connection will fail. All data scripts must run inside the VPC via `ecs-run-task.sh`.

**Always use `ecs-run-task.sh` for data scripts.** It launches a standalone Fargate task with full VPC access, streams logs from CloudWatch, and handles cleanup automatically. `scripts/ecs-run.sh` uses ECS Exec (SSM sessions) which frequently disconnects within seconds, losing all output. Reserve `ecs-run.sh` only for quick interactive debugging (e.g. `scripts/ecs-run.sh bash`).

| Tool | Use for | Reliability | Notes |
|---|---|---|---|
| `scripts/ecs-run-task.sh` | All data scripts (backfills, migrations, audits) | Reliable | Standalone Fargate task, CloudWatch logs, no session timeout |
| `scripts/dev-db-query.sh` | Quick SQL queries | Good for short queries | Uses ECS Exec internally; may drop on long queries |
| `scripts/ecs-run.sh` | Interactive debugging only | Unreliable | SSM sessions drop after seconds; never use for scripts |

```
# Run a script and wait for completion (default)
scripts/ecs-run-task.sh scripts/backfill_llm_enrichment.py -- --dry-run

# Long-running tasks: launch and detach, check logs later
scripts/ecs-run-task.sh --detach scripts/reingest_from_s3.py -- --all
scripts/ecs-run-task.sh --logs <task-arn>

# Initial population of a county with S3 data but no DB records
scripts/ecs-run-task.sh scripts/rebuild_db.py -- --county "Orange"

# Tail logs for a task by ID (printed when the task launches)
scripts/ecs-task-logs.sh <task-id>
scripts/ecs-task-logs.sh <task-id> --follow

# Override CPU/memory (default: 1024 CPU / 4096 MB)
scripts/ecs-run-task.sh --cpu 2048 --memory 8192 scripts/audit_field_completeness.py
```

**Large-county rebuilds need memory override.** `rebuild_db.py --county <name>` holds per-worker OpenSearch/Postgres clients and LLM batch state for every document in the county. At the default 4096 MB, counties with thousands of documents (Los Angeles, Santa Clara, Orange) can exit 137 (OOM). Use `--cpu 2048 --memory 8192` for these rebuilds (see #2481):

```
scripts/ecs-run-task.sh --cpu 2048 --memory 8192 scripts/rebuild_db.py -- --county "Los Angeles"
scripts/ecs-run-task.sh --cpu 2048 --memory 8192 scripts/rebuild_db.py -- --county "Santa Clara"
scripts/ecs-run-task.sh --cpu 2048 --memory 8192 scripts/rebuild_db.py -- --county "Orange"
```

Smaller counties (a few hundred documents or fewer) run fine at the 1024/4096 default.

### Output-size limits via SSM / ECS Exec

`scripts/dev-db-query.sh` occasionally truncates large query results mid-field. The root cause is the SSM output budget — the SSM / ECS Exec per-session output byte cap (~1 KB effective, varies with task load), not a timing race or psql buffering issue. See #3195.

Three mitigations were landed in #3195 / PR #3201:

- **Compact JSON default** in `scripts/dev_db_query_runner.py` (`json.dumps(..., separators=(",", ":"))`) so the payload is as small as possible before it hits the transport layer.
- **SSM chatter stripping** in `scripts/dev-db-query.sh` (the `grep -Ev` filter against `Starting session…` / `Exiting session…` / `Cannot perform start session: EOF` / `The Session Manager plugin was installed…`) so SSM banner lines do not contaminate JSON consumers.
- **5-attempt retry loop** in `scripts/dispatcher/helpers/_query_lib.sh` (`_query_lib_run`) that re-executes the query and re-validates with `jq -e .` until the captured output parses as valid JSON.

**Architectural escape hatch.** When the caller actually needs more than ~1 KB of structured output, run the work as a oneshot ECS task via `scripts/ecs-run-task.sh` and read results from CloudWatch Logs (or write them to S3). That path does not use SSM, so there is no per-session output cap.

**Rule of thumb:** if you expect more than ~1 KB of JSON from a single helper query, split the query, narrow the `SELECT`, or use `scripts/ecs-run-task.sh` instead of `scripts/dev-db-query.sh`.

### Container Insights (per-task CPU/memory/network visibility)

Container Insights is **enabled** on the `judgemind-dev` ECS cluster (see `aws_ecs_cluster.main` in `infra/terraform/modules/compute/main.tf`). Under the `enabled` tier — which is what's live — CloudWatch emits metrics in the `ECS/ContainerInsights` namespace at these dimension levels:

| Dimension set | What you can ask |
|---|---|
| `ClusterName` | Cluster-wide CPU / memory / network / storage totals |
| `ClusterName + ServiceName` | Per-service (e.g. `judgemind-dispatcher-dev`, `judgemind-api-dev`, `judgemind-ingestion-worker-dev`) |
| `ClusterName + TaskDefinitionFamily` | Per-task-definition-family (covers oneshot `judgemind-oneshot-dev`, `judgemind-scraper-dev`, and agent-runner family rollups) |

Metrics include `CpuUtilized`, `CpuReserved`, `MemoryUtilized`, `MemoryReserved`, `NetworkRxBytes`, `NetworkTxBytes`, `StorageReadBytes`, `StorageWriteBytes`, `EphemeralStorageUtilized`, `EphemeralStorageReserved`, and Fargate task counters (`RunningTaskCount`, etc.).

**Per-`TaskId` metrics require the `enhanced` tier** (billed higher). Under `enabled`, you cannot select a single ECS task ARN — pivot to `TaskDefinitionFamily` when you need narrower dimensions than `ServiceName`. Oneshot tasks (`judgemind-oneshot-dev`) share one family across all launches, so intra-family correlation is done via CloudWatch logs (filter by task id) plus the family-level metric totals over the same time window.

**Per-service query example** — CPU for the dispatcher service over the last hour (MCP-first: `mcp__awslabs_cloudwatch-mcp-server__get_metric_data`). CLI fallback:

```
aws cloudwatch get-metric-data --region us-west-2 \
  --start-time $(date -u -v-1H +%FT%TZ) --end-time $(date -u +%FT%TZ) \
  --metric-data-queries '[{"Id":"m1","MetricStat":{"Metric":{"Namespace":"ECS/ContainerInsights","MetricName":"CpuUtilized","Dimensions":[{"Name":"ServiceName","Value":"judgemind-dispatcher-dev"},{"Name":"ClusterName","Value":"judgemind-dev"}]},"Period":60,"Stat":"Average"},"ReturnData":true}]'
```

**Per-task-definition-family query example** — memory for a oneshot family:

```
aws cloudwatch get-metric-data --region us-west-2 \
  --start-time $(date -u -v-1H +%FT%TZ) --end-time $(date -u +%FT%TZ) \
  --metric-data-queries '[{"Id":"m1","MetricStat":{"Metric":{"Namespace":"ECS/ContainerInsights","MetricName":"MemoryUtilized","Dimensions":[{"Name":"TaskDefinitionFamily","Value":"judgemind-oneshot-dev"},{"Name":"ClusterName","Value":"judgemind-dev"}]},"Period":60,"Stat":"Maximum"},"ReturnData":true}]'
```

**When a task is "stuck," correlate three signals:**

1. **Metric rollup by family/service** at `Period=60, Stat=Maximum` for `MemoryUtilized` and `CpuUtilized` — flat near zero for many minutes on a task that claims to be running suggests the process is hung, not thrashing.
2. **CloudWatch Logs** for the task-specific log stream (the task id suffix in `/ecs/<family>` / `/ecs/<service>` log groups) to correlate metric flats with absence of log lines.
3. **`DescribeTasks`** for the task ARN to confirm `healthStatus`, `lastStatus`, and `stoppedReason` haven't flipped.

If a future investigation genuinely requires per-task-ARN metrics (to disambiguate two concurrent oneshots within the same family), toggle the setting to `enhanced` on a targeted basis — expect a billing uptick proportional to concurrent task count.

### CloudWatch Alarms (dev)

Alarms wired through Terraform — `enable_alerts = true` on each module gates whether the alarm resources are created. `alert_sns_topic_arn` is `module.compute.alerts_topic_arn` (the same SNS topic surfaces in Telegram and the `/admin/dispatcher` cockpit).

| Alarm name prefix | Module | Source | Fires when |
|---|---|---|---|
| `judgemind-api-5xx-` | `api-service` | `AWS/ApplicationELB` `HTTPCode_Target_5XX_Count` | API 5xx error count > threshold in 5 min |
| `judgemind-api-4xx-spike-` | `api-service` | `AWS/ApplicationELB` `HTTPCode_Target_4XX_Count` | API 4xx error count > threshold in 5 min |
| `judgemind-api-latency-p99-` | `api-service` | `AWS/ApplicationELB` `TargetResponseTime` (p99) | API p99 latency > threshold for 2 consecutive 5-min periods |
| `judgemind-api-unhealthy-hosts-` | `api-service` | `AWS/ApplicationELB` `UnHealthyHostCount` | ALB target group has unhealthy hosts |
| `dispatcher-polled-cost-` | `api-service` | log metric filter on `/ecs/judgemind-api-${env}` matching `{ $.msg = "graphql.cost.breakdown" }`, namespace `Judgemind/API`, metric `PolledQueryCost` | Cockpit's polled `dispatcherState` query cost > 900 (cap 1000) in 3 of last 5 minutes — pre-cap early warning, see #4110 |
| `judgemind-scraper-no-success-24h-` | `compute` | log metric filter on `/ecs/judgemind-scraper-${env}` matching `"scraper_run_complete"`, namespace `Judgemind/Scraper`, metric `ScraperSuccessCount` | No successful scraper run in the past 24 hours |
| `judgemind-data-quality-no-run-2h-` | `compute` | log metric filter on `/ecs/judgemind-ingestion-worker-${env}` matching `"data_quality_check_complete"`, namespace `Judgemind/DataQuality`, metric `DataQualityCheckCount` | Hourly data-quality check has not completed in 2 hours — the GitHub Actions workflow may be failing or disabled |
| `judgemind-ingestion-worker-crash-loop-` | `compute` | log metric filter on `/ecs/judgemind-ingestion-worker-${env}` matching `"Infrastructure error"` / `"Unhandled exception"`, namespace `Judgemind/Ingestion`, metric `IngestionWorkerCrashCount` | Ingestion worker has crashed >= 3 times in 15 minutes — see #1044 |
| `judgemind-ingestion-worker-idle-` | `compute` | log metric filter on `/ecs/judgemind-ingestion-worker-${env}` matching `{ $.event = "Heartbeat: idle for *" }`, namespace `Judgemind/Ingestion`, metric `IngestionWorkerIdleSeconds` | Worker idle (no messages) for > `ingestion_idle_threshold_seconds` — alive but not receiving work, see #2220 |
| `judgemind-dispatcher-heartbeat-stale-` | `dispatcher-daemon` | `Judgemind/Dispatcher` `HeartbeatStale` | Daemon heartbeat hasn't advanced in `heartbeat_stale_seconds` |
| `judgemind-dispatcher-stuck-timeout-repeated-` | `dispatcher-daemon` | log metric filter on `stuck_timeout_repeated` events | Same agent hits `stuck_timeout` twice within the configured window |
| `judgemind-dispatcher-diagnoser-fallback-spike-` | `dispatcher-daemon` | log metric filter on `diagnoser_fallback` events | Diagnoser fell back to mechanical escalation `>= diagnoser_fallback_threshold` times in the configured window |
| `judgemind-dispatcher-list-advanceable-failed-` | `dispatcher-daemon` | log metric filter on `_list_advanceable_agents` exceptions | Supervisor tick raised an unhandled exception |
| `judgemind-dispatcher-recover-scan-failed-` | `dispatcher-daemon` | log metric filter on `_recover_scan` exceptions | Supervisor tick raised an unhandled exception |
| `judgemind-dispatcher-resume-scan-failed-` | `dispatcher-daemon` | log metric filter on `_resume_scan` exceptions | Supervisor tick raised an unhandled exception |
| `judgemind-dispatcher-observe-external-terminal-failed-` | `dispatcher-daemon` | log metric filter on `_observe_external_terminal` exceptions | Supervisor tick raised an unhandled exception |
| `judgemind-dispatcher-reap-agent-tasks-select-failed-` | `dispatcher-daemon` | log metric filter on `_reap_agent_tasks` exceptions | Supervisor tick raised an unhandled exception |
| `judgemind-dispatcher-agent-runner-${env}-errors` (search key: `judgemind-dispatcher-agent-runner-errors`) | `dispatcher-agent-runner` | log metric filter on `/ecs/judgemind-dispatcher-agent-runner-${env}` matching `{ $.level = "ERROR" \|\| $.level = "FATAL" }`, namespace `Judgemind/AgentRunner`, metric `AgentRunnerErrorCount` | Agent-runner task emitted ERROR or FATAL — phase may have crashed before the daemon reap pass observed STOPPED |
| `dispatcher-v3-eventbridge-failures-` (per-skill) | `dispatcher-v3-scheduled-skills` | `AWS/Events` `FailedInvocations` per `RuleName = ${name_prefix}-<skill>` | EventBridge rule could not invoke its scheduled-skill ECS target — see adversarial-review MAJOR 7 / #3890 |
| `dispatcher-v3-scheduled-skills-dlq-depth-` | `dispatcher-v3-scheduled-skills` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` on the shared scheduled-skills DLQ | EventBridge enqueued a dead-letter but the ECS task never drained it — see #3956 / #3890 |

Confirm an alarm exists / inspect its config:

```
aws cloudwatch describe-alarms --region us-west-2 \
  --alarm-name-prefix dispatcher-polled-cost
```

Confirm a metric filter exists / inspect its pattern:

```
aws logs describe-metric-filters --region us-west-2 \
  --log-group-name /ecs/judgemind-api-dev
```

### Dev DB Connection Budget

The dev RDS instance (`judgemind-dev`) runs on **`db.t4g.small`** (2 GB RAM).  PostgreSQL 16's `max_connections` is derived from the instance-class memory via the formula `LEAST({DBInstanceClassMemory/9531392}, 5000)` — on `db.t4g.small` this resolves to roughly **~170 connections**.  Reserved slots:

- `rds.rds_reserved_connections = 4`
- `superuser_reserved_connections = 3`
- `reserved_connections = 2`

So usable budget is **~161 concurrent application connections**.

Long-lived steady-state consumers:

| Consumer | Typical connections | Notes |
|---|---|---|
| `judgemind-ingestion-worker-dev` | 1 | Persistent `psycopg.connect` in `ingestion/worker.py::_get_connection`, reused across events |
| `judgemind-api-dev` | 1-2 per task | Short-lived per-request connections plus minor overhead |
| CloudWatch / Performance Insights | 1-2 | RDS management connections |
| Subtotal | **~5** | Baseline even with no scripts running |

Burst consumers — watch these when launching oneshot tasks:

| Consumer | Max connections | Notes |
|---|---|---|
| `rebuild_db.py --concurrency N` | **N + 1** | One per `ProcessPoolExecutor` worker, plus the main process's reset connection (default `--concurrency 64` → 65 connections; `--concurrency 16` → 17) |
| `reingest_from_s3.py` | 1-2 | Single-process script |
| `enrich_all_rulings.py` | 1-2 | Single-process script, but holds a long-running transaction |
| `scripts/dev-db-query.sh` | 1 | One-shot query per invocation |

**Why this matters.** Launching a second `rebuild_db.py` while one is already running (`2 × 65 = 130 connections`), or letting an old `rebuild` hang around retrying failed connections while you start a new one, can push total past the ~161 usable ceiling.  The first script to get refused logs:

```
psycopg.OperationalError: connection failed: … FATAL:
remaining connection slots are reserved for roles with privileges of the "rds_reserved" role
```

Best practices:

1. Never launch a rebuild while another rebuild or large backfill is already running against dev. Check first — preferred path: `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "ListTasks"`, `api_params: {"cluster": "judgemind-dev", "desiredStatus": "RUNNING"}`. CLI fallback: `aws ecs list-tasks --cluster judgemind-dev --desired-status RUNNING`. (See `docs/agent/aws-api-access.md`.)
2. If you see `rds_reserved` errors, first check for runaway oneshot tasks (preferred: MCP `ListTasks` + `DescribeTasks` as above; CLI: `aws ecs list-tasks` then `aws ecs describe-tasks`) and stop any that are stuck retrying — each zombie task holds N connections until it exits.
3. When iterating locally, prefer `scripts/rebuild_db.sh` against the Docker Compose Postgres rather than dev.
4. If you *must* run rebuild with aggressive concurrency on dev, drop `--concurrency` to match the headroom (e.g. `--concurrency 32` leaves ~100 connections free for other callers). Note: as of #2575, `rebuild_db.py` queries `max_connections` and `pg_stat_activity` at startup and self-clamps `--concurrency` to stay within the 80% connection headroom — manual tuning is a fallback, not a requirement.

**History.** The instance was bumped from `db.t4g.micro` (max_connections ≈ 84) to `db.t4g.small` in #2549 after rebuild + backfill contention reliably triggered connection-slot exhaustion.

### Zombie oneshot prevention (retry cap + lifetime cap)

**Problem.** A `rebuild_db.py` run against a dev database that is already near its connection limit can hit `BrokenProcessPool` in every worker, then enter the serial-retry pass.  The serial pass re-runs each crashed key in its own `max_workers=1` subprocess — fine for a handful of bad PDFs, catastrophic when *every* key crashed because of a systemic cause (DB exhaustion, OOM, network partition).  A 1,694-key rebuild then tries to serially retry all 1,694 keys at roughly one every few minutes, turning a 10-minute rebuild into a 12+ hour zombie task while the exhausted resources never recover (#2572, #2549).

**Defense in depth.** Two independent caps now prevent this pattern:

1. **In-script retry cap — `scripts/rebuild_db.py`.**  Before entering the serial retry pass, the script checks whether the crash count exceeds a configurable threshold.  If it does, the pass is aborted with a terminal error that names the systemic cause (pool exhaustion, OOM) and exits non-zero so the orchestrator surfaces the failure.

   | Flag | Default | Env var | Purpose |
   |---|---|---|---|
   | `--max-retry-count` | `200` | `REBUILD_MAX_RETRY_COUNT` | Absolute ceiling on crashed keys eligible for serial retry.  Set to `0` to disable. |
   | `--max-retry-ratio` | `0.10` | `REBUILD_MAX_RETRY_RATIO` | Fraction of total keys that crashed.  A high ratio (e.g. 15%) signals systemic failure.  Set to `0` to disable. |

   Strict `>` comparisons on both thresholds: `--max-retry-count 200` means "up to and including 200 retries, abort above that."  The abort also logs a sample of 20 crashed keys so operators have a starting point for manual diagnosis.  Exit code is `2` (distinct from the normal `0`/`1`) so alerting can distinguish retry-cap aborts from per-doc failures.

2. **Container-level lifetime cap — `scripts/ecs-run-task.sh --max-runtime <secs>`.**  Independently of what the script does, ECS oneshot tasks can be wrapped with `timeout --preserve-status --signal=TERM --kill-after=30 <secs>` so the container self-terminates after a bounded wall-clock deadline.  This is opt-in (no default) to preserve behavior for existing callers — pass it explicitly for long-running jobs:

   ```
   # Cap a rebuild at 2 hours even if it hangs on something not covered by --max-retry-*
   scripts/ecs-run-task.sh --max-runtime 7200 --cpu 2048 --memory 8192 \
       scripts/rebuild_db.py -- --county "Los Angeles"
   ```

   `timeout` sends `SIGTERM` at the deadline, giving Python's atexit handlers and `psycopg` a chance to close connections cleanly, and escalates to `SIGKILL` 30 seconds later if the script ignores the signal.  The container then exits with the script's own exit code (on clean termination) or `137` (SIGKILL).  Requires coreutils, which is present in the `python:3.12-slim` base image used by the ingestion worker task definition.

**When to use which.**  The in-script cap is always-on for `rebuild_db.py` and handles the specific pool-break-storm pattern surgically.  The lifetime cap is a blanket backstop for any oneshot that could hang for reasons the script doesn't know about (slow network, LLM API outage, stuck DB query).  Use both together for rebuilds on dev.

**Manual stop runbook.**  If you spot a zombie oneshot already running (ECS task that has been `RUNNING` far longer than expected, or dev DB showing `rds_reserved` errors):

1. **Find the task ARN.** Preferred: `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "ListTasks"`, `api_params: {"cluster": "judgemind-dev", "desiredStatus": "RUNNING"}`. CLI fallback: `aws ecs list-tasks --cluster judgemind-dev --desired-status RUNNING --region us-west-2`.
2. **Confirm it's the oneshot and check `startedAt` vs now.** Preferred: `ecs_resource_management` with `api_operation: "DescribeTasks"`, `api_params: {"cluster": "judgemind-dev", "tasks": ["<arn>"]}`. CLI fallback: `aws ecs describe-tasks --cluster judgemind-dev --tasks <arn> --region us-west-2`.
3. **Stop the task** — sends SIGTERM then SIGKILL. MCP-first: `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "StopTask"` and `api_params: {"cluster": "judgemind-dev", "task": "<arn>", "reason": "zombie retry-loop (#2572)"}`. CLI fallback: `aws ecs stop-task --cluster judgemind-dev --task <arn> --reason "zombie retry-loop (#2572)" --region us-west-2`.
4. Wait for the task to fully STOP (connection slots release as psycopg closes).  Verify with `scripts/dev-db-query.sh "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '%rebuild%'"`.
5. Diagnose the root cause before re-running — if connection exhaustion, confirm no other rebuild is running; if OOM, bump `--memory`; if the retry cap was tripped, consult the crashed-key sample in the CloudWatch logs.

### Reingest vs Rebuild

`reingest_from_s3.py` operates on **existing database records only** — it queries the `documents` table to find S3 keys to reprocess. If you run it for a county with no records in the `documents` table, it will process 0 documents silently.

| Scenario | Script | Why |
|---|---|---|
| Cleanup orphaned/corrupted `derived.*` state (failed run, bad IDs, partial mutation) | `rebuild_db.py --county <name>` | `derived.*` is fully rebuildable from S3. Rebuild is idempotent, validates the real ingestion/enrichment path (fixing inbound data, not just existing rows), and handles edge cases surgical scripts miss. Surgical one-offs often introduce bugs of their own — only write one if rebuild cost is prohibitive at the affected scale. |
| Re-process existing records after extraction logic changes | `reingest_from_s3.py --county <name>` | Queries `documents` table — only works when records already exist |
| Initial population of a county that has S3 data but no DB records | `rebuild_db.py --county <name>` | Discovers documents directly from S3 keys — does not require pre-existing DB records. The Python script's default already preserves existing data; no flag is needed. |
| Full database rebuild from scratch | `rebuild_db.py --reset` | `--reset` is opt-in and truncates derived tables before re-processing everything from S3. |

### One-off / permanent script convention

Every top-level `scripts/*.py` file (excluding the `archive/`, `eval/`, `tests/`, and `spotcheck/` subdirectories) must carry exactly one of these headers in the first 50 lines:

- `# one-off: true` — finite-lifetime script (backfill, cleanup, migration, fixup). Candidate for archival to `scripts/archive/` once its work is done.
- `# permanent: true` — re-runnable utility (parameterizable, idempotent, intended to be invoked repeatedly). Exempt from one-off nagging and staleness checks.

The marker makes scripts programmatically classifiable. The `/audit` skill (§1.9) computes a self-adjusting threshold of `permanent_count + 5` from `scripts/check-script-headers.py --count` output — a new permanent utility landing raises the ceiling automatically, while a new one-off consumes a slot of headroom. When the total exceeds that threshold, the audit files a chore issue listing the unarchived one-off scripts as archival candidates. See #2533 (original convention) and #2547 (extension to all scripts + self-adjusting threshold) for background.

One-off scripts that have been run and verified should be moved to `scripts/archive/` to keep the directory manageable.

```python
#!/usr/bin/env python3
"""Backfill missing party names for Santa Barbara rulings."""
# venv: scraper-framework
# one-off: true
```

```python
#!/usr/bin/env python3
"""Query the dev DB and print row counts per table."""
# venv: scraper-framework
# permanent: true
```

The CI `script-headers-check` job and `.githooks/pre-push` both run `scripts/check-script-headers.sh`, which fails closed on any unmarked top-level script. Check marker counts locally with `scripts/check-script-headers.py --count`.

## Secrets Retrieval

Use `scripts/with-secret.sh` — never run `aws secretsmanager get-secret-value` as a standalone command (the secret value will appear in chat output). Never write secrets to disk. Instead, use the wrapper script to inject secrets as env vars:

```
scripts/with-secret.sh -e CF_API_TOKEN=judgemind/cloudflare/api-token -- terraform apply
scripts/with-secret.sh -e DB_USER=judgemind/dev/db/connection:.username -e DB_PASS=judgemind/dev/db/connection:.password -- ./run.sh
```

The `-e VAR=secret-id` form uses the raw SecretString. The `-e VAR=secret-id:.field` form extracts a JSON key. Multiple `-e` flags can be chained.

## Dev admin account (screenshot + auth-gated flows)

`scripts/screenshot.py --auth` logs into `dev.judgemind.org` using credentials stored in AWS Secrets Manager at `judgemind/dev/agent-admin`. The account has `users.role = 'admin'` on the dev database, so admin-gated pages (e.g. `/admin/data-quality`, `/admin/dispatcher`) render with full admin content rather than the "Access Denied" / 404 fallback. See `.claude/skills/screenshot/SKILL.md` for usage.

The secret's `email` and `password` keys are used by the script; any other consumer needing authenticated dev access should fetch them via `scripts/with-secret.sh`:

```
scripts/with-secret.sh \
    -e AGENT_EMAIL=judgemind/dev/agent-admin:.email \
    -e AGENT_PASSWORD=judgemind/dev/agent-admin:.password \
    -- <command>
```

To rotate the password, generate a new strong random value, update both `public.users.password_hash` on dev (via `scripts/dev-db-query.sh --rw`, using a bcrypt cost-12 hash — see `packages/api/src/auth/passwords.ts`) and the `password` field in the Secrets Manager entry. The existing `email` should stay stable unless you are also rotating the account identity.
