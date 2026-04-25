# AWS API access — MCP vs `aws` CLI vs `scripts/ecs-*.sh`

> **When to read this:** you are writing or editing a skill, agent doc, or CLAUDE.md section that interacts with AWS (ECS, CloudWatch Logs, S3, Secrets Manager) and need to choose between an `mcp__awslabs_*` MCP tool, the `aws` CLI, and the existing `scripts/ecs-*.sh` wrappers.
>
> **TL;DR:** prefer MCP for all ad-hoc ECS and CloudWatch reads and writes (Phase B: `ALLOW_WRITE=true` is set, IAM role scoped to dev only). Keep the `scripts/ecs-*.sh` wrappers for the full launch-and-stream-logs and rollout-wait workflows. Keep the `aws` CLI for S3, secrets, live log tailing, and anything inside `scripts/`/`.github/`.

## Why this doc exists

Agents historically used the `aws` CLI for every AWS operation, which produced a steady stream of friction:

- `aws ecs describe-tasks --cluster ... --tasks ...` requires `--query` JMESPath plus `--output json` plus shell-quoted JSON to extract a single field.
- `aws logs filter-log-events` and `aws logs start-query` need millisecond-since-epoch timestamps that have to be computed in a separate tool call (no `$()`).
- The combination of mixed-quote shell escaping and JSON CLI args trips the platform's safety hooks frequently.

The two AWS Labs MCP servers expose the underlying AWS APIs as typed tools — region, service identifiers, and JSON params are passed structurally and the response is parsed JSON. For ad-hoc reads from inside a Claude Code session this is a clear win.

## What is installed (Phase B — scoped write access)

Two MCP servers are configured in `~/.claude.json` at `local` scope:

- **`awslabs.cloudwatch-mcp-server`** — actively maintained by AWS Labs. Covers Logs Insights queries, log-group discovery, metric data, alarm history. Does **not** cover live-tail (`FilterLogEvents` streaming).
- **`awslabs.ecs-mcp-server`** — flagged by its own README as "legacy, no more updates" but the `ecs_resource_management` dispatcher is a thin boto3 shim over the ECS API. Configured with `ALLOW_WRITE=true` — Phase B unlocks write operations (`RunTask`, `UpdateService`, `StopTask`, `RegisterTaskDefinition`) via the scoped `judgemind-agent-dev` IAM role.

Both servers use the `judgemind-agent` AWS profile (`AWS_PROFILE=judgemind-agent`), which assumes the `judgemind-agent-dev` IAM role via `sts:AssumeRole`. The role is scoped to dev-only resources — no prod cluster, no prod log groups, S3 limited to `staging/` and `spotcheck/` prefixes only.

## Operator setup

After `terraform apply` in `environments/dev`, run `terraform output agent_role_arn` to get the role ARN, then add this profile to `~/.aws/config`:

```ini
[profile judgemind-agent]
role_arn = <agent_role_arn from terraform output>
source_profile = default
region = us-west-2
```

Then add `AWS_PROFILE` and `ALLOW_WRITE` to the MCP server env blocks in `~/.claude.json`:

```json
{
  "mcpServers": {
    "awslabs.ecs-mcp-server": {
      "env": {
        "AWS_PROFILE": "judgemind-agent",
        "ALLOW_WRITE": "true"
      }
    },
    "awslabs.cloudwatch-mcp-server": {
      "env": {
        "AWS_PROFILE": "judgemind-agent"
      }
    }
  }
}
```

After editing `~/.claude.json`, quit and relaunch the CLI (see §"CLI relaunch requirement" below).

## Decision rule

