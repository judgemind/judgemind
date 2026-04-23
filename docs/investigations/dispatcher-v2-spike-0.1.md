# Dispatcher v2 Spike 0.1 — `claude -p` end-to-end on Fargate

**Status:** Complete — verdict: **GO**
**Issue:** #2683
**Spec:** `docs/specs/dispatcher-v2-spec.md` §15 (spike 0.1), §17 Open Questions 1 + 2

## Summary

Ran a minimal `@anthropic-ai/claude-code` 2.1.114 image on ECS Fargate in the dev
account (`155326049300`, `us-west-2`) through four scenarios: `success`,
`turn_limit`, `auth_fail`, and `mcp_probe`. All four tasks booted, reached
`RUNNING`, executed `claude -p`, and exited cleanly with distinguishable
outputs. The per-scenario summary lives in `dispatcher_spike.runs` on the
dev RDS instance.

This spike GATES spikes 0.2, 0.3, 0.4, and 0.7. The verdict is **go** — the
entire dispatcher v2 daemon architecture (§4, §6, §14 of the spec) is viable
on Fargate as specified, with a small caveat on exit-code classification
(see Finding 2 below).

## How the spike was run

- Image: `Dockerfile.dispatcher-spike` (repo root). Built from `node:20-slim`
  with `@anthropic-ai/claude-code@latest` globally installed. Deliberately
  NOT layered on top of the scraper-framework image — the spike is about
  proving `claude -p` can run under Fargate, not validating any coexistence
  with Playwright/Chromium baggage.
- Terraform module: `infra/terraform/modules/dispatcher-spike/` — 8 resources
  (ECR repo, lifecycle policy, CloudWatch log group, task/exec IAM roles,
  one secrets-read inline policy, the managed-policy attachment, task
  definition).
- Wrapper: `scripts/dispatcher-spike/run_fargate_claude_p.sh` launches one
  task per invocation via `aws ecs run-task`, passes the scenario as a
  container command override, polls `describe-tasks` until `STOPPED`, pulls
  the CloudWatch log tail, and writes a `dispatcher_spike.runs` row.
- Container entrypoint: `scripts/dispatcher-spike/container-entry.sh` picks
  a prompt per scenario and invokes `claude -p --max-turns <N>
  --mcp-config /etc/dispatcher-spike/mcp-config.json -- <prompt>`.

All 4 scenarios ran twice (once to validate the wrapper fix, once to persist
to Postgres). Total cold-start-to-STOPPED wall-clock is consistently ~60s
per task (15s PROVISIONING, 15s PENDING, 10-15s RUNNING, 15s DEPROVISIONING).

## Findings (the 5 bullets §15 asks for)

### 1. Did the task launch successfully? YES.

All 8 Fargate tasks (4 scenarios × 2 runs) reached `RUNNING` and
`EssentialContainerExited`.  Wall-clock from `aws ecs run-task` to
`STOPPED` was ~60s for every run. Representative task ARNs:

- success:    `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/a36c0a1d422f4b6193e7bace1433be13`
- turn_limit: `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/420877835155401bbd4be8028d54ff29`
- auth_fail:  `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/cff3676692ba4cafaf39f195d70e16b9`
- mcp_probe:  `arn:aws:ecs:us-west-2:155326049300:task/judgemind-dev/6cd1452cd7a64426b522f46f548e41e6`

No IAM permission issues. The task execution role picked up
`ANTHROPIC_API_KEY` from Secrets Manager (`judgemind/anthropic/api-key`)
correctly. No `$HOME` permission surprises — uid 1100 (custom `spike` user;
the default `node` user at uid 1000 on `node:20-slim` is reserved, so we
used 1100) writes `~/.claude/` without issue.

### 2. Exit code on a successful `claude -p`: **0**.

CloudWatch log tail for a canonical success run:
```
[dispatcher-spike] scenario=success
[dispatcher-spike] whoami=spike home=/home/spike pwd=/home/spike
[dispatcher-spike] node=v20.20.2 claude=/usr/local/bin/claude
[dispatcher-spike] invoking: claude -p --max-turns 10 --mcp-config /etc/dispatcher-spike/mcp-config.json -- <prompt 55 chars>
OK
[dispatcher-spike] claude exited with code=0
```
`stoppedReason` from ECS: `Essential container in task exited`.

### 3. Exit code on a forced-failure (turn-limit) `claude -p`: **1**.

