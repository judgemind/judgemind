# `aws` to AWS MCP migration — audit table

> **Purpose:** concrete mapping of every `aws` subcommand currently referenced in agent-facing skills and docs to the corresponding `mcp__awslabs_*` tool (or a noted gap). Companion to `docs/agent/aws-api-access.md`, which explains the decision rules; this document is the exhaustive inventory.

## Scope of the audit

- **In scope:** every `aws` invocation inside agent-facing markdown — the SKILL.md files under `.claude/skills/` (task, dispatcher, audit, spotcheck), the root `CLAUDE.md`, and the per-topic references under `docs/agent/`.
- **Out of scope:** shell scripts under `scripts/` (the `scripts/ecs-*.sh` wrappers, `scripts/with-secret.sh`, etc.), GitHub Actions workflows under `.github/workflows/`, and `.githooks/`. MCP runs inside Claude Code and is not reachable from shell scripts that execute outside the agent context — same constraint as the GitHub MCP migration in `docs/agent/gh-to-mcp-migration.md`.

## Tool-by-tool mapping

| `aws` subcommand | MCP equivalent | Status | Notes |
|---|---|---|---|
| `aws ecs describe-services --cluster judgemind-dev --services <name>` | `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "DescribeServices"` | **Available** | Returns full service descriptor (`serviceName`, `desiredCount`, `runningCount`, `taskDefinition`, deployments). No `--query` / `--output json` shell juggling. |
| `aws ecs list-tasks --cluster judgemind-dev --desired-status RUNNING` | `ecs_resource_management` with `api_operation: "ListTasks"` | **Available** | Filter via `api_params.desiredStatus`. |
| `aws ecs describe-tasks --cluster judgemind-dev --tasks <arn>` | `ecs_resource_management` with `api_operation: "DescribeTasks"` | **Available** | Use after `ListTasks` to inspect `lastStatus`, `startedAt`, `containers[*].exitCode` for the zombie-oneshot runbook. |
| `aws ecs stop-task --cluster ... --task <arn>` | `ecs_resource_management` with `api_operation: "StopTask"` | **Available** | Phase B: `ALLOW_WRITE=true` and the `judgemind-agent-dev` role grants `ecs:DescribeTasks` (ListTasks + DescribeTasks needed to find the ARN first). Use MCP for ad-hoc stop; `aws ecs stop-task` remains as CLI fallback and in shell scripts. |
| `aws ecs run-task --cluster ... --task-definition ...` | `ecs_resource_management` with `api_operation: "RunTask"` | **Available** | Phase B: write path unblocked. Use MCP for ad-hoc one-shots where streamed logs are not needed. The full launch-and-stream-logs workflow stays on `scripts/ecs-run-task.sh` — the script provides network config, log-stream waiting, and exit-code propagation that MCP does not replicate. |
| `aws ecs register-task-definition` / `aws ecs update-service` | `ecs_resource_management` with `api_operation: "RegisterTaskDefinition"` / `"UpdateService"` | **Available** | Phase B: write path unblocked. Use MCP for ad-hoc one-shots. Stay on `scripts/ecs-redeploy.sh` for full service redeployments that need rollout-wait. |
| `aws logs describe-log-groups` | `mcp__awslabs_cloudwatch-mcp-server__describe_log_groups` | **Available** | |
| `aws logs start-query` + `aws logs get-query-results` (Logs Insights) | `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` (sync) and `get_logs_insight_query_results` (poll) | **Available** | Pass `query_string` directly — no shell-quoting the JMESPath. Time range as ISO-8601 / relative; no millisecond-epoch math. |
| `aws logs filter-log-events --start-time ... --filter-pattern ...` (one-shot non-streaming) | `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` with a `filter`-based query | **Available (use Insights)** | Insights is the supported path for ad-hoc filtering. No native `FilterLogEvents` tool. |
| `aws logs tail <log-group> --follow` (live streaming) | _(no MCP equivalent)_ | **Gap — stays on `scripts/ecs-logs.sh --follow`** | CloudWatch MCP has no streaming tool. Polling-based Insights queries would add latency and per-query cost. |
| `aws cloudwatch describe-alarms --state-value ALARM` | `mcp__awslabs_cloudwatch-mcp-server__get_active_alarms` | **Available** | |
| `aws cloudwatch describe-alarm-history` | `mcp__awslabs_cloudwatch-mcp-server__get_alarm_history` | **Available** | |
| `aws cloudwatch get-metric-data` | `mcp__awslabs_cloudwatch-mcp-server__get_metric_data` | **Available** | |
| `aws s3 cp s3://<bucket>/<key> <local>` | _(no MCP equivalent)_ | **Gap — stays on `aws` CLI** | Neither `awslabs.cloudwatch-mcp-server` nor `awslabs.ecs-mcp-server` covers S3. The generic `awslabs.aws-api-mcp-server` could fill this but is being held until the Phase B IAM boundary lands (it exposes the full AWS API surface — not safe with ambient admin credentials). |
| `aws s3 ls`, `aws s3 sync`, `aws s3 rm` | _(no MCP equivalent)_ | **Gap — stays on `aws` CLI** | Same — Phase B candidate via `aws-api-mcp-server` with IAM scoping. |
| `aws secretsmanager get-secret-value` | _(no MCP equivalent — and never call directly)_ | **Gap — stays on `scripts/with-secret.sh`** | The wrapper script pipes the secret into the child process's env without exposing it to disk or chat output. Direct CLI calls echo the secret to chat — never use them. |
| `aws ecs execute-command --interactive` (interactive ECS Exec via SSM) | _(no MCP equivalent)_ | **Gap — stays on `scripts/dev-db-query.sh` / `scripts/ecs-run.sh`** | Requires local `session-manager-plugin` to tunnel an SSM stdin/stdout stream. Neither MCP nor the AWS SDK can replicate the interactive stream. |
| `aws sts get-caller-identity`, `aws configure list` | _(no MCP equivalent)_ | **Gap — stays on `aws` CLI** | Auth-state introspection — not exposed by either MCP server. |
| Anything inside `scripts/`, `.github/workflows/`, or `.githooks/` | _(MCP unreachable)_ | **Gap — stays on `aws` CLI** | MCP runs inside Claude Code; subshells and CI runners cannot reach it. Same constraint as the GitHub MCP migration. |

