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

**Dev apply is automated by the dispatcher.** When the dispatcher merges a PR that touches `infra/terraform/`, it automatically runs `terraform apply` for the dev environment. The dispatcher detects infra PRs by checking changed file paths, determines which environments need an apply, and handles init/plan/apply inline. See `.claude/skills/dispatcher/SKILL.md` "Auto-apply dev terraform" for the full procedure.

**If the dispatcher is not running** (e.g., during interactive sessions), the subagent that authored the PR must apply to dev manually:
```
terraform -chdir=$REPO_ROOT/infra/terraform/environments/dev apply -target=module.<module_name> -auto-approve
```
Verify the apply succeeds. If it fails, file a `priority/p1` issue.

**For DNS/hosting environments** that require the Cloudflare API token:
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

## Dispatcher v2 Cutover

The dispatcher v2 daemon is an opt-in production replacement for the laptop-dispatcher's `/dispatcher` skill. It runs on Fargate, claims `agent/ready` issues from GitHub, and drives each one end-to-end through `claude -p '/task-v2-*'` subprocesses (plan → ralph → summary → push → CI watch → merge → deploy watch → verify → retro → cleanup). Phase 3 (#2782) shipped all the orchestration code; Phase 3E (#2798) was the final code piece. **The cutover from `concurrency_cap=0` (cold) to `concurrency_cap=1` (one in-flight agent) is an explicit operator action — not part of any PR.**

Spec: `docs/specs/dispatcher-v2-spec.md` §6 (state machine), §8 (failure taxonomy + diagnoser), §15 (Phase 3 gate).

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

The daemon validates phase strings via the `PHASE_*` constants — adding a new phase value requires a code change but no migration. See `scripts/dispatcher/daemon.py` constants section for the canonical list.

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
4. If you *must* run rebuild with aggressive concurrency on dev, drop `--concurrency` to match the headroom (e.g. `--concurrency 32` leaves ~100 connections free for other callers).

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
3. **Stop the task** — sends SIGTERM then SIGKILL. The MCP `StopTask` is gated behind Phase B (`ALLOW_WRITE`), so this step stays on the CLI: `aws ecs stop-task --cluster judgemind-dev --task <arn> --reason "zombie retry-loop (#2572)" --region us-west-2`.
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
