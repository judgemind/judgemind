# AWS API access — MCP vs `aws` CLI vs `scripts/ecs-*.sh`

> **When to read this:** you are writing or editing a skill, agent doc, or CLAUDE.md section that interacts with AWS (ECS, CloudWatch Logs, S3, Secrets Manager) and need to choose between an `mcp__awslabs_*` MCP tool, the `aws` CLI, and the existing `scripts/ecs-*.sh` wrappers.
>
> **TL;DR:** prefer MCP for ad-hoc structured reads (ECS DescribeServices/DescribeTasks/ListTasks, CloudWatch Logs Insights queries). Keep the `scripts/ecs-*.sh` wrappers for the full launch-and-stream-logs workflow. Keep the `aws` CLI for writes, for live log tailing, and for anything in the Gap rows of `docs/agent/aws-to-mcp-migration.md`.

## Why this doc exists

Agents historically used the `aws` CLI for every AWS operation, which produced a steady stream of friction:

- `aws ecs describe-tasks --cluster ... --tasks ...` requires `--query` JMESPath plus `--output json` plus shell-quoted JSON to extract a single field.
- `aws logs filter-log-events` and `aws logs start-query` need millisecond-since-epoch timestamps that have to be computed in a separate tool call (no `$()`).
- The combination of mixed-quote shell escaping and JSON CLI args trips the platform's safety hooks frequently.

The two AWS Labs MCP servers expose the underlying AWS APIs as typed tools — region, service identifiers, and JSON params are passed structurally and the response is parsed JSON. For ad-hoc reads from inside a Claude Code session this is a clear win.

## What is installed (Phase A — read-only)

Two MCP servers are configured in `~/.claude.json` at `local` scope:

- **`awslabs.cloudwatch-mcp-server`** — actively maintained by AWS Labs. Covers Logs Insights queries, log-group discovery, metric data, alarm history. Does **not** cover live-tail (`FilterLogEvents` streaming).
- **`awslabs.ecs-mcp-server`** — flagged by its own README as "legacy, no more updates" but the `ecs_resource_management` dispatcher is a thin boto3 shim over the ECS API. Configured with `ALLOW_WRITE` unset (read-only) — Phase A constrains us to `Describe*`, `List*`, and `Get*` calls.

Both servers use ambient AWS credentials via boto3 — same `~/.aws/credentials` or env vars as the `aws` CLI uses today. Phase B (issue filed, blocked by Phase A) will introduce a scoped `iam_agent` Terraform role and flip `ALLOW_WRITE=true` once the IAM boundary is in place.

## Decision rule

| Situation | Use | Why |
|---|---|---|
| Look up an ECS service's running task count, deployment status, or task definition revision | **MCP** (`mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "DescribeServices"`) | One typed call, no `--query`/`-o json`. |
| List currently running tasks for a cluster (e.g. spot-check for runaway oneshots) | **MCP** (`ecs_resource_management` with `api_operation: "ListTasks"` then `"DescribeTasks"`) | Same — one call per step, structured response. |
| Check the last-status of a oneshot task you launched (poll until STOPPED) | **MCP** (`ecs_resource_management` with `api_operation: "DescribeTasks"`) | Cleaner than `aws ecs describe-tasks --query 'tasks[0].lastStatus'`. |
| Run a Logs Insights query against an ECS log group (e.g. count errors in last 30 min, find recent successful processing line) | **MCP** (`mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query`) | Returns parsed result rows; no millisecond-epoch math, no JSON parsing. |
| Discover available log groups before querying | **MCP** (`mcp__awslabs_cloudwatch-mcp-server__describe_log_groups`) | |
| Inspect active CloudWatch alarms or alarm history | **MCP** (`get_active_alarms`, `get_alarm_history`) | |
| **Launch a oneshot Fargate task and stream its logs to the agent's terminal** | **`scripts/ecs-run-task.sh`** | The script handles task-definition resolution, network config, log-stream waiting, and exit-code propagation. The MCP `RunTask` exists but is gated behind Phase B (`ALLOW_WRITE`) and would not include the stream-logs convenience. |
| Quick SQL against the dev DB (SELECT, EXPLAIN) | **`scripts/dev-db-query.sh`** | Uses interactive ECS Exec via `session-manager-plugin`. MCP cannot replicate the interactive SSM stream. |
| Interactive shell into the ingestion worker for ad-hoc debugging | **`scripts/ecs-run.sh`** | Same — interactive Exec is not MCP-replicable. |
| Live tail (`tail -f`) of an ECS log group | **`scripts/ecs-logs.sh --follow`** | CloudWatch MCP has no streaming tool; polling-based Insights queries would add latency and per-query cost. |
| Force a service redeploy (new task definition, kick the deployment) | **`scripts/ecs-redeploy.sh`** | Wraps `RegisterTaskDefinition` + `UpdateService` and waits for rollout. MCP write path is Phase B. |
| Download an object from S3 (e.g. spotcheck artifacts, raw PDFs) | **`aws s3 cp`** | Neither MCP server covers S3. The `aws-api-mcp-server` (generic CLI wrapper) is being held until Phase B. |
| Read a Secrets Manager secret value into an env var | **`scripts/with-secret.sh`** | Wraps `aws secretsmanager get-secret-value` and pipes into the child process's env without writing the value to disk or to chat output. Never call `aws secretsmanager get-secret-value` directly. |
| Anything inside `scripts/`, `.github/workflows/`, or `.githooks/` | **`aws` CLI** | MCP runs inside Claude Code and is not reachable from shell scripts that execute outside the agent context. |

