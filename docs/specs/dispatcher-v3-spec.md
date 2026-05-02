# Dispatcher v3 — Just `/task` in ECS

**Status:** Draft / design
**Authors:** Claude + Drew (2026-04-29 architecture review, revised 2026-04-30 after adversarial review)
**Authority:** v2 (`docs/specs/dispatcher-v2-spec.md`) remains authoritative in production. v3 is a greenfield replacement, built alongside, cut over via a single config flip.

---

## 1. Thesis

The v2 phase orchestrator was a reasonable bet at the time. In retrospect, it pays full structural cost (~33K LOC daemon + 5,857 lines of bash + a parallel set of failure terminals between two execution modes) for benefits that either (a) `/task` already provides internally — context isolation via ralph's fresh-context subagents, fix-CI hop, retry-on-CI-red — or (b) almost never fire usefully in practice — mid-pipeline crash resume, per-phase model selection, granular phase observability.

v3 is the design we'd write knowing what v2 taught us: **run `/task` end-to-end as one ECS task per issue, with a tiny launcher around it.** No phase orchestrator. No state machine over `dispatcher.agents.phase`. No two-implementation drift. No `next_directive` or `recommendation.action` enums. No 5,857-line bash entrypoint.

This is also enabled by something v2 didn't have: **1M-token context windows on the current Claude models** (Sonnet 4.5+, Opus 4.5+). v2's per-phase split was driven by the 200K window of its era — keeping each phase under ~21% of 200K (Spike 0.3, #2685) was a real constraint. At 1M, the whole pipeline including ralph's worker+reviewer subagent summaries and a long `gh run watch` tail fits comfortably with headroom. The phase-isolation-for-context justification for v2's architecture is gone.

When we want phase-level granularity for a specific failure mode, we add it as a one-off (a separate ECS task for verify, say) — not as a unifying architecture.

## 2. Goals

- **Continuous operation** off-laptop, surviving redeploys, sleep, reboots.
- **Single source of truth for orchestration logic** — and that source is `/task`'s SKILL.md, not a Python state machine.
- **Operator visibility** sufficient for triage. Not a real-time phase tooltip; "which agents are running and what's their latest stdout" is enough.
- **Self-healing on common failures** via `/task`'s own retry logic + an on-failure diagnoser invocation.
- **Cheap to maintain.** Target: <1,500 LOC of dispatcher-side Python, all in one package.

## 3. Non-goals

- Per-phase ECS tasks. One ECS task per issue, end-to-end.
- Phase-level state in Postgres (`dispatcher.agents.phase`, `phase_transitions`, `phase_attempts` — none of these exist in v3).
- Phase-level retry budgets. `/task` retries internally; if it exits non-zero, the whole task is the unit of retry.
- A daemon-resident orchestrator. The "daemon" is a thin scheduler.
- Mid-pipeline resume after daemon crash. Daemon crashes are rare and don't affect in-flight ECS tasks (they keep running).
- **Per-phase liveness machinery.** No phase-aware stuck-timeout, no per-phase "agent has been on step X for N minutes" check, no DB column the daemon polls to detect "agent disagrees with what phase I think it's in." v3 keeps two coarse liveness signals only: a wall-clock cap (default 6h, §11 OQ#4) and a session-log silent-hang detector (no log growth in 30min, §4.1) — both read external signals (ECS task state, CloudWatch Logs `lastEventTimestamp`), neither requires the daemon to know `/task`'s internal state. The v2-shape `agent_silent_hang` and `stuck_timeout` machinery still applies in spirit (Anthropic-side hangs and network blips affect `claude -p` regardless of who owns the phase loop), but v3 satisfies it with two simple checks rather than five overlapping ones.
- Shadow-mode parallel runs (v2 §6b's "second runner executes the same phase in parallel with output discarded"), multi-tenant. Same as v2 non-goals.
- Sub-1M-token context runners. The design assumes the runner provides ≥1M tokens of context (Claude Sonnet/Opus 4.5+ era). If a future runner ships with a smaller window we'd need to re-introduce phase-style splits — explicitly out of scope for v3 v1.

(Multi-runner support is **out of scope for v3 v1** but kept as a future direction — see §12. The entrypoint shape doesn't preclude it.)

## 4. Components

Three independently-deployable pieces. Each is small enough for one engineer to hold in their head.

### 4.1 Launcher (`dispatcher` ECS service, single replica)

The only long-lived process. Loop runs every 30s:

1. **Consume commands.** `dispatcher.commands` poll-consumed: `start`, `stop`, `pause`, `force_kill <agent_id>`, `set_cap <n>`. Same control plane as v2.
2. **Heartbeat.** UPDATE `dispatcher.runs.heartbeat_ts`. CloudWatch alarm pages if stale > 5min.
3. **Watch in-flight ECS tasks.** For each row in `dispatcher.agents` with `task_arn IS NOT NULL AND ended_at IS NULL`, `ecs:DescribeTasks` and check session-log progress:
   - `RUNNING` and session log growing → continue.
   - `RUNNING` and session log size unchanged for `silent_hang_minutes` (default 30) → `ecs:StopTask`, mark `failed` with `exit_reason='silent_hang'`, launch diagnoser. The session log size is read from CloudWatch Logs (`DescribeLogStreams` returns `lastEventTimestamp`) — no S3 round-trip per tick.
   - `STOPPED, exit 0` → mark `succeeded`.
   - `STOPPED, exit non-zero` → mark `failed`, capture `stoppedReason`, launch the diagnoser ECS task (§4.2).
4. **Claim if cap allows.** If `count(running) < concurrency_cap`, scan `gh issue list --label agent/ready --state open`, pick one trusted issue (author trust check via `scripts/check-issue-author.sh`), check **per-issue claim budget** (`SELECT count(*) FROM agents WHERE issue_number = $1` < `claim_attempts_max`, default 3), then `ecs:RunTask` against the `task-runner` task definition with `TASK_ISSUE_NUMBER=<n>` and `AGENT_ID=<uuid>` env. Issues that have hit the claim budget get `status/needs-human` + Telegram-alert and are skipped. Atomic claim sequence: add `status/in-progress` label → INSERT agents row (`status='claiming'`) → remove `agent/ready` → `ecs:RunTask` → UPDATE agents row with `task_arn`, `status='running'`. Failure mid-sequence reconciles on the next tick.
5. **Circuit breaker.** Rolling 2-of-3 in last 1h on terminal outcomes; if tripped, set `concurrency_cap=0` and Telegram-alert.

That's the entire launcher. Target ~700 LOC.

#### Task-runner entrypoint

The entrypoint is ~50 lines of Python (`scripts/dispatcher/agent_runner.py`), not bash. The agent task image already has the `dispatcher` Python package installed for the launcher; the entrypoint reuses it. The deliberate choice here is to **structurally preclude the v2 drift pattern** — v2's bash entrypoint started small and grew to 5,857 lines because each "one more case arm" was invisibly cheap. Python doesn't have bash's "just shell out" gravity, and a Python module is testable in CI from day one.

```python
#!/usr/bin/env python3
"""Task-runner entrypoint. Invokes /task via the configured runner,
streams stdout to a session log, archives to S3 on exit."""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path
import boto3

from dispatcher.runners import build_argv  # §12 — single source of truth

AGENT_ID        = os.environ["AGENT_ID"]
ISSUE_NUMBER    = os.environ["TASK_ISSUE_NUMBER"]
RUNNER          = os.environ.get("RUNNER", "claude")
SESSIONS_BUCKET = os.environ["SESSIONS_BUCKET"]
SESSION_FILE    = Path(f"/tmp/session-{AGENT_ID}.jsonl")


def upload_archive() -> None:
    if not SESSION_FILE.exists():
        return
    try:
        boto3.client("s3").upload_file(
            str(SESSION_FILE), SESSIONS_BUCKET, f"{AGENT_ID}.jsonl"
        )
    except Exception as exc:                       # noqa: BLE001 — best-effort
        print(f"session-archive-upload-failed: {exc}", file=sys.stderr)


def main() -> int:
    argv = build_argv(RUNNER, ISSUE_NUMBER, AGENT_ID)
    with SESSION_FILE.open("wb") as log:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        for chunk in proc.stdout:
            sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
            log.write(chunk); log.flush()
        return proc.wait()


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        upload_archive()
    sys.exit(rc)
```

`build_argv` (§12) returns the runner-specific argv list — Claude's `claude -p -w "/task #N" --output-format stream-json --include-partial-messages` for v3 v1, with the multi-runner cases pre-declared. `-w` (worktree) gives `/task` the auto-created worktree it expects, the same execution surface `/task` already runs in autonomously today via Agent-tool spawn.

The single small `/task` SKILL.md edit needed: read `AGENT_ID` from env if set, otherwise fall back to the existing cwd-derived id.

#### Session capture: CloudWatch primary (raw), S3 archive (compact)

Two captures, two purposes:

1. **CloudWatch Logs** receives the raw stream-json via the `awslogs` log driver in real time. This is the **liveness signal** the silent-hang detector reads (via `DescribeLogStreams.lastEventTimestamp`). It survives SIGKILL/OOM/spot-reclaim because the ECS log driver ships incrementally. CloudWatch's per-event size (256KB) and per-batch (1MB) limits can drop large tool-result events under burst, so it is **not** the diagnoser's primary read.
2. **S3 archive** receives a *compact transcript* (≈20:1 compression vs raw stream-json, per the `judgemind-transcripts` repo's measured ratios — e.g. 41.9MB jsonl → 2.2MB txt). The entrypoint runs `judgemind-transcripts/render-transcript.py` on EXIT to produce the compact form, then uploads both the raw jsonl (cheap; cold-reads possible) and the rendered transcript. A 200MB raw session becomes a ~10MB transcript that the diagnoser can read in one shot. The diagnoser's default read is the compact transcript; raw jsonl is the fallback.

Diagnoser fallback order: compact S3 transcript → raw S3 jsonl → CloudWatch `GetLogEvents`. Each is a complete-enough source of truth; the cascade exists because the EXIT trap can fail (SIGKILL bypass), in which case CloudWatch is the only survivor and may be lossy under burst.

### 4.2 Diagnoser (one-shot ECS task, on-demand)

When a `task-runner` task exits non-zero (or is killed by silent-hang detection), the launcher spawns a `diagnoser` task with `AGENT_ID=<uuid>`. The diagnoser task runs `claude -p "/diagnose-failure $AGENT_ID"` against the same image. The diagnoser:

- **Reads the full `/task` session.** Tries `s3://<sessions-bucket>/<agent_id>.jsonl` first; if missing or truncated (SIGKILL/OOM bypassed the EXIT trap), falls back to CloudWatch Logs (`GetLogEvents` against the task's log stream). Either way the diagnoser gets the stream-json transcript: every tool call, file read, gh response, and subagent message `/task` produced. The diagnoser sees what `/task` saw.
- Reads the agent's row + the issue body/comments + the PR (if any) for current state — the session is a snapshot at exit time and may be stale on those.
- Has full `gh` authority. Decides: post comment + re-add `agent/ready` to retry (subject to the per-issue claim budget; §4.1), file follow-up issue, mark `status/needs-human`, or close `status/invalid`.
- Performs side effects directly. Writes `agents.outcome_summary` for the audit trail. Exits.

#### Diagnoser failure handling

The diagnoser is itself an ECS task that can fail. Cap is **1 diagnoser per agent_id** — if the diagnoser exits non-zero, OOMs, or is reclaimed:

- The launcher marks the agent `status='needs_review'` and Telegram-alerts.
- The next claim of the same issue (subject to §4.1's per-issue budget) bypasses the diagnoser entirely on its first failure — there's no recursion of "diagnose the diagnoser."

Force-kill semantics: `force_kill <agent_id>` cascades to the diagnoser if one is running (`ecs:StopTask` on both `task_arn` and `diagnoser_arn`). This prevents an orphan diagnoser from running after the operator has already decided.

No `recommendation.action` enum. No `next_directive`. No `_consume_action_*` methods. The diagnoser is an authority, not an advisor — same position as v2-spec's empowered-diagnoser direction (#3366), but applied cleanly: there's no orchestrator to advise.

To retry an agent, the diagnoser re-adds `agent/ready`. The launcher's next tick claims it like any other ready issue. That's the entire retry loop.

### 4.3 Progress milestones (best-effort observation, optional)

To give the cockpit a sense of "where is this agent right now" without reintroducing a phase state machine, `/task` calls a tiny helper at each natural milestone. The helper does one UPDATE on the agent row.

```bash
# scripts/dispatcher/progress.sh — three lines that matter
psql "$DATABASE_URL" -c "UPDATE dispatcher.agents
  SET current_milestone = '$2',
      current_milestone_detail = '${3:-}',
      current_milestone_at = now()
  WHERE agent_id = '$1';" || true
```

`/task`'s SKILL.md gets a one-liner: "After each natural step, call `scripts/dispatcher/progress.sh $AGENT_ID <milestone> [detail]`." Suggested milestones (not enforced): `planning`, `ralph`, `summary`, `push_and_pr`, `awaiting_ci`, `fix_ci`, `merge`, `awaiting_deploy`, `verify`, `retro`. `/task` is free to invent new ones (e.g. `ralph_iter_3`); the cockpit displays whatever string is there.

Three properties keep this safe:

1. **Write-only from `/task`'s perspective.** The launcher never reads `current_milestone`. It does not branch on it, time-out off it, or compare it to a state machine. Milestones are observation, not control.
2. **Cockpit-only consumer.** The admin page reads `current_milestone` for display ("planning since 14:23"); nothing else does.
3. **Forgetting is benign on both ends.** If `/task` skips or mis-spells a milestone, the cockpit shows a stale or unfamiliar value — no incident, no failure routing, no recovery code path. The cockpit UI is built to be tolerant: a stale `current_milestone_at` renders as "last seen at <milestone> N min ago"; an unrecognized milestone string renders as the raw string. The agent's `status` (running vs succeeded vs failed) is unaffected by milestone state in either direction.

A helper-script failure (DB unreachable, network blip) returns 0 and logs to stderr; it does not abort `/task`. This is **explicitly best-effort on both write and read.**

### 4.4 Scheduled skills (EventBridge cron rules)

`audit`, `spotcheck`, `dispatcher-audit`, `dispatcher-daily-report` become EventBridge cron rules that target a `scheduled-skill` task definition. Each rule has its own schedule expression (e.g. `rate(6 hours)` for `dispatcher-audit`, `cron(0 12 * * ? *)` for `dispatcher-daily-report`). The task entrypoint is `claude -p "/$SKILL_NAME"`.

The launcher is **not involved**. EventBridge fires; ECS runs the task; the skill writes its results (issues filed, PR opened, etc.) directly via `gh`. No orchestration, no state.

The "every N merges" trigger for `audit` (#3723's pattern) becomes a tiny lambda or a recurring scheduled-skill check that compares `last_audit_pr_merged_at` against the count of merges since.

## 5. State Model

```sql
dispatcher.runs (
  run_id uuid PK,
  started_at, stopped_at, version_sha, host, pid, heartbeat_ts
);

dispatcher.agents (
  agent_id uuid PK,
  issue_number int,
  task_arn text,                       -- ECS task running /task
  diagnoser_arn text,                  -- ECS task running /diagnose-failure (if any)
  session_s3_key text,                 -- s3://<bucket>/<agent_id>.jsonl, written by task-runner on exit
  status text NOT NULL,                -- claiming, running, succeeded, failed, needs_review
  started_at, ended_at,
  exit_code int,
  exit_reason text,                    -- ECS stoppedReason
  pr_number int,                       -- written by /task; populated post-merge
  outcome_summary text,                -- written by /task or diagnoser; one-line for the cockpit
  current_milestone text,              -- best-effort, written by /task via progress.sh; cockpit-only
  current_milestone_detail text,       -- optional free-text detail, e.g. "ralph iter 3"
  current_milestone_at timestamptz,    -- last update; cockpit shows "N min ago"
  parent_run_id uuid REF runs
);
-- Per-issue claim budget query (no separate column; derived):
--   SELECT count(*) FROM agents WHERE issue_number = $1
-- vs dispatcher.config.claim_attempts_max (default 3).

dispatcher.commands (
  command_id bigserial PK,
  command text, payload jsonb,
  issued_by text, issued_at, consumed_at
);

dispatcher.config (
  key text PK, value text, updated_at, updated_by
);
-- Keys: concurrency_cap, breaker_window_minutes, breaker_threshold,
--       launcher_image_digest (pinned for in-flight task launches).

dispatcher.terminal_outcomes (   -- rolling window for breaker
  agent_id uuid, status text, ts timestamptz
);
```

That's the entire schema. **Five tables.** v2 has 11. The agents row has 11 columns; v2 has ~20.

What's not here, deliberately:
- `phase`, `phase_transitions`, `phase_attempts`, `phase_outputs` — `/task` doesn't expose phases to the dispatcher.
- `failures`, `retry_markers`, `diagnoses` (with `recommendation`/`next_directive`) — failures are an `agents.status='failed' + diagnoser_arn` pair; retry is the diagnoser re-adding `agent/ready`.
- `execution_mode`, `agent_task_arn` snapshots, `merge_unstick_attempts`, `merge_conflict_attempts`, `retries_used` — `/task` handles all of these internally.

## 6. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ ECS Service: judgemind-dispatcher-v3-dev (single replica)    │
│   Launcher loop (30s):                                       │
│   - poll commands, queue, in-flight tasks                    │
│   - claim ready issue → ecs:RunTask /task                    │
│   - on task exit nonzero → ecs:RunTask /diagnose-failure     │
│   ~600 LOC Python                                            │
└────────────┬────────────────────────────────────────────────┘
             │ ecs:RunTask (image digest pinned)
             ▼
   ┌──────────────────────┐  ┌──────────────────────┐
   │ task-runner task     │  │ diagnoser task       │
   │  claude -p '/task #N'│  │  claude -p '/diagnose│
   │  Runs end-to-end:    │  │   -failure $ID'      │
   │  plan → ralph →      │  │  Reads context, takes│
   │  push → PR → CI →    │  │  side effects, exits.│
   │  merge → verify →    │  │                      │
   │  retro → cleanup     │  │                      │
   └──────────────────────┘  └──────────────────────┘

   ┌──────────────────────────────────────────────────┐
   │ EventBridge cron rules (independent of launcher) │
   │  rate(6 hours) → dispatcher-audit task           │
   │  cron(0 12 ...) → dispatcher-daily-report task   │
   │  rate(1 day)  → spotcheck task                   │
   └──────────────────────────────────────────────────┘

   ┌─────────────┐
   │ RDS (5 tbls)│ ← all three components write directly
   └─────────────┘
```

One image. Three task definitions (launcher, task-runner, diagnoser) all baked from the same image with different entrypoints. Scheduled-skill is a fourth task def or argv variant.

## 7. What we deliberately give up vs v2

| Capability | v2 has it | v3 trade |
|---|---|---|
| Per-phase admin tooltip ("currently in ralph iter 3") | yes (state-machine-driven) | best-effort milestone, written by `/task` via `progress.sh` (§4.3); cockpit tolerates stale or unrecognized values |
| Per-phase retry budget by category | yes | `/task` is the retry unit; if it fails, diagnoser decides |
| Per-phase model selection (Haiku for verify) | yes | uniform model per `/task` invocation |
| Phase-level diagnoser routing (`fix_ci_blocked` vs `verify_failed`) | yes | diagnoser sees the whole agent context and decides |
| Mid-pipeline crash resume | yes | full restart from `agent/ready`; ralph patches still persist via #3026 |
| 1500+ test cases asserting per-phase behavior | yes | `/task`'s own SKILL contract is the test surface |

If any of these turn out to be load-bearing in operation, we add them back as targeted features — not as a unifying architecture.

## 8. Cohabitation with v2 — parallel operation

v2 and v3 run **in parallel** during v3's bringup. v2 is the proven workhorse and keeps shipping at full cap; v3 ramps from cap=1 alongside v2, proving itself on a fraction of the queue while v2 carries the rest. This preserves throughput during the ramp (vs a single-flag flip that would sacrifice 95% of greens/24h while v3 climbs from cap=1) and gives a continuous rollback knob (set v3 cap=0 at any time; v2 keeps running).

### 8.1 What's shared, what's separate

- **Shared:** Postgres schema (`dispatcher.*`), the `agent/ready` GitHub label queue, the issue-author trust check, the `status/in-progress` claim interlock, the `dispatcher.commands` control plane.
- **Separate:** ECS services (`judgemind-dispatcher-dev` for v2; `judgemind-dispatcher-v3-dev` for v3), task definitions, log groups, breakers (each daemon scopes to its own outcomes), concurrency caps, run-history tables (v3 adds `runs.dispatcher_version`).
- **Per-row ownership:** every `dispatcher.agents` row carries `parent_run_id`, which points to a row in `dispatcher.runs` whose `dispatcher_version` is either `'v2'` or `'v3'`. Each daemon filters its recovery + breaker logic to its own rows.

### 8.2 The v2 changes required (three small PRs)

These land **in v2** before v3 deploys, and they're non-breaking when v3 isn't running:

1. **Scope recovery loops to own runs.** `_resurrect_orphan_pr_agents`, `_reap_completed_agent_tasks`, `recover_abandoned_agents`, and the boot-time stale-claim sweep currently scan all `dispatcher.agents` rows. Add a filter: `WHERE parent_run_id IN (SELECT run_id FROM runs WHERE dispatcher_version = 'v2')`. Without this, v2 sees a v3-owned `failed` row with `pr_number` set and tries to resurrect a PR v3's diagnoser already handled.
2. **Scope breaker to own outcomes.** Add `dispatcher.terminal_outcomes.parent_run_id` (nullable, backfilled to v2 for existing rows). v2's `_evaluate_circuit_breaker` filters to v2 outcomes; v3's breaker filters to v3 outcomes. Without this, a v3 failure cluster during ramp trips v2's cap.
3. **Recognize a `dispatcher/v3-only` label as a skip filter.** During ramp, operators can mark specific issues for v3 by adding `dispatcher/v3-only`; v2's claim step skips them. v3 has no equivalent label (claims any ready issue not in v2's hands) — but if we want to bound v3 claims similarly during early smoke, a `dispatcher/v2-only` label works the same way. Optional but useful for forced routing during the cap=1 smoke.

The cross-daemon claim race is already handled: both daemons add `status/in-progress` before removing `agent/ready`, atomically. Whichever wins the label flip owns the issue.

### 8.3 Schema strategy

- v3 adds: `runs.dispatcher_version`, `terminal_outcomes.parent_run_id`, plus the v3-only columns on `agents` (`current_milestone`, `current_milestone_at`, `session_s3_key`, `diagnoser_arn`, `outcome_summary`).
- v2 keeps writing to: `phase_transitions`, `retry_markers`, `failures`, `diagnoses`, `phase_outputs`, `phase_attempts` (if it existed), and the v2-specific columns on `agents` (`phase`, `retries_used`, `merge_unstick_attempts`, `merge_conflict_attempts`, `execution_mode`, `agent_task_arn`).
- v3 doesn't read any v2-only column. v2 doesn't read any v3-only column. Cross-daemon writes are scoped by `parent_run_id`.

Cleanup migration runs **after** v2 is retired (§9 step 7), not during cohabitation. No destructive schema changes happen while both daemons are alive.

## 9. Cutover plan — gradual ramp

1. **Land v2 awareness PRs** (the three small edits in §8.2). v2 keeps running at its current cap throughout this work; the changes are non-breaking until v3 actually deploys.
2. **Build v3.** Launcher + entrypoint + diagnoser path + EventBridge crons. Lands as a stack of PRs. v3's ECS service deploys at cap=0 — present but not claiming.
3. **Smoke v3 at cap=1, v2 at current cap.** Operator adds `dispatcher/v3-only` to one trusted issue, sets v3 `concurrency_cap=1`. v3 claims that one issue, runs `/task` end-to-end, ships it. v2 keeps shipping the rest. Verify the merged PR + verify-evidence comment + retro all look right. Total throughput ≈ v2's current rate + 1.
4. **Ramp v3 up, v2 down in lockstep.** Each step: increase v3 cap by 2, decrease v2 cap by 2. Watch v3's greens and mean wall-clock vs v2's baseline. If v3 lags, hold or roll back the step. If v3 matches or exceeds, proceed: v3=4 / v2=cap-4, then 6/cap-6, then 8/cap-8 (assuming a target combined cap of ~8). Soak each step until the operator is satisfied — there's no fixed duration; the gate is "v3's outcomes at this cap look as good as v2's, across enough merges to be confident."
5. **v2 cap=0 (passive).** v2 is still up — its `dispatcher.commands` control plane handles `force_kill` and ad-hoc commands on its remaining historical agents — but it doesn't claim new work. v3 is at full cap and shipping the entire queue.
6. **Soak v3-only.** Run cap=0/full-v3 until the operator is satisfied. The gate is "v3 has exercised the rare paths" — at minimum: a fix-CI hop, a diagnoser invocation, a verify failure post-merge, every scheduled-skill cron, a force_kill, a circuit-breaker trip-and-reset. Until those paths have fired and behaved correctly, v2 stays online as the rollback button.
7. **Stop v2 service.** Scale `judgemind-dispatcher-dev` to 0. Keep its task def + image for one redeploy-back rollback button. v3 is sole.
8. **Cleanup migration.** Drop v2-only tables (`phase_transitions`, `retry_markers`, `failures`, `diagnoses`, `phase_outputs`) and v2-only columns on `agents`. Remove v2's three awareness edits (the `parent_run_id` filters become unconditional once there's only one version).

Rollback at any step before step 7: lower v3 cap, raise v2 cap. Both daemons keep working. No state surgery, no schema reverts. Step 7 introduces a slower rollback (re-scale v2 service back to 1), but by step 7 v3 has demonstrated the full failure surface at full cap.

A failed v3 agent during steps 3–6 doesn't poison the v2 queue — its row carries `parent_run_id` from a v3 run, so v2's recovery logic ignores it. The v3 diagnoser handles it the same way it would post-cutover.

## 10. IAM & secrets

The agent skills running inside ECS tasks (`task-runner`, `diagnoser`, `scheduled-skill`) get **dev-account admin authority** — not principle-of-least-privilege scope. Two reasons:

1. The whole point of `/task` is solving an arbitrary developer-authored task. We cannot enumerate in advance whether this issue needs S3 writes, RDS queries, ECS one-offs, CloudWatch tail, or Secrets Manager reads. v2's operator-laptop pattern (operator's AWS creds via `aws-vault`) gave `/task` whatever the operator had — in practice dev-admin. v3 preserves that.
2. `/task` already calls `scripts/with-secret.sh`, `scripts/ecs-run-task.sh`, `scripts/dev-db-query.sh`, `scripts/rebuild_db.sh`, etc. (see `docs/agent/infrastructure-reference.md`). Those scripts assume dev-account authority. Stripping it means a list of skills that can't run, which is the opposite of "v3 reuses `/task` as-is."

Task roles:

| Task def | Scope |
|---|---|
| `launcher` | Narrow scheduler scope: `ecs:RunTask` on the agent task-defs, `ecs:DescribeTasks`, `ecs:StopTask`, `iam:PassRole` for the agent task role, RDS connect to `dispatcher.*` only, Secrets Manager read for `TELEGRAM_BOT_TOKEN`. The launcher is not an agent. |
| `task-runner`, `diagnoser`, `scheduled-skill` | **Same role**: dev-admin. Full S3, full RDS (all schemas), `ecs:RunTask` (so the agent can launch its own one-offs), CloudWatch Logs read/write, Secrets Manager read for dev secrets, ECR pull, `aws ecs execute-command` (debugging via #3145 pattern), `gh` PAT via Secrets Manager. The diagnoser may want to run a debug script; a scheduled-skill audit may want to query the DB; `/task` itself may need to rebuild data. They all have the same authority because the worst case is the same. |

Production accounts are **not** in scope. The trust policy on the agent task role excludes assuming any prod-account role. This matches the CLAUDE.md rule "Never deploy to production."

**The actual security gate is the issue-author trust check** (§4.1, `scripts/check-issue-author.sh`). Because the agent can do anything in dev, the question "should this agent run at all" must be answered *before* `ecs:RunTask` fires. Untrusted issue authors are filtered out at the launcher's claim step — same gate v2 has.

**Trade-off explicitly accepted.** v2 had a multi-phase pipeline where the plan phase produced a written plan distinct from the AC, and the ralph reviewer adversarially reviewed *code* diffs before SHIP. v3 collapses everything into one continuous `/task` invocation, which means destructive shell side effects (`rebuild_db.sh`, `ecs-run-task.sh`, etc.) happen inside `/task` rather than gated by ralph's reviewer. We accept this because: (a) legitimate workflows do require destructive actions — wholesale data rebuilds in dev are normal; (b) the trust check + per-issue claim budget bound the blast radius; (c) `/task`'s own ralph + summary review still gates *code* changes, which is where most regressions originate. If a class of destructive-action incident emerges in production, we add a skill-layer gate at that point — not preemptively.

**Secrets in the session log.** Secrets injected via Secrets Manager → ECS env vars don't appear in the session log unless a skill explicitly prints them. `scripts/with-secret.sh` confines secret values to the subshell that needs them. The session log is captured to S3, so a leaked secret would persist there — the sessions bucket has private IAM-only access by default, and operators treat session logs as sensitive (same handling as CloudWatch Logs in dev).

## 11. Open questions

1. **Session-log retention and size.** A long ralph run can produce a stream-json transcript of 50–200 MB. CloudWatch Logs retention default 30 days; S3 archive default 90 days, parameterized per `dispatcher.config.session_retention_days`. The diagnoser may want to summarize-then-discard for very large sessions, but that's an in-skill concern, not infra.
2. **EventBridge vs a tiny scheduler in the launcher.** EventBridge gives outright separation of concerns and zero state coupling, at the cost of one more thing to operate. For four crons, EventBridge is cheaper to operate than a scheduler-in-Python. Default to EventBridge; revisit if a skill needs operator intervention beyond cron.
3. **Hooks (`PreToolUse`, `SubagentStop`, etc.) inside `/task`'s ECS task.** v2 has hooks writing to `dispatcher.failures` to catch sub-task signals. v3 doesn't have `dispatcher.failures`; the same signals are present in the session log (every tool call is in stream-json), so the diagnoser sees them naturally. **Exception:** the gh-rate-budget hook stays as a *block* (exits non-zero so the tool call is denied locally) — not as a `dispatcher.*` write. It's a circuit breaker, not observation, and lives entirely inside the task-runner.
4. **The `/task` ECS task wall-clock cap.** Wall-clock cap is the *coarse* liveness check (silent-hang detection in §4.1 is the fine-grained one). Pick a generous wall-clock — 6h covers virtually every real `/task` run including a long ralph + fix-CI cycle. Configure as ECS task `stopTimeout` plus a launcher-side deadline check. If 6h proves wrong, raise it. Silent-hang threshold (default 30min no log growth) is the more typical kill path.
5. **Trust check — at queue claim or at `/task` start?** Today the launcher does it before `RunTask`. Keep that; it's cheap and the gate is critical.
6. **`/task` SKILL.md AGENT_ID env-var read.** One-line edit: read `AGENT_ID` from env if set, else fall back to cwd-derived id. Land this edit in v2's `/task` SKILL.md before v3 cutover so v2 still works (env var unset → cwd derivation, same as today).

---

## 12. Future direction: multi-runner support

The v2 spec §6b made a strong case for runner-agnostic LLM invocation: comparing cost/quality across Claude, Gemini, OpenCode, and Cursor, hedging against single-vendor outages, picking the right model per task. v2 designed a `Runner` Protocol but never implemented anything beyond `ClaudeRunner`. v3 v1 stays single-runner (`claude -p -w`) but the design choices preserve the option.

What stays runner-shaped:

- **The entrypoint is one Python module.** Adding a runner is one entry in `dispatcher.runners.RUNNERS`, not a refactor:

  ```python
  # scripts/dispatcher/runners.py — single source of truth for runner argv.
  import os
  import sys
  from typing import Callable

  RUNNERS: dict[str, Callable[[str, str], list[str]]] = {
      # IMPORTANT: --worktree takes an OPTIONAL value, so passing it as
      # `-w "<prompt>"` causes the prompt to be consumed as the worktree
      # name (verified empirically — see the launch experiment for #3835).
      # Always use --worktree=NAME form, with the prompt as the trailing
      # positional argument.
      "claude":   lambda n, agent_id: [
          "claude", "-p", f"--worktree=agent-{agent_id}",
          "--output-format", "stream-json", "--include-partial-messages",
          f"/task #{n}",
      ],
      "gemini":   lambda n, agent_id: ["gemini", "-p",
                                        f"Run /task for issue #{n}",
                                        "--output-format", "stream-json"],
      "opencode": lambda n, agent_id: ["timeout", "6h", "opencode", "run",
                                        f"Run /task for issue #{n}"],
      "cursor":   lambda n, agent_id: ["timeout", "6h", "cursor-agent", "-p",
                                        f"Run /task for issue #{n}",
                                        "-m", os.environ.get("MODEL", "")],
  }


  def build_argv(runner: str, issue_number: str, agent_id: str) -> list[str]:
      if runner not in RUNNERS:
          sys.exit(f"unknown runner: {runner}")
      return RUNNERS[runner](issue_number, agent_id)
  ```

  Each entry is one line. The dict is unit-testable. Per-runner exit-code mapping (Gemini's distinct codes 0/1/41/53 vs Claude's exit-1-for-everything) lives in a sibling `EXIT_CLASSIFIERS` dict when added, not scattered through bash regex.

- **Skills already work cross-tool.** `/task` lives at `.claude/skills/task/SKILL.md`. Symlink `.agents/skills/task/` → that directory and Gemini + OpenCode pick it up natively (the `agentskills.io` standard). OpenCode also reads `.claude/skills/` directly. Cursor 2.4+ supports SKILL.md.

- **Hooks differ per runner.** v2 §6b enumerated the differences (`PreToolUse` vs `BeforeTool`, tool-name namespaces, etc.). The single hook v3 keeps as a *block* (gh-rate-budget, §11 OQ#3) needs a per-runner adapter. Defer until a second runner actually lands.

- **Per-task runner selection.** `dispatcher.config.runner` (default `claude`) sets the global default. Per-issue override via a label (e.g. `runner/gemini`) read by the launcher at claim-time and passed as env to the task-runner.

What's not yet specified (do this work when adding the second runner, not before):

- Auth secrets for non-Anthropic APIs (`GEMINI_API_KEY`, OpenAI/xAI keys, Cursor session tokens).
- Per-runner exit-code mapping (Gemini's distinct codes 0/1/41/53 vs Claude's exit-1-for-everything; v2 §8 third-line stderr-regex classifier).
- Hook adapters and skill-frontmatter sanitization (drop `allowed-tools` and `model` keys that non-Claude runners don't recognize).
- Session-log format normalization — `claude -p` stream-json, `gemini -p --output-format stream-json`, and OpenCode's run output are similar enough that the diagnoser reads them all, but each has its own envelope. A small parser-shim normalizes; defer until needed.

The point is the design doesn't *block* multi-runner. When there's a measured cost/quality reason to add Gemini or OpenCode (or to hedge against an Anthropic outage), the work is bounded: an entrypoint case-arm, a skill-symlink, a per-runner exit-code table, and a hook adapter.

---

**Reviewer prompt:** the v2 → v3 retrospective concluded that almost everything the v2 orchestrator does is duplicated by `/task`'s own logic, and what isn't duplicated is rarely useful in practice. Stress-test that claim. For each v2 mechanism not present in v3, ask: is `/task` actually doing this internally, or did v3 forget? Flag the latter.
