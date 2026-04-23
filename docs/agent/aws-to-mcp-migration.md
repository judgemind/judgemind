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
| `aws ecs stop-task --cluster ... --task <arn>` | `ecs_resource_management` with `api_operation: "StopTask"` | **Gap (write — Phase B)** | ECS MCP server has `ALLOW_WRITE` unset in Phase A. Keep `aws ecs stop-task` until Phase B IAM scoping lands. |
| `aws ecs run-task --cluster ... --task-definition ...` | `ecs_resource_management` with `api_operation: "RunTask"` | **Gap (write — Phase B)** | Same — write-gated. The full launch-and-stream-logs workflow stays on `scripts/ecs-run-task.sh` even after Phase B (script provides network config, log streaming, exit-code propagation). |
| `aws ecs register-task-definition` / `aws ecs update-service` | `ecs_resource_management` with `api_operation: "RegisterTaskDefinition"` / `"UpdateService"` | **Gap (write — Phase B)** | Stay on `scripts/ecs-redeploy.sh` for service redeploys (handles rollout-wait). |
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

## Write-path status — read this before migrating writes

`awslabs.ecs-mcp-server` is configured at `local` scope with `ALLOW_WRITE` unset. Live smoke test from a `/task` subagent on 2026-04-18:

- `mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `DescribeServices` — works (returns full descriptor for `judgemind-ingestion-worker-dev`: ACTIVE, 1/1 running, td rev 492).
- The same tool with `RunTask` / `RegisterTaskDefinition` / `UpdateService` / `StopTask` would be refused by the server's allow-list — these are the operations that Phase B will unlock once the IAM boundary is in place.
- `mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query` against `/ecs/judgemind-ingestion-worker-dev` — works.

Until Phase B lands: agents can only use MCP for reads. Writes stay on `aws` CLI and the `scripts/ecs-*.sh` wrappers. **Do not mass-migrate write callsites in this PR** — they would all fail.

The Phase B follow-up issue is filed and `Blocked by` the Phase A issue. Once Phase B lands, write calls will be migrated incrementally.

## Migration plan (two phases)

### Phase A (this PR) — MCP-first for ECS and CloudWatch reads, docs establish the direction

1. Add `docs/agent/aws-api-access.md` with the decision rule: **prefer MCP for ad-hoc reads (ECS DescribeServices/DescribeTasks/ListTasks, CloudWatch Logs Insights), keep `scripts/ecs-*.sh` wrappers for full launch-and-stream workflows, keep `aws` CLI for writes (until Phase B), live log tailing, S3, secrets, and anything inside `scripts/`/`.github/`.**
2. Update skills so the verification-table references that today say "check ECS logs via `scripts/ecs-logs.sh ...`" mention the MCP equivalent first for ad-hoc Insights queries (the script stays as-is for the live-tail / convenience path).
3. Leave write calls (RunTask, UpdateService, StopTask) on `aws` CLI / `scripts/ecs-*.sh` with a pointer to the Phase B follow-up. Mark them explicitly "MCP write path blocked on Phase B".
4. Update `CLAUDE.md` to point at the new doc rather than duplicating the decision rule.

### Phase B (follow-up issue, blocked by Phase A)

1. Define `iam_agent` Terraform module: scoped role with `ecs:Run/Describe/ListTask*`, `logs:Get/Describe/FilterLogEvents`, `s3:GetObject/PutObject/DeleteObject/ListBucket` on staging prefixes only, `secretsmanager:GetSecretValue` on `judgemind/*`, `iam:PassRole` narrow to scraper/maintenance task roles.
2. Configure both MCP servers to assume the role via `AWS_PROFILE` with `role_arn` + `source_profile`.
3. Flip `ALLOW_WRITE=true` on the ECS MCP server.
4. Migrate write callsites: ad-hoc `RunTask` invocations from skills can move to MCP. The `scripts/ecs-*.sh` wrappers stay — they encapsulate non-MCP value (network config, log streaming, exit-code propagation, deployment rollout waits).
5. Consider adopting `awslabs.aws-api-mcp-server` (generic CLI wrapper) for S3 and Secrets Manager reads now that the IAM boundary is enforced.

## Verification

After this PR:

```
grep -rnE 'aws (ecs|logs|s3|secretsmanager|cloudwatch) ' .claude/skills/ docs/agent/ CLAUDE.md
```

Should show only out-of-scope operations (S3 download in spotcheck, secrets retrieval examples, the zombie-task runbook in `infrastructure-reference.md` which keeps `aws ecs list-tasks` / `stop-task` as the recovery path) plus the pointer references in `docs/agent/aws-api-access.md` and this file (which intentionally contain the old patterns as examples).

Write operations (`aws ecs run-task` via `scripts/ecs-run-task.sh`, `aws ecs update-service` via `scripts/ecs-redeploy.sh`, `aws s3 cp`, `aws secretsmanager get-secret-value` via `scripts/with-secret.sh`) still appear — that is the intended Phase A state.
