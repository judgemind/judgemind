# Dispatcher Execution Mode Flip Runbook

Operator guide for flipping `dispatcher.config.agent_execution_mode` between
`'ecs'` (default since #3093) and `'subprocess'` (legacy fallback).

The config row is the **only** lever for switching modes — no code deploy
required. The daemon reads the value at claim time and snapshots it onto
`dispatcher.agents.execution_mode` for each new agent; in-flight agents are
unaffected by a subsequent row flip.

---

## Background

| Mode | Behaviour |
| --- | --- |
| `'ecs'` (default) | Daemon calls `ecs:RunTask` to launch a per-agent Fargate task. The agent is independent of the daemon container — daemon redeploys do not kill it. |
| `'subprocess'` (legacy fallback) | Daemon forks the runner as a child process inside its own ECS task. Each daemon redeploy SIGKILLs all child processes, abandoning in-flight agents. |

The default was flipped to `'ecs'` in #3093 (Stage 4 of the #3086
per-agent-ECS migration). Stage 3 smoke (#3092) confirmed the ECS path
end-to-end before the flip.

---

## Pre-Flip Checklist (dev → ecs)

Before confirming `'ecs'` is the active mode on dev, verify:

1. **Smoke is green.** Stage 3 smoke run (#3092) passed end-to-end. Confirm
   via the `#3092` issue comments.

2. **Agent-runner image is up to date.** The ECR image for
   `judgemind-dispatcher-agent-runner-dev` must be at the current `main` HEAD.
   Check the last successful `build-agent-runner` CI run:

   ```bash
   gh run list --workflow build-agent-runner.yml --repo judgemind/judgemind \
     --limit 5 --json status,conclusion,headBranch,createdAt
   ```

3. **Fatal-event alarm is armed.** The `dispatcher-agent-runner-dev-agent-runner-fatal`
   CloudWatch alarm must exist and be in `OK` state:

   ```bash
   aws cloudwatch describe-alarms \
     --alarm-names dispatcher-agent-runner-dev-agent-runner-fatal \
     --region us-west-2
   ```

4. **No `daemon_restart_abandoned` in recent logs.** Stage 2 reaper
   (`recover_abandoned_agents`) must be handling ECS-mode rows correctly:

   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/judgemind-dispatcher-dev \
     --start-time "$(date -d '1 hour ago' +%s)000" \
     --filter-pattern '"daemon_restart_abandoned"' \
     --region us-west-2
   ```

---

## Dev Observation Window (post-flip)

After the default flips to `'ecs'` on dev (via #3093 merge + auto Terraform
apply), run a 24-hour capped observation sweep:

1. **Set cap=1** on the daemon so at most one new agent is claimed per tick
   (reduces blast radius during the soak):

   ```sql
   -- scripts/dev-db-query.sh
   SELECT * FROM dispatcher.config WHERE key = 'max_agents';
   -- If absent or > 1, insert/update:
   INSERT INTO dispatcher.config (key, value)
   VALUES ('max_agents', '1')
   ON CONFLICT (key) DO UPDATE SET value = '1';
   ```

2. **Watch execution_mode distribution** for newly-claimed agents:

   ```sql
   -- scripts/dev-db-query.sh
   SELECT execution_mode, count(*)
   FROM dispatcher.agents
   WHERE created_at > now() - interval '1 hour'
   GROUP BY 1;
   ```

   Expected after flip: `ecs | N` only, no `subprocess` rows (unless an
   operator has written a one-shot override row).

3. **Watch the fatal-event alarm** in CloudWatch:
   `dispatcher-agent-runner-dev-agent-runner-fatal`.

4. **Confirm no `daemon_restart_abandoned` events** over the soak window via
   CloudWatch Logs Insights:

   ```
   fields @timestamp, agent_id, issue_number
   | filter event = "daemon_restart_abandoned"
   | sort @timestamp desc
   | limit 20
   ```

   Query log group: `/ecs/judgemind-dispatcher-dev`.

---

## Production Cutover (human-gated)

The production flip is intentionally **not** a code-only change. An operator
writes the config row directly. This keeps prod cutover human-gated,
independent of the Stage 4 code PR.

1. Confirm dev observation window is clean (0 fatal events, 0
   `daemon_restart_abandoned`, `execution_mode='ecs'` dominant in the agents
   table).

2. Write the row on prod via `scripts/dev-db-query.sh` pointing at the
   production database (or via an ECS Exec session):

   ```sql
   INSERT INTO dispatcher.config (key, value)
   VALUES ('agent_execution_mode', 'ecs')
   ON CONFLICT (key) DO UPDATE SET value = 'ecs';
   ```

3. Verify the next agent claim picks up `execution_mode = 'ecs'`:

   ```sql
   SELECT agent_id, issue_number, execution_mode, created_at
   FROM dispatcher.agents
   ORDER BY created_at DESC
   LIMIT 5;
   ```

4. Watch the prod fatal-event alarm:
   `dispatcher-agent-runner-production-agent-runner-fatal`.

---

## Rollback

If fatal events or unexpected abandonment occur, flip back to `'subprocess'`
with a single `psql` write. No code deploy, no Terraform apply needed.

```sql
INSERT INTO dispatcher.config (key, value)
VALUES ('agent_execution_mode', 'subprocess')
ON CONFLICT (key) DO UPDATE SET value = 'subprocess';
```

New agents claimed after this write take the legacy subprocess path
(`_run_orchestration_phases`). In-flight ECS-mode agent tasks continue
running — they were already launched and their `execution_mode` row is
immutable.

To stop a specific in-flight ECS agent task:

```bash
aws ecs stop-task \
  --cluster judgemind-dev \
  --task <AGENT_TASK_ARN> \
  --reason "execution-mode rollback" \
  --region us-west-2
```

Retrieve the `agent_task_arn` from the `dispatcher.agents` table.

---

## Stage 5 Hygiene Items (deferred, not in this PR)

The following cleanup items are deferred to Stage 5 (not yet filed):

- Delete `'subprocess'` from `AGENT_EXECUTION_MODES` and remove
  `_run_orchestration_phases` from `daemon.py`.
- Flip the `dispatcher.agents.execution_mode` column DEFAULT from
  `'subprocess'` to `'ecs'` (or add a follow-up migration). The daemon
  specifies `execution_mode` explicitly at INSERT time so this column
  default is effectively dead code; it is a hygiene item, not a
  functional one.
- Document the full dispatcher CloudWatch alarm catalogue in
  `docs/agent/infrastructure-reference.md` once the agent-runner alarm
  from this PR is confirmed live on dev.