## Write-path status — Phase B (current state)

`awslabs.ecs-mcp-server` is configured at `local` scope with `ALLOW_WRITE=true`. Both MCP servers assume the `judgemind-agent-dev` IAM role via `AWS_PROFILE=judgemind-agent`.

Phase A smoke test (2026-04-18):
- `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `DescribeServices` — worked (returned full descriptor for `judgemind-ingestion-worker-dev`: ACTIVE, 1/1 running, td rev 492).
- `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` against `/ecs/judgemind-ingestion-worker-dev` — worked.

Phase B smoke tests (run post-merge by operator — see issue #2697):
- Smoke test 1 (read): `DescribeServices` against `judgemind-ingestion-worker-dev` — confirm still works.
- Smoke test 2 (write): no-op `UpdateService` — confirm write path is unblocked.
- Smoke test 3 (negative): `DescribeServices` against a prod cluster — confirm IAM boundary holds.

`scripts/ecs-*.sh` wrappers are not removed — they encapsulate stream-logs propagation, network config, and rollout-wait logic that MCP tools do not replicate.

## Migration plan

### Phase A (complete) — MCP-first for ECS and CloudWatch reads

1. Added `docs/agent/aws-api-access.md` with the decision rule: **prefer MCP for ad-hoc reads (ECS DescribeServices/DescribeTasks/ListTasks, CloudWatch Logs Insights), keep `scripts/ecs-*.sh` wrappers for full launch-and-stream workflows, keep `aws` CLI for writes (until Phase B), live log tailing, S3, secrets, and anything inside `scripts/`/`.github/`.**
2. Updated skills so the verification-table references mention the MCP equivalent first for ad-hoc Insights queries (script stays for live-tail / convenience path).
3. Left write calls (RunTask, UpdateService, StopTask) on `aws` CLI / `scripts/ecs-*.sh` with a pointer to Phase B.
4. Updated `CLAUDE.md` to point at the new doc rather than duplicating the decision rule.

### Phase B (current) — scoped write access via `iam_agent` Terraform role

1. Defined `iam_agent` Terraform module (`infra/terraform/modules/iam_agent/`): scoped role with `ecs:Run/Describe/ListTask*`, `logs:Get/Describe/FilterLogEvents/StartQuery/GetQueryResults/StopQuery`, `s3:GetObject/PutObject/DeleteObject` on staging and spotcheck prefixes only, `secretsmanager:GetSecretValue` on `judgemind/*`, `iam:PassRole` narrow to scraper/maintenance task roles.
2. Wired into `environments/dev/main.tf`; role ARN exposed via `terraform output agent_role_arn`.
3. Operator configures `~/.aws/config` with `judgemind-agent` profile and updates `~/.claude.json` — see `docs/agent/aws-api-access.md` §"Operator setup".
4. `ALLOW_WRITE=true` is set on the ECS MCP server.
5. Ad-hoc write callsites (StopTask, RunTask, UpdateService) in skills can now use MCP. The `scripts/ecs-*.sh` wrappers stay — they encapsulate stream-logs propagation, network config, and rollout-wait logic.

### Phase C (follow-up) — `awslabs.aws-api-mcp-server` for S3 / Secrets Manager reads

`awslabs.aws-api-mcp-server` is deferred to Phase C. Even with the scoped IAM role, `aws-api-mcp-server` exposes the entire AWS API surface (including IAM list-write paths the current role does not grant). A Phase C should enumerate the specific S3/Secrets reads worth migrating before enabling it. Until then, S3 and Secrets Manager operations stay on `aws` CLI and `scripts/with-secret.sh`.

## Verification

After this PR:

```
grep -rnE 'aws (ecs|logs|s3|secretsmanager|cloudwatch) ' .claude/skills/ docs/agent/ CLAUDE.md
```

Should show only out-of-scope operations (S3 download in spotcheck, secrets retrieval examples, the zombie-task runbook in `infrastructure-reference.md` which keeps `aws ecs list-tasks` / `stop-task` as the recovery path) plus the pointer references in `docs/agent/aws-api-access.md` and this file (which intentionally contain the old patterns as examples).

Write operations (`aws ecs run-task` via `scripts/ecs-run-task.sh`, `aws ecs update-service` via `scripts/ecs-redeploy.sh`, `aws s3 cp`, `aws secretsmanager get-secret-value` via `scripts/with-secret.sh`) still appear — that is the intended Phase A state.