Using `--max-turns 1` on a prompt that requires at least 2 tool calls:
```
[dispatcher-spike] scenario=turn_limit
[dispatcher-spike] invoking: claude -p --max-turns 1 --mcp-config /etc/dispatcher-spike/mcp-config.json -- <prompt 108 chars>
[dispatcher-spike] claude exited with code=1
Error: Reached max turns (1)
```
Note: the error message on stdout is the authoritative classifier — the
exit code alone is ambiguous.

**Caveat — exit codes are NOT distinguishable between auth fail and
turn-limit.** Both return exit code 1. An auth-fail run (bogus
`ANTHROPIC_API_KEY` injected by the entry script for the `auth_fail`
scenario) also produces exit 1:
```
[dispatcher-spike] scenario=auth_fail
[dispatcher-spike] invoking: claude -p --max-turns 10 --mcp-config /etc/dispatcher-spike/mcp-config.json -- <prompt 14 chars>
Invalid API key · Fix external API key
[dispatcher-spike] claude exited with code=1
```

**Implication for the daemon spec (§7):** the "category" enum for
`dispatcher.failures` cannot be derived from exit code alone. The daemon
MUST also capture a stdout/stderr tail and classify via regex on the
first 200 chars. Specifically:
- `Error: Reached max turns` → `subprocess_turn_limit`
- `Invalid API key` / `401` → `subprocess_auth_fail`
- neither → `subprocess_unknown_failure`

This is a clarification of §7, not a change — the spec already says
"Failures are labeled by cheap deterministic signals (hooks, exit codes,
timeouts)". The hook-based categorization in §9 (`emit_failure.py` writing
directly to `dispatcher.failures`) remains the right answer for the
granular-category path. The wrapper-level fallback classification described
here kicks in only when the hook was never reached (e.g. auth failure
before the first tool call).

### 4. Were `github_*` MCP tools callable? YES — with explicit `--mcp-config`.

The `mcp_probe` scenario asks `claude -p` to enumerate every MCP tool it has
available. With `--mcp-config /etc/dispatcher-spike/mcp-config.json` where
the config declares only the `github` server, `claude -p` responds with the
full 26-tool `mcp__github__*` suite:
```
- mcp__github__add_issue_comment
- mcp__github__create_branch
- mcp__github__create_issue
- mcp__github__create_or_update_file
- mcp__github__create_pull_request
- mcp__github__create_pull_request_review
- mcp__github__create_repository
- mcp__github__fork_repository
- mcp__github__get_file_contents
- mcp__github__get_issue
- mcp__github__get_pull_request
- mcp__github__get_pull_request_comments
- mcp__github__get_pull_request_files
- mcp__github__get_pull_request_reviews
- mcp__github__get_pull_request_status
- mcp__github__list_commits
- mcp__github__list_issues
- mcp__github__list_pull_requests
- mcp__github__merge_pull_request
- mcp__github__push_files
- mcp__github__search_code
- mcp__github__search_issues
- mcp__github__search_repositories
- mcp__github__search_users
- mcp__github__update_issue
- mcp__github__update_pull_request_branch
```
Exit code: 0. This run **resolves Open Question 2 in §17** — MCP tool
propagation to `claude -p` subprocesses works exactly as expected when the
config is passed explicitly via `--mcp-config`. The failure mode observed
in #2656 (Agent-tool subagent with no MCP access) was specific to how the
Agent-tool sandbox propagates config; `claude -p` invocations spawned
directly by the daemon will have the MCP servers the daemon gives them,
no more, no less.

The spike's `mcp-config.json` references `${GITHUB_TOKEN}`, but the
container did NOT have `GITHUB_TOKEN` set (the spike's Terraform module
does not wire it in). The MCP server registers its tools at session start
regardless — actually *invoking* a `mcp__github__*` tool against a real
issue would have failed with a 401. The important thing the probe proved
is that (a) the CLI reads `--mcp-config`, (b) it starts the declared
server, and (c) the server's tools are visible to the session. Putting a
scoped GitHub PAT into a new Secrets Manager entry and wiring it into the
task's `secrets[]` list is mechanical plumbing for spike 0.7 or Phase 1.

### 5. Auth, `$HOME`, and IAM surprises: none that block Phase 1.

- **`$HOME`:** uid 1100 (`spike`) owns `/home/spike/.claude/`. Claude CLI
  creates the directory contents on first invocation without complaint.
  `node:20-slim` already reserves uid 1000 for `node` — using 1000 for the
  spike user fails with "UID 1000 is not unique"; 1100 avoids the
  collision.
