# Dispatcher `execution_mode` awareness audit — 2026-04

**Scope.** Every SQL read or write of `dispatcher.agents` in
`scripts/dispatcher/daemon.py` (as of commit pre-#3158). For each call
site: is the code path execution-mode-aware, or does it carry
subprocess-mode assumptions that silently mishandle ECS-mode agents?

**Motivation.** Three Option A rollout bugs all had the same shape — a
code path authored pre-ECS that assumed the agent was a local subprocess
under the daemon:

| # | Site | Bug |
|---|------|-----|
| #3091 | reap-tick path | early drafts routed STOPPED tasks through the subprocess crash handler; caught pre-merge |
| #3128-era | `_persist_agent_task_arn` | ECS launch didn't flip `execution_mode`, producing reconciliation ambiguity after daemon restart |
| #3152 | `recover_abandoned_agents` | startup sweep marked every in-flight ECS agent `daemon_restart_abandoned` on daemon redeploy |

The forward-scan audit below enumerates every `dispatcher.agents`
read/write and classifies each as **PASS** (mode-aware or
fundamentally mode-independent), **CONCERN** (silently assumes
subprocess semantics), or **BACKSTOP** (mode-agnostic by construction).

## Classification summary

- **PASS (mode-aware):** 4 sites — `recover_abandoned_agents`,
  `_reap_completed_agent_tasks`, `_launch_agent_ecs_task`,
  `_persist_agent_task_arn`.
- **BACKSTOP (phase-driven, mode-independent by construction):** most
  terminal writers (`_mark_agent_terminal`, `_write_merged_at`,
  `_write_verified_at`, `_write_retroed_at`, `_write_failure_summary`,
  `_restore_succeeded_and_advance_done`), phase read/write
  (`_update_agent_phase`), status read (`_observe_external_terminal`,
  `_read_agent_status_phase`, `_read_merged_at_and_verified_at`,
  `_read_verify_skip_reason`), claim INSERT (`_atomic_claim`), run
  counter (`_count_running_agents` / `_has_active_agent`), helper
  lookups (`_lookup_active_owner_kind`, `_lookup_issue_number_by_agent`,
  `_read_retries_used`), diagnoser context
  (`_build_diagnoser_context`), housekeeping retention sweeps.
- **CONCERN (this audit):** 3 sites fixed in this PR —
  `_check_stuck_agents`, `_resume_retrying_agent`,
  `_handle_force_stop` (per-agent branch).
- **CONCERN (tracked as follow-ups):** 3 sites filed as issues —
  diagnoser `_consume_action_*` family (terminal writes do not stop
  ECS task), `_advance_awaiting_ci` / `_run_fix_ci` (uses
  local worktree_path for fix-ci subprocess), agent-runner entrypoint
  (no external-terminal signal observation mid-run).

## Site-by-site table

Line numbers are against `scripts/dispatcher/daemon.py` at the audit
commit. "Fix" column: `pr-3158` = this PR, `follow-up #N` = filed as
separate issue, `n/a` = no action needed.

| Line | Call site | Code shape | Category | Disposition | Fix |
|------|-----------|------------|----------|-------------|-----|
| 2727 | `_handle_force_stop` SELECT `pid` | `SELECT pid FROM dispatcher.agents WHERE agent_id = %s` | CONCERN | For ECS agents `pid` is NULL on daemon host — the subsequent SIGKILL no-ops and the ECS task keeps running while the DB row reads `crashed`. | pr-3158 — stop the ECS task via `ecs:StopTask` before marking crashed |
| 2738 | `_handle_force_stop` UPDATE crashed | UPDATE status=crashed, ended_at=now() | BACKSTOP | Phase-based terminal write, correct for both modes (both branches need the row flipped). | n/a |
| 2814 | `_handle_retry` SELECT status | `SELECT status, retries_used FROM dispatcher.agents WHERE agent_id = %s` | BACKSTOP | Operator-initiated retry read. Mode-independent — operator decides which agents to retry. | n/a |
| 2831 | `_handle_retry` UPDATE retrying | UPDATE status='retrying' | BACKSTOP | Sets the agent to the retrying lane. Same signal for both modes. | n/a |
| 3411 | `_has_active_agent` SELECT 1 | `SELECT 1 FROM ... WHERE status = 'running'` | BACKSTOP | Concurrency cap predicate. `status='running'` covers both modes. | n/a |
| 3890 | `_atomic_claim` INSERT | `INSERT ... execution_mode = %s` | PASS | Claim-time write persists the mode so every downstream branch can observe it. | n/a |
| 4000 | `_lookup_active_owner_kind` SELECT kind | race-lost diagnostic | BACKSTOP | Daemon↔daemon race diagnostic, mode-independent. | n/a |
| 4302 | `recover_abandoned_agents` SELECT execution_mode, agent_task_arn | daemon-restart reconciliation | PASS (#3152) | Branches on `execution_mode`. ECS → survival INFO event, no DB action. Subprocess → daemon_restart_abandoned + retry marker. | n/a |
| 5476 | `_read_retries_used` SELECT retries_used | Ralph retry-context builder | BACKSTOP | Counter read; applies to both modes equally. | n/a |
| 6599 | `_update_agent_phase` UPDATE phase | phase advance | BACKSTOP | Phase progression write. Daemon-side advances write the same value for either mode; agent-runner likewise writes through its own psql client but to the same column. | n/a |
| 6651 | `_write_merged_at` UPDATE merged_at | merge-time flip | BACKSTOP | Merge event write. Daemon-side post-merge transition; mode-independent. | n/a |
| 6741 | `_write_verified_at` UPDATE verified_at | post-verify stamp | BACKSTOP | Milestone stamp. Mode-independent. | n/a |
| 6773 | `_write_verify_skip_reason` UPDATE verify_skip_reason | self-deploy PR | BACKSTOP | Pre-push self-deploy flag. Mode-independent. | n/a |
| 6818 | `_read_merged_at_and_verified_at` SELECT | verify short-circuit | BACKSTOP | Milestone read. | n/a |
| 6867 | `_restore_succeeded_and_advance_done` UPDATE | post-verify recovery | BACKSTOP | Post-merge state restore. | n/a |
| 6895 | `_read_verify_skip_reason` SELECT | verify gate | BACKSTOP | Column read. | n/a |
| 6931 | `_write_retroed_at` UPDATE retroed_at | milestone | BACKSTOP | Milestone stamp. | n/a |
| 7403 | `_write_failure_summary` UPDATE failure_summary | admin-cockpit hint | BACKSTOP | Templated failure-one-liner. Mode-independent. | n/a |
| 7478, 7488, 7496 | `_mark_agent_terminal` UPDATE | terminal state | BACKSTOP | Canonical terminal writer. Mode-independent by construction — the row transition is the contract; mode-specific teardown happens in the callers. | n/a (but callers must know to stop ECS tasks — see #3165) |
| 7882 | `_lookup_issue_number_by_agent` SELECT issue_number | helper | BACKSTOP | Bare lookup. | n/a |
| 8327 | `_observe_external_terminal` SELECT status | phase-boundary killswitch | BACKSTOP (subprocess-only observer) | Called by the subprocess worker thread between phases. ECS agent-runner does NOT invoke this — it has no equivalent. | #3166 (agent-runner lacks external-terminal observer) |
| 8377 | `_resume_retrying_agent` SELECT retrying | retry pickup | CONCERN | Picks up `status='retrying'` agents **without filtering on execution_mode**; creates a local subprocess worktree, silently forking an ECS agent to subprocess mode. | pr-3158 — skip ECS rows; they must resume via the agent-runner launch path, not the subprocess lane |
| 8417 | `_resume_retrying_agent` UPDATE running | retry status flip | CONCERN | Part of the same method — the flip precedes local worktree + subprocess. | pr-3158 — guarded by the execution_mode skip |
| 9215 | `_list_active_agents_for_telegram` SELECT | admin read | BACKSTOP | Best-effort read for operator UX. | n/a |
| 11211 | `_list_advanceable_agents` SELECT | scheduler queue | CONCERN | Picks up both subprocess AND ECS agents in post-ralph phases (awaiting_ci, awaiting_deploy, done, retro_done, retro_failed). For ECS agents, the agent-runner is still running and performs its own mechanical stubs for those phases; the daemon's advance handlers race with the agent-runner AND some use local `worktree_path` (which doesn't exist for ECS). See #3167. | #3167 |
| 12279 | `_bump_retries_used` UPDATE retries_used | retry counter bump | BACKSTOP | Counter increment. | n/a |
| 13374 | `_persist_phase_output_row` JOIN dispatcher.agents (for retries_used) | phase output persist | BACKSTOP | Metadata read. | n/a |
| 14008 | `_check_stuck_agents` SELECT running | supervisor stuck-timeout scan | CONCERN | SELECTs **every** `status='running'` agent regardless of `execution_mode`. For an ECS agent in a long ralph (cap 15h under `STUCK_TIMEOUT_SECONDS_BY_PHASE`) that goes longer (or any ECS agent whose `phase_transitions.ts` MAX is stale because the agent-runner isn't writing one mid-ralph), the supervisor flips it to `crashed` + enqueues a retry marker — at which point `_resume_retrying_agent` would silently fork it to subprocess. | pr-3158 — filter out `execution_mode='ecs'`. ECS stuck detection belongs to the `_reap_completed_agent_tasks` path (STOPPED → route via `_handle_agent_failure`), not the per-phase timer. |
| 14657, 14669 | `_process_retry_markers` JOIN dispatcher.agents (for worktree_path, issue_number) | retry drain | PASS (downstream CONCERN gated in `_resume_retrying_agent`) | The marker drain is mode-independent (drops worktree best-effort, flips status). The actual mode-fork happens in `_resume_retrying_agent` which we now guard. | pr-3158 (indirectly) |
| 14768 | `_process_retry_markers` UPDATE retrying | retry reset | BACKSTOP | Same as above. | n/a |
| 15153 | `_lookup_issue_number_by_agent_from_db` SELECT | helper | BACKSTOP | Bare lookup. | n/a |
| 15791 | `_build_diagnoser_context` SELECT worktree_path | diagnoser context | BACKSTOP | Best-effort read of `ralph-done.txt` from the worktree. For ECS agents the path doesn't exist on the daemon, so `ralph_done_content` is empty — graceful degradation. | n/a (graceful) |
| 15843, 16006, 16056 | `_build_diagnoser_context` / `_find_diagnoser_candidates` JOINs | diagnoser | BACKSTOP | Failure-table joins — mode-independent. | n/a |
| 15923 | retry-marker history read | diagnoser | BACKSTOP | Marker history. | n/a |
| 16647, 16657, 16683 | `_consume_action_*` `_mark_agent_terminal` + retry-marker inserts | diagnoser → terminal | CONCERN (terminal-only) | Marks the agent `failed` in the DB but does NOT stop a running ECS task. The ECS task keeps running until it writes its own status or is reaped. For tier-3 escalate / close / block this is a zombie risk. | #3165 (stop ECS task on diagnoser-forced terminal) |
| 18179 | `_persist_agent_task_arn` UPDATE agent_task_arn, execution_mode | ECS launch | PASS | Authoritative mode flip — every downstream reader sees `execution_mode='ecs'`. | n/a |
| 18252 | `_reap_completed_agent_tasks` SELECT agent_task_arn | ECS reap | PASS | Filters on `agent_task_arn IS NOT NULL`. | n/a |
| 18417 | `_reap_completed_agent_tasks` `_handle_agent_failure` call | ECS reap → failure route | PASS | Unified failure route for ECS-specific `agent_task_stopped_unexpectedly` category. | n/a |
| 18511 | `_read_agent_status_phase` SELECT status, phase | post-reap status read | BACKSTOP | Read-only for reap bookkeeping. | n/a |

## Fixes landing in PR #3158 (this PR)

### Fix 1 — `_check_stuck_agents` skips ECS agents

**Before.** SELECT every `status='running'` agent → apply per-phase
`STUCK_TIMEOUT_SECONDS_BY_PHASE` threshold → on timeout, mark
`crashed` + enqueue retry marker.

**After.** SELECT filter adds `AND execution_mode <> 'ecs'` (NULLs
coerced to subprocess via `COALESCE`). ECS agents rely on
`_reap_completed_agent_tasks` for liveness observation. An ECS task
that actually hung will eventually be STOPPED by ECS (container health
check, SIGKILL, OOM), at which point the reap path routes the failure
via `_handle_agent_failure` with category
`agent_task_stopped_unexpectedly`. No change for subprocess agents.

### Fix 2 — `_resume_retrying_agent` skips ECS agents

**Before.** Pick the oldest `status='retrying'` agent → create a new
local worktree → call `_run_orchestration_phases` (subprocess pipeline).

**After.** SELECT filter adds `AND execution_mode <> 'ecs'`. If an ECS
agent ever lands in `status='retrying'` (e.g. operator-initiated
`retry` command, or future bug), the subprocess lane skips it. An
`agent_ecs_retry_not_supported` log event fires once per observation
so the operator sees it; follow-up #4 tracks ECS retry support. The
guard is strictly additive — subprocess retry behaviour is unchanged.

### Fix 3 — `_handle_force_stop` per-agent branch calls `ecs:StopTask` first

**Before.** SELECT `pid` → UPDATE status='crashed' → SIGKILL(pid).
For ECS agents `pid` is NULL so the SIGKILL is a no-op; the Fargate
task keeps running.

**After.** SELECT `pid, execution_mode, agent_task_arn` → UPDATE
status='crashed' → branch on `execution_mode`:
- subprocess: SIGKILL(pid) as before.
- ecs: `ecs:StopTask(cluster=cfg.ecs_cluster_arn, task=agent_task_arn,
  reason='operator_force_stop')`. Best-effort; botocore errors log and
  continue — the DB row is already flipped so the reap tick will
  eventually catch up.

### Fix 4 — CI hygiene check `scripts/check-dispatcher-execution-mode-aware.sh`

Wired into `.github/workflows/ci.yml` dispatcher-tests job. Scans
`scripts/dispatcher/daemon.py` for every `UPDATE dispatcher.agents`
statement whose surrounding 30 lines (inclusive of the SQL string's
docstring context) do not mention `execution_mode`, `agent_task_arn`,
or a `# exec-mode-agnostic (#N):` annotation. Fails CI with the list
of offending sites and a fix-shape hint. Pattern mirrors
`check-terminal-routing-comments.sh` (#3062) and
`check-git-gh-retries.sh` (#3089).

## Follow-ups filed

1. **#3165 (FOLLOW-1) — diagnoser-forced terminals must stop ECS tasks.**
   `_consume_action_escalate` / `_close` / `_block_and_comment` /
   `_file_prerequisite_task` / `_block_on_existing_task` write
   `status='failed'` to the agent row but do not stop the underlying
   ECS task. For an ECS agent, this produces a zombie (DB says failed,
   task still running). Fix: after `_mark_agent_terminal` for an ECS
   agent, call `ecs:StopTask` before the method returns. Add a shared
   helper `_stop_ecs_task_if_ecs(agent_id)` and invoke from each of
   the five consumers.
2. **#3166 (FOLLOW-2) — agent-runner entrypoint has no external-terminal
   observer.** The subprocess worker thread calls
   `_observe_external_terminal` between phases so a diagnoser-written
   `failed` aborts the worker. The Bash agent-runner entrypoint does
   not — it runs each phase to completion and then reads the next
   phase from `dispatcher.agents`. A diagnoser escalation during a
   long ralph cannot preempt the ECS task. Fix: agent-runner polls
   `SELECT status FROM dispatcher.agents` between phases and exits
   non-zero if the row is terminal.
3. **#3167 (FOLLOW-3) — `_advance_running_agents` post-ralph phase handlers
   race with agent-runner mechanical stubs.** For ECS agents, both the
   daemon's `_advance_awaiting_ci` / `_advance_awaiting_deploy` /
   `_run_fix_ci` AND the agent-runner's mechanical-phase loop try to
   advance the same row. Some advance handlers use local
   `worktree_path` which doesn't exist for ECS. Fix shape: either (a)
   the daemon owns the post-ralph pipeline and the agent-runner exits
   after `push_and_pr`, OR (b) the agent-runner owns the full
   pipeline and the daemon only reads state + reaps. Decide before
   Stage 4 flips the default.
4. **#3168 (FOLLOW-4) — `_resume_retrying_agent` has no ECS-side support.**
   When fix 2 lands, an ECS agent flipped to `retrying` is skipped
   with a warning. The ECS retry surface needs its own launcher
   (reuse `_launch_agent_ecs_task` with a fresh agent-runner task).
   Today the forked-to-subprocess path was silently "working" for
   budgeted retries — the fork itself is the bug, even if the retry
   eventually succeeded as a subprocess. Track the gap so the product
   decision ("ECS retries relaunch the task" vs. "ECS retries
   terminal-and-reclaim via the issue queue") is explicit.

## Validation

- Unit tests for the three fixes land in
  `scripts/dispatcher/tests/test_daemon_execution_mode_audit.py`.
- The CI hygiene check is exercised by
  `scripts/dispatcher/tests/test_check_dispatcher_execution_mode_aware.py`
  against synthetic pass/fail fixtures.
- The existing `test_daemon_restart_cascade.py::
  TestRecoverAbandonedAgentsEcsExecutionMode` suite keeps the #3152
  fix pinned.