| Situation | Use | Why |
|---|---|---|
| Look up an ECS service's running task count, deployment status, or task definition revision | **MCP** (`mcp__awslabs_ecs-mcp-server__ecs_resource_management` with `api_operation: "DescribeServices"`) | One typed call, no `--query`/`-o json`. |
| List currently running tasks for a cluster (e.g. spot-check for runaway oneshots) | **MCP** (`ecs_resource_management` with `api_operation: "ListTasks"` then `"DescribeTasks"`) | Same — one call per step, structured response. |
| Check the last-status of a oneshot task you launched (poll until STOPPED) | **MCP** (`ecs_resource_management` with `api_operation: "DescribeTasks"`) | Cleaner than `aws ecs describe-tasks --query 'tasks[0].lastStatus'`. |
| Run a Logs Insights query against an ECS log group (e.g. count errors in last 30 min, find recent successful processing line) | **MCP** (`mcp__awslabs_cloudwatch-mcp-server__execute_log_insights_query`) | Returns parsed result rows; no millisecond-epoch math, no JSON parsing. |
| Discover available log groups before querying | **MCP** (`mcp__awslabs_cloudwatch-mcp-server__describe_log_groups`) | |
| Inspect active CloudWatch alarms or alarm history | **MCP** (`get_active_alarms`, `get_alarm_history`) | |
| **Ad-hoc `RunTask` — launch a task without needing streamed logs** | **MCP** (`ecs_resource_management` with `api_operation: "RunTask"`) | Phase B: `ALLOW_WRITE=true` is set and the scoped role permits `ecs:RunTask` on the dev cluster. Use for quick one-shots where you'll poll `DescribeTasks` for status. |
| **Launch a oneshot Fargate task and stream its logs to the agent's terminal** | **`scripts/ecs-run-task.sh`** | The script handles task-definition resolution, network config, log-stream waiting, and exit-code propagation. MCP `RunTask` does not include the stream-logs convenience — script stays for all launch-and-stream-logs flows. |
| **Force a service redeploy (ad-hoc one-shot)** | **MCP** (`ecs_resource_management` with `api_operation: "UpdateService"`) | Phase B: `ALLOW_WRITE=true` is set. Use for quick ad-hoc redeployments; use `scripts/ecs-redeploy.sh` for rollout-wait flows. |
| Quick SQL against the dev DB (SELECT, EXPLAIN) | **`scripts/dev-db-query.sh`** | Uses interactive ECS Exec via `session-manager-plugin`. MCP cannot replicate the interactive SSM stream. |
| Interactive shell into the ingestion worker for ad-hoc debugging | **`scripts/ecs-run.sh`** | Same — interactive Exec is not MCP-replicable. |
| Live tail (`tail -f`) of an ECS log group | **`scripts/ecs-logs.sh --follow`** | CloudWatch MCP has no streaming tool; polling-based Insights queries would add latency and per-query cost. |
| Full service redeploy with rollout-wait (new task definition, kick the deployment) | **`scripts/ecs-redeploy.sh`** | Wraps `RegisterTaskDefinition` + `UpdateService` and waits for rollout; MCP has no rollout-wait convenience. |
| Download an object from S3 (e.g. spotcheck artifacts, raw PDFs) | **`aws s3 cp`** | Neither MCP server covers S3. `awslabs.aws-api-mcp-server` is deferred to Phase C — see §"Write-path note" below. |
| Read a Secrets Manager secret value into an env var | **`scripts/with-secret.sh`** | Wraps `aws secretsmanager get-secret-value` and pipes into the child process's env without writing the value to disk or to chat output. Never call `aws secretsmanager get-secret-value` directly. |
| Anything inside `scripts/`, `.github/workflows/`, or `.githooks/` | **`aws` CLI** | MCP runs inside Claude Code and is not reachable from shell scripts that execute outside the agent context. |

## The write-path note (Phase B state — scoped writes enabled)

`awslabs.ecs-mcp-server` is configured with `ALLOW_WRITE=true` (Phase B). The server now accepts mutating ECS operations (`RunTask`, `RegisterTaskDefinition`, `UpdateService`, `StopTask`). These are safe because the MCP servers assume the `judgemind-agent-dev` IAM role, which is scoped to:

- **ECS:** dev cluster only (`judgemind-dev`) — no prod cluster ARN granted
- **CloudWatch Logs:** `/ecs/judgemind-*-dev` groups only — prod groups (`*-production`) are not matched
- **S3:** `staging/*` and `spotcheck/*` prefixes only — no access to `raw/`, `derived/`, or the Terraform state bucket
- **Secrets Manager:** `judgemind/*` prefix only
- **IAM PassRole:** scraper role ARN only (maintenance role if wired)

**`awslabs.aws-api-mcp-server` is deferred to Phase C.** Even with the scoped IAM role in place, `aws-api-mcp-server` exposes the entire AWS API surface — including IAM list-write paths the current role does not grant. A Phase C follow-up should enumerate the specific S3/Secrets Manager reads worth migrating (e.g. `s3:GetObject` for spotcheck artifact download, `secretsmanager:GetSecretValue` for ad-hoc debugging) before enabling it. Until then, S3 and Secrets Manager operations stay on `aws` CLI and `scripts/with-secret.sh`.

The `scripts/ecs-*.sh` wrappers are **not removed** — they encapsulate non-MCP value (network config, log streaming, exit-code propagation, deployment rollout waits) that the MCP tools do not replicate.

## CLI relaunch requirement

Same gotcha as the GitHub MCP server (#2658): when `~/.claude.json` is edited to add or change an `mcpServers` entry, **subagents launched before the relaunch will not see the new tools**. The dispatcher and any in-flight `/task` subagents must be restarted after the edit. The smoke-test pattern is:

1. Edit `~/.claude.json` with `AWS_PROFILE=judgemind-agent` and `ALLOW_WRITE=true` per §"Operator setup" above.
2. Quit and relaunch the CLI.
3. **Smoke test 1 (read still works):** `ToolSearch query="select:mcp__awslabs_ecs-mcp-server__ecs_resource_management"` then `ecs_resource_management` with `api_operation: "DescribeServices"` against `judgemind-ingestion-worker-dev` — confirm it returns the service descriptor.
4. **Smoke test 2 (write unblocked):** `ecs_resource_management` with `api_operation: "UpdateService"` and a no-op payload (e.g. `forceNewDeployment: false` with the current task definition) — confirm it succeeds without an `ALLOW_WRITE` refusal error.
5. **Smoke test 3 (negative — prod boundary holds):** `ecs_resource_management` with `api_operation: "DescribeServices"` against a production cluster (e.g. `judgemind-prod`) — confirm the call is denied by IAM, not by the MCP server's `ALLOW_WRITE` guard. The IAM role's `Resource` list does not include any prod cluster ARN.
6. Spawn a minimal subagent that repeats smoke test 1, to confirm the tools propagated to the subagent context too.

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