- **Auth:** `ANTHROPIC_API_KEY` is injected by the ECS secrets path and
  works for the first `claude -p` call with no warm-up. No additional
  "device code" or browser auth step is triggered in `-p` mode.
- **IAM:** the execution role needs ONLY `ecs:TaskExecution` managed policy
  + a custom `secretsmanager:GetSecretValue` policy scoped to the secrets
  the task consumes. The task role (assumed by the container at runtime)
  needs no extra privileges for the spike — all AWS interactions happen
  from the wrapper script outside the container.
- **Networking:** we reused the ingestion worker's subnets + security
  group. Outbound 443 to `api.anthropic.com` works through the existing
  NAT gateway; no new VPC plumbing needed.
- **Variadic flag trap:** `claude -p --mcp-config <configs...>` is
  variadic, so `claude -p --mcp-config /path/to/config.json "prompt text"`
  silently sucks the prompt into the config list and errors with "MCP
  config file not found". Use `--` to terminate option parsing before the
  prompt: `claude -p ... --mcp-config /path/to/config.json -- "prompt"`.
  (Documented here because the daemon's `/task-v2-*` wrappers will hit
  this the first time they add another variadic flag.)

## Verdict: GO

Spike 0.1 passes every acceptance criterion on issue #2683. The daemon
architecture as specified is viable on Fargate. Proceed to spike 0.2
(Hook → Postgres from inside a `claude -p` subprocess) on the basis of
these findings.

## What this spike explicitly did NOT prove

- **Long runs.** Wall-clock here was ~60s per task. Open Question 1 (per-phase
  context budget) requires a real `/task-v2-*` skill payload, 45-90 min end-
  to-end, and token-usage telemetry. That is spike 0.3.
- **Concurrency.** We launched one task at a time. Spike 0.6 (worktree
  footprint at peak concurrency) is needed before we raise
  `config.concurrency_cap` beyond 1.
- **Actual MCP tool invocation.** The `mcp_probe` scenario enumerated tools;
  it did not call one. Spike 0.2 (hook-based `dispatcher.failures` writes)
  will incidentally prove the CLI can execute MCP tool calls.

## Follow-up issues filed

- **(will be filed after this investigation lands)** Cleanup spike 0.1
  infrastructure: delete the `dispatcher-spike` Terraform module, the
  Dockerfile, the `scripts/dispatcher-spike/` directory, the
  `dispatcher_spike` schema, and all ECR images tagged under
  `judgemind/dispatcher-spike`. The spike leaves these artifacts in place
  so reviewers of this finding can reproduce it; cleanup is a separate PR
  per the issue's explicit instructions.
- **(will be filed)** When spike 0.7 adds a scoped GitHub PAT secret for
  the daemon, wire it into this task's `secrets[]` in the spike module so
  `mcp_probe` can be extended to actually call `mcp__github__get_issue`
  against a `type/spike` test issue.
- **(will be filed)** Update the daemon spec §7 with the exit-code
  classification caveat from Finding 3: exit code alone is not enough to
  distinguish auth-fail from turn-limit from miscellaneous crashes. The
  daemon's `emit_failure.py` hook (§9) plus a wrapper-level stdout-regex
  fallback together give the needed granularity.

## Reproducing the spike

```
# Build + push image
docker build --platform linux/amd64 \
    -f Dockerfile.dispatcher-spike \
    -t judgemind/dispatcher-spike .
docker tag judgemind/dispatcher-spike \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest
docker push \
    155326049300.dkr.ecr.us-west-2.amazonaws.com/judgemind/dispatcher-spike:latest

# Apply Terraform (creates ECR repo, task def, IAM roles, log group)
terraform -chdir=infra/terraform/environments/dev \
    apply -target=module.dispatcher_spike -auto-approve

# Migration (the API deploy will run this automatically; manual fallback):
scripts/dev-db-query.sh --rw \
    "CREATE SCHEMA IF NOT EXISTS dispatcher_spike; CREATE TABLE IF NOT EXISTS dispatcher_spike.runs (...)"

# Run each scenario:
scripts/dispatcher-spike/run_fargate_claude_p.sh success
scripts/dispatcher-spike/run_fargate_claude_p.sh turn_limit
scripts/dispatcher-spike/run_fargate_claude_p.sh auth_fail
scripts/dispatcher-spike/run_fargate_claude_p.sh mcp_probe

# Inspect:
scripts/dev-db-query.sh \
    "SELECT run_id, scenario, exit_code, LEFT(stderr_tail, 120) FROM dispatcher_spike.runs ORDER BY started_at DESC LIMIT 10;"
```