## The write-path note (read this before adopting MCP for anything that mutates AWS)

`awslabs.ecs-mcp-server` is configured with `ALLOW_WRITE` unset — the server refuses all mutating operations (`RunTask`, `RegisterTaskDefinition`, `UpdateService`, `StopTask`, `CreateService`, `DeleteService`). This is intentional: in Phase A the server uses ambient credentials, which today means full admin in the dev account. Without an IAM boundary, an LLM-driven write call could in principle modify production resources.

Phase B (follow-up issue, blocked by the Phase A issue) will:

1. Define a new `iam_agent` Terraform module with a scoped role: `ecs:Run/Describe/ListTask*`, `logs:Get/Describe/FilterLogEvents`, `s3:GetObject/PutObject/DeleteObject/ListBucket` on staging prefixes only, `secretsmanager:GetSecretValue` on `judgemind/*`, `iam:PassRole` narrow to scraper/maintenance task roles.
2. Configure both MCP servers to assume the role via `AWS_PROFILE` with `role_arn` + `source_profile`.
3. Flip `ALLOW_WRITE=true` on the ECS MCP server once the IAM boundary is in place.
4. Migrate the small set of write callsites in skills (e.g. ad-hoc `RunTask` for a quick verification job) to MCP.

Until Phase B lands: writes stay on `aws` CLI and the `scripts/ecs-*.sh` wrappers. The wrappers will not migrate even after Phase B — they encapsulate non-MCP value (network config, log streaming, exit-code propagation, deployment rollout waits).

## CLI relaunch requirement

Same gotcha as the GitHub MCP server (#2658): when `~/.claude.json` is edited to add or change an `mcpServers` entry, **subagents launched before the relaunch will not see the new tools**. The dispatcher and any in-flight `/task` subagents must be restarted after the edit. The smoke-test pattern is:

1. Edit `~/.claude.json`.
2. Quit and relaunch the CLI.
3. Run a CLI-level smoke test (`ToolSearch query="select:mcp__awslabs_ecs-mcp-server__ecs_resource_management"` then a `DescribeServices` call against the dev cluster).
4. Spawn a minimal subagent that does the same call, to confirm the tools propagated to the subagent context too.

## How to load a deferred MCP tool

MCP tools are **deferred** — they appear as names in the subagent's tool registry but the JSON schema is not loaded until you ask for it. Before the first use in a session, call `ToolSearch`:

```
ToolSearch query="select:mcp__awslabs_ecs-mcp-server__ecs_resource_management" max_results=1
```

Multiple tools can be loaded at once by comma-separating in the `select:` query. Common bundles:

- ECS reads: `select:mcp__awslabs_ecs-mcp-server__ecs_resource_management`
- CloudWatch reads: `select:mcp__awslabs_cloudwatch-mcp-server__describe_log_groups,mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query,mcp__awslabs_cloudwatch-mcp-server__get_logs_insight_query_results`

After the schema is loaded, the tool is callable like any other function for the rest of the session.

**Tool-name gotcha.** The MCP server segment uses **hyphens** (`awslabs_cloudwatch-mcp-server`, `awslabs_ecs-mcp-server`), not underscores. Tool names downstream of the server segment use underscores normally (`describe_log_groups`, `execute_log_insights_query`). Always copy the full hyphenated form when writing a `ToolSearch` query.

## Quoting / escaping

MCP call arguments are JSON-structured — no shell involved. An ECS `api_params` object is just a JSON literal. CloudWatch Insights queries are passed as a native `query_string` field — no need to escape quotes for the shell or to compute millisecond-epoch timestamps from `date -d '30 minutes ago'`. The MCP server handles `start_time` / `end_time` as ISO-8601 strings or relative offsets depending on the tool.

## Tool-by-tool mapping

For the full inventory of every `aws` subcommand referenced in agent-facing skills/docs and its MCP counterpart (including the gaps that stay on CLI/script), see `docs/agent/aws-to-mcp-migration.md`.
