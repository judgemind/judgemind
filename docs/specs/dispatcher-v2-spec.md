# Dispatcher v2 — Self-Healing Autonomous Daemon

**Status:** Draft — for adversarial review
**Author:** Claude + Drew (2026-04-17 session)
**Replaces:** `.claude/skills/dispatcher/SKILL.md` (LLM-driven laptop dispatcher)

---

## 1. Goals

- **Continuous operation.** Runs off-laptop, 24/7. Survives laptop sleep, context limits, and local reboots.
- **Self-healing.** Detects stuck agents, API overload, uncommitted work, cwd drift, and CI failures; retries deterministically; escalates only when the retry budget is exhausted.
- **Observable from anywhere.** Current state queryable from the web app (admin page) and from a local CLI after `git pull`. No SSH required for a status check.
- **Deterministic orchestration, LLM only at the leaves.** Spawning, polling, retrying, merging, cleaning up — pure Python. All LLM reasoning happens inside `/task`, `/ralph`, `/spotcheck`, `/audit` agents that the daemon invokes as subprocesses.
- **Runner-agnostic leaf LLM.** The daemon spawns a "task runner" subprocess per phase and consumes its structured output. Claude (`claude -p`) is the first-class runner, but the interface is deliberately narrow — skill name, worktree path, input JSON, output JSON, timeout — so Cursor, Gemini CLI, OpenCode, or any future agent runner plugs in behind a thin adapter. Unlocks cost/quality comparison, hedging against a single-vendor outage, and picking the right model per phase (e.g. a cheaper model for mechanical summary vs. a reasoning model for CI fix). See §6b.
- **Progress without prompting.** Idle-mode behavior (run `/audit` after N PRs; run `/spotcheck` on schedule) encoded as rules, not conversations.

## 2. Non-Goals

- Multi-tenant / multi-repo. One daemon, one repo (`judgemind/judgemind`), for now.
- High availability. Single replica. A brief outage during deploy is fine.
- Replacing `/task`, `/ralph`, `/spotcheck`, `/audit` as LLM agents. They stay exactly as they are — the daemon just calls them differently.
- Real-time dashboards with charts. Admin page is a table view + controls. Pretty comes later.
- Cross-account credential handling. Daemon uses the same AWS/GitHub/Anthropic creds the laptop dispatcher uses today, injected via ECS task role + Secrets Manager.
- Mixing runners mid-phase. A phase is always executed by a single runner end-to-end. Runner selection is per-phase (and configurable per agent), but never swapped inside a phase. Also: no simultaneous competitive execution ("race Claude vs. Gemini on the same phase and take the best") — a fun idea, out of scope for v2.

## 3. Principles

1. **Postgres is the state of record.** Not files, not S3, not agent-status markers on disk. Everything the daemon does is reflected in `dispatcher.*` tables within the next tick.
2. **Every tick is idempotent.** A crash-and-restart mid-tick produces the same eventual state as a clean run.
3. **Runtime signals over post-hoc classification.** Failures are labeled by cheap deterministic signals (hooks, exit codes, timeouts). Daily summaries are SQL aggregations, not LLM transcript reviews.
4. **Retry markers, not retry RPCs.** Hooks and sub-subprocesses signal the daemon by writing rows (`dispatcher.failures`, `dispatcher.retry_markers`), never by calling back. Survives any crash.
5. **Fail to escalate, not to silent drop.** After the retry budget is exhausted, the task gets `status/needs-human` and a Telegram message with the issue URL. Nothing quietly vanishes.
6. **No LLM subprocess spans more than one workflow phase.** Each `claude -p` invocation runs one tightly-scoped phase (plan, implement, summarize, verify, etc.) with fresh context. Inter-phase handoff is strictly via `dispatcher.*` + small file artifacts the next phase reads. This is what lets us live without auto-compact in print mode (see §18 Risk 4b) and keeps every leaf LLM call small, reproducible, and replayable.

## 4. Architecture Overview

```
┌───────────────────────────────────────────────────────────────┐
│ ECS Fargate Service: judgemind-dispatcher-dev (single replica) │
│                                                               │
│   ┌─────────────────────┐      ┌──────────────────────────┐   │
│   │ Scheduler loop      │      │ Supervisor loop          │   │
│   │  (every 30s)        │      │  (every 2min)            │   │
│   │  - poll agent/ready │      │  - check stuck agents    │   │
│   │  - spawn /task subp │      │  - scan retry_markers    │   │
│   │  - merge green PRs  │      │  - enforce 529 backoff   │   │
│   │  - apply terraform  │      │  - write dispatcher.*    │   │
│   └──────────┬──────────┘      └──────────────────────────┘   │
│              │                                                │
│              ▼                                                │
│   ┌────────────────────────────────────────────────────┐      │
│   │ Subprocess pool: N concurrent `claude -p` workers  │      │
│   │  (one per active /task agent)                      │      │
│   │  - each owns an ephemeral worktree inside the      │      │
│   │    container                                       │      │
│   │  - exit code + stderr → dispatcher.failures        │      │
│   └────────────────────────────────────────────────────┘      │
└───────────────────────────────────────────────────────────────┘
          │                             │
          ▼                             ▼
   ┌─────────────┐              ┌──────────────┐
   │ RDS Postgres│              │ GitHub API   │
   │ dispatcher.*│              │ (gh / MCP)   │
   └──────┬──────┘              └──────────────┘
          │
          ├──► Admin page (web app → GraphQL → dispatcher.*)
          └──► Local CLI (scripts/dispatcher-status.py → GraphQL)
```

Control plane (inbound):
- Admin page writes to `dispatcher.commands` (start, stop, drain, retry, pause).
- Local CLI writes to the same table via the same GraphQL mutation.
- Telegram: **outbound notifications only** (§13). No inbound command routing — if chat-style interaction is wanted, the user opens a Claude Code session locally and queries `dispatcher.*` via the same GraphQL.

## 5. State Model

New schema `dispatcher.*` in the existing dev RDS. Authoritative. Every field the daemon reads or writes lives here.

| Table | Purpose | Key columns |
|---|---|---|
| `dispatcher.runs` | One row per daemon boot. | `run_id`, `started_at`, `stopped_at`, `version_sha`, `host`, `pid` |
| `dispatcher.agents` | One row per `/task` agent invocation. | `agent_id` (uuid), `issue_number`, `worktree_path`, `phase`, `status` (running / succeeded / failed / retrying), `started_at`, `ended_at`, `exit_code`, `pr_number`, `retries_used`, `parent_run_id` |
| `dispatcher.phase_transitions` | Append-only log. Replaces `tmp/agent-status/*.txt`. | `agent_id`, `phase`, `ts`, `autocompact_count` |
| `dispatcher.failures` | One row per deterministic failure detection. | `failure_id`, `agent_id`, `category` (enum, see §7), `detected_by` (`hook:subagentstop`, `supervisor:stuck`, etc.), `details` (jsonb), `ts` |
| `dispatcher.retry_markers` | Pending retries waiting for a scheduler tick. | `marker_id`, `agent_id`, `reason`, `attempt` (1..3), `retry_after_ts`, `resolved_at` |
| `dispatcher.commands` | Admin/CLI control channel. Poll-consumed by scheduler. | `command_id`, `command` (`start` / `stop` / `drain` / `pause` / `retry` / `force_kill`), `issued_by`, `issued_at`, `consumed_at`, `payload` (jsonb) |
| `dispatcher.config` | Live-editable settings (concurrency cap, idle mode on/off, backoff schedule). | `key`, `value`, `updated_at`, `updated_by` |
| `dispatcher.diagnoses` | One row per judgment-required failure routed to a diagnosis subprocess (see §8). Retains recommendation + post-retry outcome for effectiveness tracking. | `diagnosis_id`, `failure_id`, `agent_id`, `status` (pending / completed / failed), `recommendation` (jsonb), `outcome` (jsonb), `started_at`, `completed_at`, `actions_taken` (jsonb — empowered-diagnoser side-effect audit trail, migration 46), `next_directive` (text — `respawn_at=<phase>` \| `terminal` \| NULL, migration 46) |

Rationale for Postgres over files/S3:
- The dev DB already exists and has backups.
- `derived.*` + `dispatcher.*` joins enable queries like "median `ralph-worker` time per area-label" without a separate telemetry store.
- Single source of truth is the single most important property for a crash-restart-safe daemon.

Local access: the dev DB sits in a private VPC. Local CLI and the admin page both go through the web app's GraphQL (already auth'd, already deployed). The daemon connects directly via the task role's DB creds. No new network paths.

### Ephemeral vs Durable State — `tmp/` in the Daemon World

Current skills write extensively to `{worktree}/tmp/`: status files, ralph logs, PR bodies, helper scripts, review logs, analysis scratch. On a laptop this survives until the user cleans it up. In the daemon, the worktree lives in Fargate ephemeral storage and vanishes when (a) the daemon cleans it post-task, (b) the container restarts (deploy, OOM, task replacement), or (c) the subprocess exits cleanly but the container restarts before the daemon reaps.

**Principle:** if a value in `tmp/` is read by anything other than the subprocess that wrote it, it must also exist in Postgres or S3. A temp file is a handoff *within* a process, never *between* processes. With per-phase `/task-v2-*` subprocesses (§6), many files that used to stay within a single `/task` process now cross phase boundaries — this classification accounts for that.

**Already durable** (by existing or in-flight work):
- `tmp/agent-status/*.txt` → `dispatcher.phase_transitions` (this spec).
- `tmp/task-timings.jsonl` → S3 via #2643 (must land before daemon cutover).
- `tmp/ralph/review-log.jsonl` → S3 via #2647.

**Must be promoted to durable for the daemon:**
- `tmp/ralph/ralph-done.txt` — currently a signal file the parent reads. Replace with a final `dispatcher.phase_transitions` row (`phase='ralph-done'`, payload `{status, iterations, final_verdict}`). Daemon reads from DB, not disk.
- Hook failure signals (`no_commit_on_exit`, `cwd_drift`, etc.) — **hooks write directly to `dispatcher.failures` via `scripts/dispatcher/emit_failure.py`** (thin `psycopg[binary]>=3.1` wrapper, ~15 lines of runtime code; spike 0.2 #2684 shipped the reference implementation). Hooks run inside the `/task-v2-*` subprocess, which inherits `DATABASE_URL` from the container env and is in the same VPC as the DB. No marker-file indirection. Insert is wrapped in try/except + log: hook failure must never block the subprocess, and DB transient errors fall through to PR-vs-GH reconciliation (Risk 1) which still catches the crashed-agent case.
- Diagnoser context bundle — daemon constructs it from DB + S3 sources and persists to `dispatcher.diagnoses.context` *before* spawning the subprocess. The file passed via `--input-file` is just a cached copy; if the container restarts, the daemon reconstructs from the DB row on resume.
- **Per-phase skill outputs** — each `/task-v2-*` subprocess writes a structured result to `{worktree}/tmp/dispatcher-output/<phase>.json` before exiting (e.g. `/task-v2-summary` emits `{commit_message, pr_body, process_summary_comment}`; `/task-v2-verify` emits `{evidence_comment, verification_status}`). The daemon reads this file on subprocess exit, persists to `dispatcher.phase_outputs (agent_id, phase, output_json)`, and only then advances to the next phase. If the file is missing, the phase is treated as failed (no silent progression).

**Fine to stay ephemeral** (consumed inside a single subprocess lifetime):
- Per-step helper scripts, curl output buffers, ralph worker-reviewer scratch inside a single `/task-v2-ralph` invocation — written and consumed by the same `claude -p` subprocess. Losing them after exit is fine because they've been consumed by that phase's output file.

Audit step: before Phase 1 scaffolding, grep `.claude/skills/` and `docs/agent/` for every `tmp/` reference and classify each against the three buckets above. Any file read across phase boundaries (i.e. by a different `claude -p` subprocess or by the daemon itself) must be in the "must be promoted" list.

## 6. Scheduler Loop (every 30s)

The scheduler drives a per-phase state machine for each active agent. Each tick:

1. **Consume commands.** Read unconsumed rows from `dispatcher.commands`, apply, mark consumed. `stop` blocks new spawns but lets in-flight finish. `drain` same, with aggressive timeout. `pause` suspends both scheduler and supervisor.
2. **Promote retry markers.** Any `dispatcher.retry_markers` row with `retry_after_ts <= now` and `resolved_at IS NULL` becomes a candidate for re-spawn.
3. **Poll queue.** `gh issue list --label agent/ready --state open`. Cross-reference `dispatcher.agents` to exclude issues already in-flight. The candidate-pick filter (`_pick_candidate_issue`) chains five gates per candidate, in this order: `_issue_already_attempted` (DB) → `_issue_in_cooldown` (DB) → `_orphan_pr_recovery_pending` (DB + `gh pr view`) → `_issue_author_trusted` (`scripts/check-issue-author.sh`) → `_issue_already_shipped` (`scripts/check-shipped-pr.sh`, #4211). The shipped-zombie gate is the cheapest layer at which to catch an issue whose code has already merged on `main` without a `Closes #N` keyword (typical pre-#3994 placeholder-titled PRs); on a high-confidence match the daemon inline-cleans up the zombie via `_handle_shipped_zombie` (post verification-evidence comment + close with `--reason completed` + strip `agent/ready` and `status/in-progress`) and advances to the next candidate. Exit 1 (not-shipped) and exit 2 (script error) both fall through to the normal claim path — fail-open. The agent-side `/task` SKILL.md §4a.2 pivot remains as defense-in-depth for any zombie that bypasses this filter (operator hand-dispatch, race with a /task subagent already mid-claim).
4. **Compute spawn budget.** `config.concurrency_cap` (default 5) − count of agents in `status='running'` − retries-to-promote.
5. **Advance per-agent state machines.** For each active agent, determine the next phase based on `dispatcher.agents.phase` and spawn the corresponding subprocess. Phase map (see §6a for skill definitions):

   ```
   (new)                    → claim        [mechanical]
   claim                    → planning     [claude -p /task-v2-plan]
   planning                 → setup        [mechanical: install deps]
   setup                    → ralph        [claude -p /task-v2-ralph]
   ralph:ship               → summary      [claude -p /task-v2-summary]
   ralph:ac_infeasible      → diagnose     [claude -p /diagnose-failure; §8]
   summary:ok               → push_and_pr  [mechanical: git + gh pr create]
   summary:ac_infeasible    → diagnose     [claude -p /diagnose-failure; §8]
   push_and_pr              → ci_watch     [mechanical: gh run watch]
   ci_watch:green           → merge        [mechanical: gh pr merge]
   ci_watch:red             → ci_fix       [claude -p /task-v2-fix-ci]
   merge                    → deploy_watch [mechanical: gh run watch on deploy wf]
   deploy_watch             → verify       [claude -p /task-v2-verify]
   verify                   → retro        [claude -p /task-v2-retro]
   retro                    → done         [mechanical: cleanup]
   ```

6. **Spawn subprocess for current phase.** For agents whose phase calls for an LLM: build the input bundle from `dispatcher.*` + git state, write to `{worktree}/tmp/dispatcher-input/<phase>.json`, spawn `claude -p '/task-v2-<phase> <agent_id>'` with `--max-turns 500` and the subprocess-wide 180-min timeout. Each phase reads the input JSON and writes `{worktree}/tmp/dispatcher-output/<phase>.json`, which the daemon parses and persists to `dispatcher.phase_outputs` before advancing `dispatcher.agents.phase`.
7. **Reap completions.** For each subprocess that exited since last tick: parse exit code + output.json, update `dispatcher.agents.phase`, emit failure row if non-zero.
8. **Idle-mode dispatch.** If spawn budget > 0, queue empty, and no PRs pending merge: run rules (§10) — `/audit` every N merged PRs, `/spotcheck` on a schedule.

Every step is a single SQL transaction or idempotent git/gh call. Crash at any point leaves state recoverable on restart — the phase map above IS the recovery protocol. On daemon boot, every `status='running'` agent resumes from its persisted `phase`.

### 6a. Per-phase skills (`/task-v2-*`)

New skills in .claude/skills/task-v2-\*/SKILL.md, each narrowly scoped to one phase. The original `/task` skill is unchanged and remains the laptop-dispatcher's execution path. Cutover and eventual deletion of `/task` is a manual follow-up after v2 proves itself (same pattern as `/dispatcher` — see §16).

| Skill | Input | Output | Typical context budget |
|---|---|---|---|
| `/task-v2-plan` | issue #N, issue body+comments | plan text, scope-check findings, go/no-go signal | ~10 min |
| `/task-v2-ralph`[^ralph-subagent][^ralph-runs-every-type][^ralph-ac-infeasible] | plan from above, worktree path | SHIP verdict + implementation diff in git, OR `AC_INFEASIBLE` verdict with `infeasible_acs` array (see footnote) | ~45-90 min (multi-invocation internally, same as today) |
| `/task-v2-summary`[^summary-ac-infeasible][^summary-deferred-acs] | issue body + git diff | process-summary comment (AC mapping), commit message, PR body, `deferred_acs` array (see footnote), OR `AC_INFEASIBLE` verdict with `infeasible_acs` array (see footnote) | ~10 min |
| `/task-v2-fix-ci` | PR #N, CI failure logs | patch + commit message, OR explicit "give up — blocker" signal | ~15-30 min |
| `/task-v2-verify`[^verify-deferred-acs] | PR #N, deploy status, AC list, `deferred_acs` from summary | verification evidence comment with per-criterion proof (including the deferred ACs, see footnote) | ~10-15 min |
| `/task-v2-retro` | full agent history (phase_transitions, failures, PR URL) | retrospective issue body(s) to file | ~10 min |

[^ralph-subagent]: **Context-budget assumption:** The outer `/task-v2-ralph` process must keep its context bounded by spawning each worker + reviewer as a fresh-context subagent (Task tool or equivalent). If the Phase 1 implementation runs workers+reviewers inline, peak context balloons to 150-200k+ and a sub-split into `/task-v2-ralph-worker` + `/task-v2-ralph-review` becomes mandatory. See spike 0.3 findings (#2685, `docs/investigations/dispatcher-v2-spike-0.3.md`).

[^ralph-runs-every-type]: **Ralph runs for every `change_type` (#2845, supersedes #2767).** The daemon routes every agent through `plan → ralph → summary` regardless of the plan output's `change_type`. Plan is read-only by contract (`.claude/skills/task-v2-plan/SKILL.md` line 18: "This phase is read-only against the repo and GitHub. Do not edit code"), so for non-testable change types — `docs`, `db_migration`, `dx_tooling`, `no_deployed_component` — ralph is still the phase that produces the committed diff. #2767 (Option B) tried to skip ralph for these types and emit `SHIP, iterations_used=0, changed_files=[]`; because plan was not in fact producing a diff, the resulting agents all shipped empty worktrees and failed the summary AC-gate (see #2832 / #2831 / #2712 on 2026-04-19 for three consecutive cap=1 failures). #2845 removes the short-circuit entirely. The inner `/ralph` skill now reads `## Testable` from `task.md` and branches: testable types run the full TDD + 3-reviewer loop; non-testable types skip TDD / diff-coverage / Gemini passes and run a single Claude reviewer. See `.claude/skills/task-v2-ralph/SKILL.md` §"Design decision" and `.claude/skills/ralph/SKILL.md` §"Change-type-aware behavior".

[^ralph-ac-infeasible]: **`AC_INFEASIBLE` verdict and the `infeasible_acs` array.** Ralph's verdict enum is `SHIP | AC_INFEASIBLE`. The non-SHIP verdict exists for the case where ralph's worker+reviewer loop determines that one or more acceptance criteria cannot be satisfied as written — typical triggers are references to a non-existent symbol (a CLI flag that isn't in the codebase), self-contradictions between two ACs, or scope that exceeds the issue (the AC demands work the issue's other ACs assume is already done). Rather than grinding iterations toward a target it cannot hit or quietly ignoring the AC, ralph emits `verdict: "AC_INFEASIBLE"` with `infeasible_acs: [{index: N, evidence: "<what I looked for and why it's not here>"}]`. An array lets ralph flag multiple ACs in one pass — a single root cause (e.g. two ACs both referencing the same non-existent flag) shouldn't force a sequential re-plan per AC. The daemon detects this in post-exit parse (§8 `ralph_ac_infeasible` row) and routes through the existing diagnoser (Tier 3), whose `reissue` action rewrites the offending AC(s) and triggers a fresh plan→ralph, `escalate` flags for human, and `close` marks the issue `status/invalid`. AI-authored ACs (from retros, audits, spotchecks) are increasingly common and bring the same failure modes as any context-limited author — the pipeline must be robust against its own upstream mistakes. Summary has the same emit authority (see `/task-v2-summary` footnote) for the case where ralph takes the liberty to SHIP with partial AC coverage and the structural impossibility surfaces downstream.

[^summary-ac-infeasible]: **Summary can also emit `AC_INFEASIBLE`.** When summary reconciles the diff against the AC list and finds an unmet AC (and the AC was not classified as deferred — see `[^summary-deferred-acs]`), it classifies further: (a) *shape mismatch* — ralph produced a valid implementation that simply doesn't match the AC's expected artifact (inline tests vs. a fixture file) → today's path: mark `needs_review`, open draft PR, post issue comment for operator review; or (b) *structural impossibility* — the AC references a symbol that doesn't exist in the codebase or the PR diff, self-contradicts another AC, or requires work outside this issue's scope → emit `verdict: "AC_INFEASIBLE"` with the same `infeasible_acs` array shape ralph uses. Daemon detects via post-exit parse (§8 `summary_ac_infeasible` row) and routes to the diagnoser exactly like the ralph-originated case. Ralph's shipped diff is discarded on this branch; the diagnoser's `reissue` produces a corrected AC set, fresh plan→ralph runs. The cost is one extra ralph run when summary catches what ralph should have; the benefit is the same code path — summary does not need a separate "recover and merge" flow. Summary's classifier should err toward *shape mismatch* (draft PR + operator) when uncertain; `AC_INFEASIBLE` requires a citable-evidence justification (grep result, file path, conflicting AC index) the same way ralph's does.

[^summary-deferred-acs]: **Deferred-to-verify ACs — marker + heuristic.** Some ACs are only verifiable once the PR is merged and deployed (e.g. `Verify: query OpenSearch count against dev DB after rebuild_db --reset --county runs`). Summary cannot validate these against the pre-merge diff — checking them there would always flag unmet and force a spurious `needs_review`. Summary classifies each AC *before* running the diff validator: (a) **marker** — `Verify:` line begins with `(post-deploy)` → deferred; (b) **heuristic** — the `Verify:` line references a dev/prod artifact (`scripts/ecs-run-task.sh`, `curl dev.api...`, `POST /<index>/_count`, `rebuild_db`, `gh run watch` on a deploy workflow, etc.) → deferred; (c) otherwise → validate against the diff as today. Deferred ACs are listed in summary's output as `deferred_acs: [{index: N, reason: "marker" | "heuristic", verify_instruction: "<the Verify: line text>"}]` and are NOT counted toward `needs_review` or `AC_INFEASIBLE`. The marker is authoritative; when an author knows an AC is post-deploy, they write `Verify: (post-deploy) ...` and summary's classifier never reaches the heuristic. The heuristic exists because a large backlog of pre-convention issues is authored without the marker and cannot be retrofitted (their authors are ephemeral prior dispatcher runs). False-positive heuristic (tagging a code-verifiable AC as deferred) is benign: verify phase runs it post-deploy and either catches the gap or confirms the pass. False-negative (missing a post-deploy AC) is the current behavior — no regression.

[^verify-deferred-acs]: **Verify consumes `deferred_acs` from summary.** The verify phase today walks the issue's AC list and produces per-criterion proof against the deployed dev environment. With summary emitting `deferred_acs`, verify now has an explicit list of which ACs were skipped pre-merge — those are the first ones it runs (using the `verify_instruction` field carried forward from summary's output). Any AC not in `deferred_acs` was already validated against the diff by summary; verify can either re-confirm post-deploy (belt) or trust summary's pre-merge pass (skip and rely on CI). Default: re-run everything verify can, so the post-deploy evidence comment covers 100% of ACs. The comment explicitly labels which ACs were deferred and why (marker vs heuristic) so operators reading a PR trail can see which validations were time-shifted.

**Per-phase context budget — measured in spike 0.3.** Analytical measurement (chars/3.5 heuristic + bounded tool-call estimates) against fixtures from #2513 (the 108-min long-tail candidate) + PR #2534 showed all six phases land comfortably inside the 200k-token window with 4×+ headroom. Peak was `/task-v2-fix-ci` at ~42k tokens (~21% of window); `/task-v2-ralph` stays bounded at ~39k (~20%) specifically because its worker+reviewer subagents run in fresh contexts (see the footnote on the ralph row). No sub-split is required. Spike 0.3 scheduled a follow-up empirical re-measurement on Fargate against production skills (#2714). See `docs/investigations/dispatcher-v2-spike-0.3.md`.

The daemon handles everything between: worktree setup, `git add/commit/push`, `gh pr create`, `gh run watch`, `gh pr merge`, deploy watch, `scripts/unblock-dependents.sh`, worktree cleanup. No LLM in those steps.

**Merge-phase stale-rollup auto-unstick (#2641).** When `gh pr merge --squash` rejects with `base branch policy prohibits the merge` after the rollup classifier already said `green` (i.e. ci-passed SUCCESS on HEAD), the daemon treats it as GitHub's `statusCheckRollup` still scoring an old FAILURE check_run from an earlier CI attempt on the same SHA (see PR #3110's "UNSTABLE-but-green" pattern). Recovery: push an empty commit to the PR branch to force a fresh rollup evaluation, then re-enter `awaiting_ci`. Bounded at `MERGE_UNSTICK_MAX_ATTEMPTS = 1` per agent lifetime, tracked via `dispatcher.agents.merge_unstick_attempts`. A second stale-rollup rejection (or any failure during the empty-commit + push sequence) routes through `_handle_agent_failure` under the tier-3 category `merge_unstick_exhausted` so the diagnoser owns the escalation. Structured log events: `merge_stale_rollup_detected`, `merge_auto_unstick_empty_commit_pushed`, `merge_unstick_exhausted`. Every other non-zero `gh pr merge` exit (gh_missing, timeout, unrelated stderr) preserves the pre-#2641 behaviour — log + return; the next tick re-polls and the rollup classifier's fresh read decides green/red/pending.

**Why separate skills rather than a single `/task-v2` with phase arg?** Two reasons: (1) each skill gets its own frontmatter (`maxTurns`, `description`) tuned to its phase; (2) debugging and iteration become scoped — a bad PR body is a `/task-v2-summary` bug, not a "somewhere in /task" bug.

**Post-compaction recovery machinery from `/task` disappears.** The phase-split design means no individual phase runs long enough to autocompact. The daemon's resume-from-`phase`-on-boot is the structural equivalent, owned by Python rather than the LLM's status-file discipline.

### 6b. Runner abstraction

Each per-phase subprocess is spawned through a `Runner` interface:

```python
class Runner(Protocol):
    def run(
        self,
        skill: str,           # e.g. "task-v2-plan"
        worktree: Path,
        input_json: Path,     # {worktree}/tmp/dispatcher-input/<phase>.json
        output_json: Path,    # {worktree}/tmp/dispatcher-output/<phase>.json
        timeout_s: int,
    ) -> RunResult: ...       # {exit_code, stderr_tail, duration_s}
```

First implementation: `ClaudeRunner` — shells out to `claude -p '/<skill> <agent_id>'` with `--max-turns 500` and the container's MCP config path.

**Candidate secondary runners** (verified 2026-04-18):

| Runner | Invocation | Agentic? | Hooks | Skills loaded from | MCP | Notes |
|---|---|---|---|---|---|---|
| **Claude Code** | `claude -p '/<skill> <id>'` | yes | `PreToolUse`, `PostToolUse`, `SubagentStop`, etc. — shell-exec, write directly to `dispatcher.failures` via `emit_failure.py` | `.claude/skills/` | yes | First-class. `--max-turns`, no `--timeout`; wrap with OS `timeout(1)`. |
| **Gemini CLI** (`@google/gemini-cli`, [repo](https://github.com/google-gemini/gemini-cli)) | `gemini -p '<prompt>' --output-format stream-json` | yes | `BeforeTool`/`AfterTool`/`SessionStart`/`SessionEnd` — same `settings.json`-style shell-exec with stdin/stdout JSON, exit code `2` blocks. **Semantics are 1:1 with Claude, but event names differ** — Claude-spelled `PreToolUse` / `PostToolUse` entries in a shared `settings.json` are silently dropped by Gemini with a one-line startup warning, so `settings.json` matchers must be written per-runner (or mechanically converted via `gemini hooks migrate --from-claude`). Verified against Gemini CLI 0.38.2, spike 0.4 (#2686). | `.gemini/skills/` **or** `.agents/skills/` (cross-tool standard) | yes, native | Exit codes 0/1/41/53: `0` success, `1` invocation/flag-parse error, `41` auth missing, `53` turn-limit — *better* granularity than `claude -p`'s `1`-for-everything. Turn-limit mechanism is **`settings.json.model.maxSessionTurns`**, not a CLI flag (`--max-turns` does not exist in Gemini 0.38.2 and produces exit 1 if passed — the `GeminiRunner` writes the limit into the worktree's `.gemini/settings.json` before spawning). Auth: on Fargate use API key (`GEMINI_API_KEY`); OAuth free tier (1000 rpd on Gemini 3 Pro) requires interactive browser flow and is operator-laptop-only. Tool names differ (`read_file`/`write_file`/`run_shell_command`) so hook matchers need a rewrite layer. |
| **OpenCode** ([sst/opencode](https://github.com/sst/opencode)) | `opencode run "<prompt>"` | yes | `tool.execute.before` / `tool.execute.after` as in-process TS/JS plugins (cleaner — live Postgres connection, typed args, mutate or throw-to-abort). **No `SubagentStop` equivalent** — closest is `session.idle` via a generic `event` firehose ([#5409](https://github.com/sst/opencode/issues/5409) tracks the gap) | `.claude/skills/` is read natively; commands need duplication in `.opencode/commands/` | yes | No `--max-turns` or `--timeout` flags — wrap with OS `timeout(1)`. Exit codes undocumented; verify empirically. |
| **Cursor CLI** (`cursor-agent`, [docs](https://cursor.com/docs/cli/overview)) | `cursor-agent -p '<prompt>' -m <model>` | yes | `hooks.json` stdio-JSON shell-exec model (introduced Cursor 1.7, Oct 2025). 6 events: `beforeShellExecution`, `beforeReadFile`, `beforeMCPExecution`, `afterFileEdit`, `beforeSubmitPrompt`, `stop`. Exit 2 = deny (same convention as Claude). **Missing:** no `SessionStart`/`SessionEnd`, no generic `beforeFileEdit`, no `SubagentStop`. `beforeReadFile` supports secret-redaction before the LLM sees content — stronger than Claude there | `.cursor/rules/` (nestable `.mdc`); also reads `CLAUDE.md` and AGENTS.md natively; Skills via SKILL.md (Cursor 2.4+) | yes, via `mcp.json` | Known bugs: `-p` mode can hang indefinitely ([forum 150246](https://forum.cursor.com/t/cursor-agent-p-print-headless-mode-hangs-indefinitely-and-never-returns/150246)); sandbox + hook `"ask"` response is ignored ([forum 155438](https://forum.cursor.com/t/beforeshellexecution-returns-permission-ask-but-sandboxed-agent-shell-still-runs-the-command-sandbox-true/155438)). Validate with timeout wrapper before daemon use. |

Runner selection lives in `dispatcher.config` as a per-phase map (`runner_by_phase`), defaulting to `claude` for every phase. Overriding is a single config update — no daemon redeploy. The same config can be overridden per agent (e.g. cost experiments on specific issues) via `dispatcher.agents.runner_override jsonb`.

**Skills portability — use the `.agents/skills/` cross-tool standard.** Gemini CLI and OpenCode both agreed on `agentskills.io` (the SKILL.md frontmatter format Anthropic also uses) and both read `.agents/skills/` as a deliberately cross-tool path. Plan: keep our canonical skill files under .claude/skills/task-v2-\*/SKILL.md (Claude reads these directly), and symlink `.agents/skills/task-v2-*/` → the same directories so Gemini and OpenCode pick them up without duplication. OpenCode additionally reads `.claude/skills/` directly. Non-standard frontmatter fields (`allowed-tools`, `model`) silently drop on non-Claude runners — audit and move those into runner-specific adapters or inline instructions. Slash-command entry points are per-runner: `/task-v2-*` works in Claude; `.opencode/commands/*.md` must be duplicated for OpenCode; Gemini uses its own slash-command config.

**Hooks parity.** Our `PreToolUse` / `SubagentStop` / `PostToolUse` hooks (§9) write directly to `dispatcher.failures`. Parity picture by runner:
- **Claude Code and Gemini CLI — near-full parity, with naming caveat.** Both support shell-exec hooks with matcher regex, stdin/stdout JSON contracts, and exit-code-based blocking. `SessionStart`/`SessionEnd` map to our `SubagentStop` use case. `emit_failure.py` itself works unchanged (the stdin JSON payload has identical field shape — `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, verified in spike 0.4 #2686). **The event names differ, however:** Claude uses `PreToolUse` / `PostToolUse`; Gemini uses `BeforeTool` / `AfterTool`. A shared `settings.json` with both keys is safe — each runner silently drops the other's key with a one-line startup warning — but hook registration blocks must be written per-runner (or auto-converted via `gemini hooks migrate --from-claude`). Tool names also differ (`Read`/`Write`/`Bash` on Claude vs. `read_file`/`write_file`/`run_shell_command` on Gemini), so matcher regexes need a per-runner namespace.
- **Cursor CLI — partial.** `beforeShellExecution` / `beforeReadFile` / `afterFileEdit` / `beforeMCPExecution` / `beforeSubmitPrompt` / `stop` cover the preflight and post-edit surface. Exit code 2 = deny (same convention as Claude), so `emit_failure.py` works with minimal adaptation. No `SessionStart`/`SessionEnd` or dedicated `SubagentStop` — fall back to daemon post-exit reconciliation for no-commit/no-push detection. `beforeReadFile`'s content-redaction capability is a bonus we don't get elsewhere (useful if we ever feed secrets through agents).
- **OpenCode — partial.** `tool.execute.before`/`.after` cover the preflight/observability hooks (arguably *better* — typed args, mutate or throw-to-abort, live in-process Postgres connection). No `SubagentStop` equivalent; fall back to daemon post-exit reconciliation (PR-vs-GH check from Risk 1, git-state check on the worktree, `gh pr view` for branch state) to cover no-commit-on-exit / no-push-on-exit. Hooks are Bun/TS plugins, not shell commands — small shim required or the plugin shells out to `scripts/dispatcher/emit_failure.py`.
- **Any runner without hooks** — everything routes through post-exit reconciliation at coarser granularity and higher latency. All paths converge on the same `dispatcher.failures` categories.

**Model support.** Orthogonal to the runner choice: each runner exposes a different set of model providers.

| Runner | Anthropic | Google | OpenAI | xAI | Open-weight | Local |
|---|---|---|---|---|---|---|
| **Claude Code** | yes — API, Bedrock, Vertex, Foundry (toggled via `CLAUDE_CODE_USE_*` env vars) | no (officially) | no (officially) — works via LiteLLM/Bifrost gateway, unsupported | no | no | no officially |
| **Gemini CLI** | no | yes — AI Studio OAuth free tier + API key + Vertex | no | no | no | no |
| **OpenCode** | yes — Anthropic API, Bedrock, Vertex | yes — AI Studio + Vertex | yes — direct + Azure | yes — Grok | yes — Groq, Together, Cerebras, DeepSeek, OpenRouter, HF | yes — Ollama, LM Studio, llama.cpp |
| **Cursor CLI** | yes (curated) | yes (curated) | yes (curated) | partial (curated) | partial (curated) | no |

Model selection at the CLI: Claude uses `--model <alias\|id>` (aliases `opus`/`sonnet`/`haiku` or full IDs like `claude-opus-4-7`) and `--max-turns <N>` for turn-limiting; Gemini uses `-m <model-id>` (aliases `auto`/`pro`/`flash`/`flash-lite`) and has **no `--max-turns` flag** — turn-limiting is `settings.json.model.maxSessionTurns`, written by the `GeminiRunner` into the worktree's `.gemini/settings.json` before spawning (verified against Gemini CLI 0.38.2 in spike 0.4 #2686); OpenCode uses `-m <provider>/<model>` (e.g. `anthropic/claude-opus-4-7`, `google/gemini-3-pro`, `openai/gpt-5`); Cursor uses `--model <id>` against its curated roster (enumerate via `cursor-agent models`). Model selection is stored in `dispatcher.config.model_by_phase` (parallel to `runner_by_phase`) and overridable per agent via `dispatcher.agents.model_override`.

**Consequence for multi-model experiments.** If we want to mix, say, Opus 4.7 for planning + Gemini 3 Pro for summary + GPT-5 for CI-fix, the simplest path is **OpenCode as the single runner** — one binary, one config, three API keys. The alternative is one runner per phase (`ClaudeRunner` for Anthropic phases, `GeminiRunner` for Google phases, `OpenCodeRunner` only for OpenAI phases), which costs us three binaries and three auth models but keeps each runner's native feature set (e.g. Claude's `opusplan` auto plan-on-Opus / execute-on-Sonnet has no equivalent in OpenCode). Defer this decision until shadow-mode data tells us whether the model swap actually moves quality/cost metrics.

**Cost/quality shadow mode.** A future experiment — `runner_shadow` in config lets a second runner execute the same phase in parallel with its output discarded and diff-logged to `dispatcher.phase_outputs` (distinguished by `phase='<phase>@shadow:<runner>'`). Useful for measuring "would Gemini have produced a similar plan?" without cutting over. Out of scope for Phase 1-4 migration; noted here so the schema doesn't preclude it. The Gemini free OAuth tier (1000 rpd on Gemini 3 Pro) is generous enough to run meaningful shadow traffic without a budget line.

### 6c. ECS execution mode (per-agent Fargate)

`dispatcher.config.agent_execution_mode` controls how the daemon spawns agents, orthogonal to runner choice (§6b):

- `'subprocess'` (legacy fallback): daemon forks the runner (e.g. `claude -p /task-v2-<phase>`) as a child process inside its own ECS task. Each daemon redeploy SIGKILLs all child processes, abandoning in-flight agents. Reachable via an explicit `dispatcher.config.agent_execution_mode = 'subprocess'` row.
- `'ecs'` (**default since #3093**, Option A, #3086/#3078): daemon calls `ecs:RunTask` to launch a dedicated per-agent task from the `judgemind-dispatcher-agent-runner-dev` task-definition family. The agent-runner task is independent of the daemon task — daemon redeploys no longer kill agents.

The config flag is stored on the agent row at claim-time (`dispatcher.agents.execution_mode`) and is immutable for that agent's lifetime.

#### Agent-runner lifecycle

1. Daemon claims an issue; config value snapshotted onto the agent row.
2. Daemon's `_launch_agent_ecs_task` calls `ecs:RunTask` on the agent-runner task-def, passing `AGENT_ID` + `ISSUE_NUMBER` as container env. `agent_task_arn` populated on the agent row. Launch is wrapped in a 3-attempt / 1s+2s backoff retry (matches the #3053/#3085 retry pattern for transient AWS errors).
3. Agent-runner entrypoint (`scripts/dispatcher/agent-runner-entrypoint.sh`) clones the repo, creates an agent branch, and runs a phase loop: claiming → planning → ralph → summary → push_and_pr → awaiting_ci → merge → awaiting_deploy → verify → retro → done. Phase-transition logic comes from the shared `phase_transitions.py` module — the same state machine the daemon uses in subprocess mode.
4. Each phase writes an input bundle at `tmp/dispatcher-input/<phase>.json` via `phase_input_shim.py` (embedded Python in the entrypoint), invokes `claude -p /task-v2-<skill> $AGENT_ID` against the clone's `.claude/skills/`, reads the structured output from `tmp/dispatcher-output/<skill>.json`, persists to `dispatcher.phase_outputs`, and advances phase state.
5. Daemon's `_reap_completed_agent_tasks` (scheduler_tick) polls `ecs:DescribeTasks` on every non-null `agent_task_arn`. STOPPED-success → noop/gap-close; STOPPED-failure → `_handle_agent_failure`; RUNNING → noop. Fresh daemons after a redeploy just resume observing the ARNs — no abandonment bookkeeping needed. The startup-boot `recover_abandoned_agents` sweep branches on `execution_mode` (#3152): subprocess-mode rows take the legacy abandon path (failure row + terminal + retry marker); `'ecs'`-mode rows emit a single `agent_ecs_survived_daemon_restart` INFO event and defer all liveness observation to the next `_reap_completed_agent_tasks` tick. Without this branch a daemon redeploy kills every in-flight ECS agent despite the Fargate task still being RUNNING — the exact defeat Option A was designed to eliminate.

#### Data-path parity (input/output contract)

Both execution modes preserve the same skill contract:

| Direction | Path | Written by |
|---|---|---|
| Input | `tmp/dispatcher-input/<phase>.json` | subprocess daemon's `_write_phase_input`, OR agent-runner's `phase_input_shim.py` |
| Output | `tmp/dispatcher-output/<skill>.json` | the skill itself (emits structured JSON via its `/task-v2-*` implementation) |

Skills read from the input file and write to the output file; neither should depend on `claude -p`'s `.result` field or on any other execution-mode-specific plumbing. When porting a mechanism between execution modes, preserve both paths. The subprocess daemon's `_write_phase_input` + per-phase `_handle_phase_*` builders are mirrored in the entrypoint's `phase_input_shim.py` builders (summary/fix-ci/verify/retro parity, #3135).

#### IAM scope (dispatcher task role, ECS-mode only)

- `ecs:RunTask`, `ecs:StopTask` — scoped to the agent-runner task-def family.
- `ecs:TagResource` on `task/<cluster>/*` — required when RunTask uses tags (missed in the original Stage 2 IAM, added in #3129).
- `ecs:DescribeTasks` — conditioned on `ecs:cluster`.
- `iam:PassRole` — scoped to the agent-runner execution + task roles.

#### Image + terraform pipelines

- `Dockerfile.dispatcher-agent-runner` is a sibling of `Dockerfile.dispatcher`: same base image + tooling, different ENTRYPOINT. Kept as two files (not `--target` variants) per the #3090 scope note.
- `.github/workflows/deploy-agent-runner.yml` auto-builds the agent-runner image on changes to `Dockerfile.dispatcher-agent-runner`, `scripts/dispatcher/agent-runner-entrypoint.sh`, or `scripts/dispatcher/phase_transitions.py`. Publishes `<sha7>` + `latest` tags to `judgemind/dispatcher-agent-runner` ECR.
- `scripts/dispatcher/agent-runner-entrypoint.sh` is explicitly `!`-excluded from `deploy-dispatcher.yml` paths so entrypoint changes don't force a daemon redeploy.
- `infra/terraform/modules/dispatcher-agent-runner/` defines the task-def, IAM role, security group, log group, and ECR repo. Auto-applied on merge by `.github/workflows/terraform.yml`'s `dev-apply` job (#3107).

#### Sizing

Agent-runner sizing matches the subprocess-daemon envelope (4 vCPU / 16 GiB) while subprocess mode remains the default. Can shrink once subprocess is retired and the dispatcher daemon scales down correspondingly. The initial Stage 1b baseline (512 CPU / 1024 MB) pegged memory at 1022/1024 MB for hours and saturated CPU at 509/512 in bursts under real ralph workload (#3153).

#### Debugging a live ECS agent

When an agent-runner task is stuck or behaving unexpectedly and CloudWatch tail doesn't reveal enough, an operator can shell into the running container via ECS Exec (#3145). Prerequisites (all wired in terraform + daemon):

- Task-role policy `task_ecs_exec_ssm` grants `ssmmessages:{Create,Open}{Control,Data}Channel`.
- `_launch_agent_ecs_task` passes `enableExecuteCommand=True` on the RunTask call.
- Operator machine has the Session Manager plugin installed (`brew install session-manager-plugin`).

One-liner (fill in the task ARN from `dispatcher.agents.agent_task_arn` or `aws ecs list-tasks`):

```bash
aws ecs execute-command \
  --cluster judgemind-dev \
  --task <arn> \
  --container agent-runner \
  --interactive \
  --command /bin/bash
```

Files and paths to inspect once inside the container:

- `/var/lib/agent-runner/repo` — the agent's worktree clone. `git log`, `git status`, inspect any in-flight diff.
- `/var/lib/agent-runner/claude-p-<phase>.stdout.json` — the raw JSON stream Claude emitted for the current/last phase. Useful when a phase is hung or produced garbled output.
- `/var/lib/agent-runner/claude-p-<phase>.stderr.log` — stderr for the current/last `claude -p` invocation. Grep for connection errors, rate limits, and internal SDK exceptions.
- `/var/lib/agent-runner/tmp/dispatcher-input/<phase>.json` — the input bundle the skill received (from `phase_input_shim.py`).
- `/var/lib/agent-runner/tmp/dispatcher-output/<phase>.json` — the skill's structured output, once written.
- `ps auxwwf` — show the process tree: is `claude -p` still running, or are we between phases?

Note: `enableExecuteCommand` only takes effect on freshly-launched tasks. Tasks launched before #3145 merged cannot be execute-command-able retroactively — they must be let finish or stopped and relaunched.

#### Known gaps

- ~~`daemon_restart_abandoned` is used by the entrypoint as a generic failed-terminal — it's a category error when the failure isn't actually from a daemon restart.~~ Resolved: PR #3277 + #3300 renamed the stub-terminal callsites to `agent_runner_route_stub`, closing #3137. Legitimate `daemon_restart_abandoned` references (restart-recovery path) remain unchanged.
- No `launch-agent-runner-smoke.sh` DX helper yet — Stage 3 smokes were run by hand. Tracked as #3138.
- ECS agent-runner has no equivalent of the daemon's `CLAUDE_P_SUBPROCESS_TIMEOUT_SECONDS`. If claude hangs inside a phase, the task runs indefinitely. Needs a wall-clock timeout per-phase or per-task.

## 7. Supervisor Loop (every 2min)

1. **Stuck detection.** Agents with `status='running'` and no `dispatcher.phase_transitions` update in >30min are flagged. Writes `dispatcher.failures(category='stuck_timeout')`. `stuck_timeout` is mechanical (§8), so this creates a `dispatcher.retry_markers` row directly with exponential backoff (60s → 300s → 900s; give up after attempt 3). Judgment-required failures route through a diagnoser subprocess first (§8 Diagnosis step) — no retry marker is created until the diagnoser returns.
2. **529 backoff.** Count failures with `category='rate_limit_529'` in the last 10 min. ≥3 → set `dispatcher.config.spawn_frozen_until = now + 10min`. Scheduler respects this.
3. **Heartbeat.** UPDATE `dispatcher.runs.heartbeat_ts = now`. External check (CloudWatch alarm) pages if stale > 5min.
4. **Daily summary.** 12:00 UTC, run a SQL aggregation over the last 24h of `dispatcher.failures` (GROUP BY category, count, most-recent example) and commit the report to docs/dispatcher-daily/YYYY-MM-DD.md via an auto-PR. Pure SQL + markdown template — no LLM.

## 8. Failure Taxonomy

**Runtime signals** (emitted in real time, cheap). Each failure has a `Tier` that determines how the daemon responds:

- **Tier 1 — mechanical.** Fixed retry policy. No LLM involved.
- **Tier 2 — mechanical-then-diagnose.** Try the mechanical fix once; if the retry fails too, escalate to the diagnoser (§8 Diagnosis Step). Covers ~70% of what would otherwise need an LLM, without paying for one on the easy cases.
- **Tier 3 — diagnose-first.** No reliable mechanical fix. Go straight to the diagnoser.

**Exit code alone is insufficient** (spike 0.1 #2683 finding 3, `docs/investigations/dispatcher-v2-spike-0.1.md`). `claude -p` returns exit code `1` for BOTH auth failures and turn-limit trips (and for miscellaneous crashes). The daemon therefore runs a three-tier classifier:

1. **First-line: PreToolUse hook writes** — `scripts/dispatcher/emit_failure.py` (§9) inserts a row into `dispatcher.failures` with the granular category (`cwd_drift`, `gh_rate_exhausted`, …) as the hook fires inside the `/task-v2-*` subprocess. This covers every failure that happens *during* a tool call.
2. **Second-line: wrapper-level stderr regex** — for failures that short-circuit before any tool call (auth fails, rate-limit 529s, turn-limit trips), the hook never fires. The daemon's scheduler classifies by matching the subprocess's captured stderr/stdout tail:
   - `Error: Reached max turns` → `subprocess_turn_limit`
   - `Invalid API key` / `401 Unauthorized` → `subprocess_auth_fail`
   - `overloaded_error` / `529` → `rate_limit_529`
   - Exit code 137 with no matching regex → `subprocess_oom_or_kill`
   - Anything else with non-zero exit and no matching regex → `subprocess_crash`
3. **Third-line: exit code 0 = success** — the daemon still checks the agent's own exit signal (`dispatcher.phase_transitions` shows a terminal phase vs. a non-terminal phase) before marking the agent `succeeded`.

**Cross-runner handling differs.** Gemini CLI returns distinct exit codes per category (spike 0.4 #2686, `docs/investigations/dispatcher-v2-spike-0.4.md`): `0` success, `1` invocation/flag-parse error, `41` auth missing, `53` turn-limit. On a Gemini subprocess the daemon can skip the stderr-regex step and map exit code directly to category. Every runner adapter writes its own `parse_exit()` that returns one of the daemon-wide `dispatcher.failures.category` values regardless of runner-native exit codes.

| Category | Tier | Detected by | Signal / First-try fix |
|---|---|---|---|
| `cwd_drift` | 1 | PreToolUse hook (`agent-worktree-guard.sh`, `worktree-write-guard.sh`) | Hook blocks, writes `dispatcher.failures` → retry with fresh worktree |
| `rate_limit_529` | 1 | Scheduler, reading stderr of `claude -p` subprocess (hook never fires — auth/rate errors short-circuit before any tool call; see spike 0.1 #2683) | Regex on Anthropic 529 error body → exponential backoff retry |
| `stuck_timeout` | 1 | Supervisor | 30min since last phase transition → retry with fresh worktree |
| `gh_rate_exhausted` | 1 | PreToolUse GH-rate guard | `gh` rate budget < 100 → sleep until reset, retry |
| `subprocess_turn_limit` | 1 | Scheduler stderr regex (Claude exit 1 + `Reached max turns` / Gemini exit 53 via `settings.json.model.maxSessionTurns`) | Retry once with narrower scope hint; second trip escalates |
| `subprocess_auth_fail` | 1 | Scheduler stderr regex (Claude exit 1 + `Invalid API key` / Gemini exit 41) | Halt spawns, page operator — no retry fixes a bad secret |
| `subprocess_crash` | 1 | Scheduler (exit code not in known set AND no regex match — exit code alone is insufficient; see §8 intro) | Retry with fresh worktree |
| `no_commit_on_exit` | 2 | SubagentStop hook | Fix: retry once in a fresh worktree (covers interrupted-mid-work cases) |
| `no_push_on_exit` | 2 | SubagentStop hook | Fix: daemon pushes the branch itself and opens PR if missing (no agent retry needed — daemon finishes the last step) |
| `ralph_max_iterations` | 2 | Post-exit parse of `{worktree}/tmp/ralph/ralph-done.txt` | Fix: one more retry with hint `ralph hit max iterations on attempt N — check test flakes or consider narrower scope` |
| `ralph_ac_infeasible` | 3 | Post-exit parse of ralph output's `infeasible_acs` array | Ralph flagged one or more ACs as infeasible — diagnose immediately (no mechanical retry fixes a malformed AC). |
| `summary_ac_infeasible` | 3 | Post-exit parse of summary output's `infeasible_acs` array | Summary found an unmet AC that is structurally impossible (references a non-existent symbol, self-contradicts, or out-of-scope) — diagnose immediately. Ralph's diff is discarded on this branch; diagnoser's `reissue` triggers a fresh plan→ralph. |
| `ci_red_after_retries` | 3 | Scheduler | PR has failing CI and `dispatcher.agents.retries_used >= 3` — diagnose immediately |
| `merge_unstick_exhausted` | 3 | Scheduler (merge-phase handler) | `gh pr merge --squash` rejected with `base branch policy prohibits the merge` after the daemon already auto-unstuck once by pushing an empty commit (#2641). Second occurrence in the same agent's lifetime — mechanical retry budget `MERGE_UNSTICK_MAX_ATTEMPTS=1` already spent. Diagnose immediately; typical causes are a real CI failure masquerading as a stale rollup, non-CI branch-protection requirements, or a GitHub API anomaly the daemon can't work around mechanically. |

**Escalation:** 3 failures on the same issue in 24h → add `status/needs-human` + `priority/p1` (no p0 — human-only), post comment with the full taxonomy history, fire Telegram with the issue URL. Daemon moves on. (For Tier 2/3 failures, the diagnoser may escalate sooner; see below.)

### Diagnosis Step — Tier 2/3 Failures

Rationale: the operator is not continuously available. Escalating straight to human on every judgment-required failure blocks an issue for hours-to-days until someone checks Telegram. The diagnoser collapses that latency window by letting an LLM propose the next step in ~5 minutes, with the human reserved for cases the diagnoser explicitly escalates.

**When to invoke:**
- **Tier 2** — only after the mechanical fix has been tried once and the failure recurs. If `no_commit_on_exit` happens, the fresh-worktree retry resolves it, we never touch the diagnoser.
- **Tier 3** — immediately on first occurrence. `ci_red_after_retries` genuinely needs judgment (flaky test? wrong fix? environment? scope drift?), and there's no reliable mechanical remedy.

**Flow:**

1. Daemon writes a `dispatcher.diagnoses` row with `status='pending'` and a context bundle (agent_id, failure_id, issue number + body, recent `phase_transitions`, `ralph-done.txt` if present, PR URL, CI log URL, prior failures on the same issue, plus `prior_mechanical_fix` for Tier 2). For `ralph_ac_infeasible` and `summary_ac_infeasible`, the bundle also carries the `infeasible_acs` array from the failure's `details` JSON so the diagnoser can see which specific ACs were flagged and the evidence gathered; the `summary_ac_infeasible` bundle additionally includes ralph's SHIP diff and summary's per-AC mapping, so the diagnoser can decide whether the `reissue` rewrite should align with what ralph already built.
2. Daemon spawns `claude -p /diagnose-failure <diagnosis_id>` — a new one-shot skill, ~5-min wall-clock budget.
3. Skill reads the context, investigates as needed (the transcript, issue thread, CI logs), writes a structured JSON to `dispatcher.diagnoses.recommendation`:
   ```json
   {
     "action": "retry" | "retry_with_hint" | "reissue" | "escalate" | "close" | "block_and_comment" | "file_prerequisite_task" | "block_on_existing_task" | "terminal",
     "reasoning": "<one paragraph>",
     "hint": "<optional: comment text to post on the issue before retry>",
     "new_scope": "<optional: rewritten issue body if action='reissue'>"
   }
   ```
4. Daemon consumes the recommendation deterministically:
   - **retry** → create retry marker, no issue comment.
   - **retry_with_hint** → post `hint` as an issue comment, then create retry marker.
   - **reissue** → post diagnosis summary as comment, replace issue body with `new_scope`, keep `agent/ready`, create retry marker.
   - **escalate** → `status/needs-human` + `priority/p1`, post diagnosis as comment, no retry.
   - **close** → add `status/invalid`, close issue with diagnosis as the close comment, no retry.
   - **block_and_comment** → post diagnosis as comment, add `status/blocked`, remove `agent/ready`, no retry.
   - **file_prerequisite_task** → file a new prerequisite issue, append `Blocked by #<new>` to the current issue body, add `status/blocked`, post comment, no retry.
   - **block_on_existing_task** → append `Blocked by #<blocker>` to the current issue body, add `status/blocked`, remove `agent/ready`, post comment, no retry.
   - **`terminal`** → SKILL has already performed the gh side-effects directly (close, comment, label, file). Daemon's `_consume_action_terminal` records the recommendation and marks the agent terminal at phase `diagnoser_terminal`. No additional gh writes from the daemon.

**Inline-action pattern (#3458):** the empowered SKILL has peer-tier `gh` authority and performs gh writes itself, logging each side-effect to `dispatcher.diagnoses.actions_taken` (JSONB array — one entry per `git_commit`, `gh_issue_create`, `gh_issue_close`, etc.) and writing `dispatcher.diagnoses.next_directive` (`respawn_at=<phase>` to resume the pipeline, or `terminal` to free the slot). The preferred shape for any action involving gh side-effects is `action="terminal"` with descriptive `action_taken` + `summary` fields so the daemon's consumer path collapses to a single `diagnoser_terminal` phase. The legacy five actions (`retry`, `retry_with_hint`, `reissue`, `escalate`, `close`) plus the three newer ones (`block_and_comment`, `file_prerequisite_task`, `block_on_existing_task`) remain fully supported for backward compat; older skills that emit these keep working unchanged.

**Budget & safety:**
- One diagnosis per failure. Never diagnose a diagnosis.
- 5-min hard wall-clock timeout on the diagnoser subprocess. Timeout or malformed JSON → fallback to the fixed mechanical policy (3-retry exponential backoff, then escalate).
- Circuit breaker: if >30% of diagnoses in the last 24h fall back (timeout, malformed JSON, subprocess crash), flip `dispatcher.config.diagnoser_enabled = false`. Operator re-enables manually.
- The diagnoser is another leaf LLM feeding a deterministic switch — no unbounded agent loops, no agent judging another agent's judgment.

**Effectiveness tracking:** once a retry resolves (success, escalation, or close), its outcome is written back to `dispatcher.diagnoses.outcome`. The daily report aggregates "diagnoser recommended X → outcome Y" so we can measure net benefit. If after a month the diagnoser is net-neutral or net-harmful vs fixed policy, cut it.

## 9. Hooks

All hooks write **directly to Postgres** via a shared helper `scripts/dispatcher/emit_failure.py`. This helper is a thin `psycopg[binary]>=3.1` wrapper (~15 lines of runtime code; the full file ships with argparse + a `--table` SQL-injection guard): opens a connection using `DATABASE_URL` from the env, inserts one row into `dispatcher.failures`, closes. The insert is wrapped in try/except — any DB error is logged to stderr and swallowed, never propagated up. Hooks execute inside the `/task` subprocess, which shares the daemon's VPC and inherits the same `DATABASE_URL` env var, so network/auth are free. The repo-wide convention is psycopg3 (imported as `psycopg`, installed as `psycopg[binary]`) — `packages/scraper-framework/pyproject.toml` pins it and `scripts/dev_db_query_runner.py` uses it; `emit_failure.py` follows suit.

**Measured latency (spike 0.2, #2684).** 20 hook-triggered inserts from 20 separate Fargate tasks into the dev RDS writer: **p50 = 179 ms, p95 = 200 ms, max = 202 ms** (stdev 13 ms). Every hook invocation is cold — there is no pool, no shared connection — so the number is dominated by TCP + TLS + auth + INSERT round-trip. 200 ms p95 is invisible inside a typical `claude -p` tool call that already takes hundreds of ms to seconds; the direct-insert design (no local spool, no reaper loop) is validated. See `docs/investigations/dispatcher-v2-spike-0.2.md`.

Three hooks:

1. **SubagentStop** (`scripts/dispatcher/hooks/subagent_stop.py`): fires when a `claude -p` subprocess exits. Checks `git diff HEAD`, `git log origin/main..HEAD`, presence of expected files (`{worktree}/tmp/ralph/ralph-done.txt`, PR number). Inserts `dispatcher.failures` with the right category, or does nothing on clean success.
2. **PreToolUse worktree guards** (already exist): `agent-worktree-guard.sh`, `worktree-write-guard.sh`. Extend to call `emit_failure.py --category cwd_drift` in addition to blocking.
3. **PreToolUse GitHub rate guard** (new): short-circuits if `gh` rate budget < 100 remaining. Calls `emit_failure.py --category gh_rate_exhausted`.

**Fallback on DB transient errors:** if the hook's insert fails (connectivity blip, credential rotation), the failure signal is lost. Acceptable because the daemon's crashed-agent reconciliation path (Risk 1 — compare `dispatcher.agents.status='running'` against live PIDs on boot + inspect git state for unpushed work) catches the same case as a generic `crashed` failure. Spike 0.2 confirmed this failure mode end-to-end with an unreachable `DATABASE_URL`: the subprocess exits 0, the tool call completes, and the row is simply missing. Measure the specific-to-generic ratio once running; if hook-insert failures are >5%, add a retry loop or promote to a local spool.

**No marker files, no reaper loop.** Principle 1 (Postgres as state of record) holds end-to-end.

## 10. Idle-Mode Rules

Hard-coded in the scheduler. No LLM decision for which idle task to run:

- `/audit` runs after every 20 merged PRs (existing pattern — already documented).
- `/spotcheck` runs daily at 14:00 UTC.
- `/security-review` runs weekly, same tick as the failure-classification job.
- All three are dispatched as normal `claude -p` subprocesses with the corresponding issue (auto-filed by the scheduler) so they appear in `dispatcher.agents` with a distinguishing `kind` field.

Toggling any of these on/off is a single `UPDATE dispatcher.config` row — editable from the admin page.

## 11. Admin Interface

New route `packages/web/app/admin/dispatcher/`.

**Auth:** gated on `users.role = 'admin'` (matches existing admin dashboard gate in `data-quality.ts`). Non-admins 404.

**GraphQL additions** (`packages/api/src/graphql/dispatcher/`):

- `query dispatcherState` → current run, active agents (count + detail), recent failures (last 24h), queue depth, spawn-frozen-until.
- `query dispatcherAgent(agentId)` → full detail for one agent (phase transitions, failures, PR status).
- `mutation dispatcherControl(command, payload)` → writes a row to `dispatcher.commands`.

**Capped-list convention:** any admin-scoped list field with a server-side cap MUST be paired with a `{listName}Depth` or `{listName}Count` non-null integer field so the cockpit can render `{shown} / {total}`. Two precedents: `queueDepth`/`blockedDepth` (queue panels, #2886) and `recentCompletionsCount` (recently-completed panel, #3172). When adding a new capped list, replicate the `formatCountLabel` helper in `packages/web/src/app/(main)/admin/dispatcher/QueuePanel.tsx` for consistent display.

No subscriptions. Admin page uses Apollo `pollInterval: 2000` (2s). Dispatcher events happen on 30s/2min tick cadences, so a 2s polling delay is invisible — not worth the WebSocket infra.

**UI sections:**

- **Header:** daemon status pill (running / paused / stopped / unhealthy), uptime, version SHA.
- **Controls (#2884 simplified):** three buttons — **Start**, **Stop**, **Force Stop**. `start` flips `concurrency_cap` back to `target_concurrency_cap` (operator-configured target; defaults to 1 when no target row is set — #3779); `stop` is graceful (blocks new spawns, lets any in-flight agent finish its current phase pipeline); `force_stop` is immediate (sets `_pause_requested` so the in-flight worker aborts at the next phase boundary). Only `force_stop` posts a confirmation modal — the whole point of the simplification is that stopping dev work must not be friction-heavy for the operator. The former Pause / Resume / Stop (drain) / Force-stop cluster and the MFA re-auth gate were removed per #2884.
- **Circuit-breaker auto-close (#2860 + #3779):** when a cluster of bad terminal outcomes opens the breaker (`concurrency_cap=0`, `cap_flipped_by="circuit_breaker"`), two paths converge to close it. **Operator-reflip (#2860):** the operator manually raises cap to ≥1; the daemon clears `cap_flipped_by` and logs `daemon.circuit_breaker_closed`. **Time-based (#3779):** when at least `circuit_breaker_window_minutes` have elapsed since the breaker opened AND the current bad-outcome count over that rolling window is below threshold, the daemon restores cap to `target_concurrency_cap` and clears the flag — without this path the breaker is in a closed-feedback-loop deadlock (cap=0 → no agents → no terminal outcomes → bad_count never refreshes). Either path emits a structured log event for CloudWatch dashboards.
- **Active agents table:** agent_id (short), issue #, phase, elapsed, worktree (link to CloudWatch logs), actions (retry, kill).
- **Queue:** upcoming `agent/ready` issues, blocked-by count.
- **Recent failures (last 24h):** grouped by category, with count and most recent example.
- **Config:** concurrency_cap, idle_mode toggles, backoff schedule — editable.

## 12. Local Access

No bespoke CLI in v1. Access paths:

- **Admin page** (`/admin/dispatcher`) — primary interface. Works from any browser with the admin cookie.
- **Log tailing** — `aws logs tail /ecs/judgemind-dispatcher-dev --follow` for live logs; no new tooling needed.
- **Ad-hoc DB queries** — `scripts/dev-db-query.sh` + a dispatcher role with SELECT on `dispatcher.*` for operators who want to run SQL.

Revisit a dedicated CLI only if the admin page proves insufficient in practice (e.g. bulk agent control, or frequent use from environments where opening a browser is awkward). Open Question 4 about session-token handling dissolves as a result.

## 13. Telegram Integration — Outbound Notifications Only

The daemon posts to Telegram directly via the Bot API. **No Claude, no MCP plugin, no responder.** Outbound-only.

Rationale: MCP tools are a Claude Code feature — they exist inside an active Claude session, not inside an arbitrary Python process. Attempting to pipe Telegram messages into a one-shot `claude -p` responder adds a Claude entry point, an MCP config path, and a latency+cost surface per message, all to solve a problem the admin page and local CLI already solve. Drop it.

**Two classes of outbound message:**

**(A) Human-attention messages — MUST include the GitHub issue URL.** These are the messages that tell the user "something needs you." The daemon never escalates without pointing the user directly at the artifact that needs their attention. Every such message body includes `https://github.com/judgemind/judgemind/issues/<N>` (or `/pull/<N>` when the blocker is a PR) as a tappable link on mobile. Current escalation triggers:

| Trigger | Message contents | Link |
|---|---|---|
| Stuck-timeout escalation | agent-id, issue #N, phase, duration, `status/needs-human` applied | issue URL |
| Deploy-failure escalation | PR #N, deploy run URL, retries exhausted | PR URL + deploy run URL |
| Diagnoser `escalate` recommendation | issue #N, failure category, one-line reasoning from diagnoser | issue URL |
| 3-strike API overload | spawns frozen for 10min, N deferred issues | dispatcher admin URL |
| `claude -p` catastrophic failure (CLI missing, auth broken, image bad) | symptom, last log lines | dispatcher admin URL |

**(B) Status messages — informational, no action expected.** Included for situational awareness; omit URLs unless they add value.
- Daemon boot (startup + version SHA)
- Daily summary at 18:00 UTC (N PRs merged, M failures by category, queue depth)

Any new escalation path added later MUST extend the table in (A) with its own issue/PR URL — "something needs human attention" and "no link to act on" are incompatible.

**Implementation:** ~20 lines of `httpx.post("https://api.telegram.org/bot<TOKEN>/sendMessage", json={...})`. Chat ID stored in `dispatcher.config.telegram_chat_id`. Bot token from Secrets Manager. Send failures are logged and swallowed — Telegram outages must never block the scheduler. Messages are persisted to `dispatcher.notifications` (schema TBD in §19) so we can reconstruct the escalation history even when Telegram is down.

**If interactive chat becomes useful later:** the user opens a Claude Code session locally, points it at the same GraphQL, and reasons about state naturally. No always-on Claude process required, no MCP-in-container puzzle, no per-message LLM spend.

## 14. Deployment

Terraform module `infra/terraform/modules/dispatcher-daemon/`:

- ECS Fargate service, 1 replica, `desiredCount=1`.
- Task definition: 1 vCPU, 2GB RAM. Room for 5 concurrent `claude -p` subprocesses + git operations.
- Image: new `Dockerfile.dispatcher` based on the existing scraper-framework image; adds Claude Code CLI via the official install script.
- Secrets (via Secrets Manager → env):
  - `ANTHROPIC_API_KEY`
  - `GITHUB_TOKEN` — **scoped Personal Access Token** (spike 0.7, #2689, `docs/investigations/dispatcher-v2-spike-0.7.md`). Scope: `repo:read`, `issues:write`, `pull-requests:write` on `judgemind/judgemind` only. Not the agent account, not the operator's full-access token. The spike validated `git push`, `gh pr create`, `gh pr view`, `gh pr close`, and `scripts/check-issue-author.sh` from inside the container using this token path via `gh auth setup-git`; a GitHub App registration is explicitly NOT required for v1 (the PAT reuses the existing `secrets[]` + execution-role plumbing; an App would add webhook + private-key-rotation + JWT-installation ceremony without payoff at ≤5-concurrent-agent scale). File a `type/decision` issue to migrate to an App only if the daemon ever needs a repo-level (non-user) identity or if rate-limit pressure materially blocks work.
  - `DATABASE_URL` (dispatcher role, read/write `dispatcher.*`)
  - `TELEGRAM_BOT_TOKEN`
  - `GEMINI_API_KEY` — only required if `runner_by_phase` / `runner_shadow` ever routes to the Gemini runner. OAuth free-tier auth is operator-laptop-only (interactive browser flow); Fargate must use the API key. See spike 0.4 (#2686).
- Log group: `/ecs/judgemind-dispatcher-dev`, 30-day retention.
- CloudWatch alarm: heartbeat staleness > 5min → SNS → email + Telegram.
- Ephemeral storage: 50GB (worktrees are transient).

**Per-subprocess `$HOME` policy.** Each `/task-v2-*` subprocess gets its own `$HOME` so that `~/.claude/` (session cache, credential material) does not leak across agents or across subsequent invocations of the same agent. The working hypothesis — validated partially by spike 0.1's uid-1100 `spike` user setup and confirmed viable by spike 0.7's `gh auth setup-git` running inside the same container — is per-subprocess `$HOME` (e.g. `HOME=/tmp/agents/<agent_id>`), not a shared container-wide `$HOME`. `$HOME`-based caches that the Claude Code, Gemini, and OpenCode runners populate (pip wheel cache, Playwright browsers, `uv` cache, `npm` cache) multiply by concurrency under this model; spike 0.6's 50 GB ephemeral-storage measurement already has ~5× headroom over the realistic mixed-workload peak of ~10 GB (see `docs/investigations/dispatcher-v2-spike-0.6.md`), so even ×5 cache multiplication fits. This is a known assumption to verify empirically in Phase 1 (not a blocker — the escape hatches are (a) collapse to shared `$HOME` with session-cache namespacing, or (b) raise ephemeral storage to 100 GB, both cheap).

Prod deployment is out of scope for v1 — dev daemon handles both repos (same dev DB, same worktrees) until we prove stability.

## 15. Observability

Three CloudWatch alarms are wired in `infra/terraform/modules/dispatcher-daemon/main.tf`, all gated on `var.enable_alerts` and routed to `var.alert_sns_topic_arn` (email + Telegram).

### 15.1 Heartbeat staleness (see §14)

| Field | Value |
|---|---|
| Metric | `HeartbeatAge` (Maximum) in `Judgemind/Dispatcher` |
| Window / period | 60s × 5 consecutive evaluations |
| Threshold | > `var.heartbeat_stale_seconds` (default 300 s) |
| `treat_missing_data` | `notBreaching` — silence does not alarm during Phase 1 (daemon off) |

Source: daemon emits `HeartbeatAge` via `_emit_heartbeat_metric` every supervisor tick.

### 15.2 Stuck-timeout repeated

| Field | Value |
|---|---|
| Metric | `StuckTimeoutRepeatedCount` (Sum) in `Judgemind/Dispatcher` |
| Window / period | `var.stuck_timeout_repeated_window_seconds` (default 600 s) |
| Threshold | ≥ 1 |
| `treat_missing_data` | `notBreaching` |

**Why a daemon-side signal, not a raw metric filter on `failure_detected`.**
CloudWatch metric filters cannot express "same `agent_id`, count ≥ 2 in 10 min" without per-dimension math alarms (which require a static dimension set). Instead, the daemon's `_flag_stuck_agents` calls `_has_prior_stuck_timeout_in_window` after each `stuck_timeout` write; if a prior failure exists within 600 s it emits a dedicated `{ "event": "stuck_timeout_repeated", ... }` structured-log line. The metric filter counts those directly. Future editors: do not replace this with a filter on `failure_detected` — the per-agent check is load-bearing.

### 15.3 Diagnoser fallback spike

| Field | Value |
|---|---|
| Metric | `DiagnoserFallbackCount` (Sum) in `Judgemind/Dispatcher` |
| Window / period | `var.diagnoser_fallback_window_seconds` (default 1800 s) |
| Threshold | ≥ `var.diagnoser_fallback_threshold` (default 2) |
| `treat_missing_data` | `notBreaching` |

Source: daemon emits `{ "event": "diagnoser_fallback", ... }` whenever the diagnoser subprocess times out, returns non-zero, or produces malformed JSON and the mechanical escalation fallback fires instead. An isolated fallback is expected (transient timeout, model hiccup); ≥ 2 in 30 min indicates a systematic problem.

## 16. Migration Plan

**Throughout migration: both the laptop `/dispatcher` skill AND the original `/task` skill stay untouched.** They are the rollback target for every phase. Do not mark either deprecated, do not remove them from the skills index, do not rewrite CLAUDE.md to remove references. The new per-phase skills are created alongside under new names (`/task-v2-plan`, `/task-v2-ralph`, etc.) so the existing interactive loop continues working unchanged while v2 is brought up. Once v2 has proven itself in full production (post-Phase 4, measured against §17), the operator deletes the old skills manually as a standalone follow-up — not as part of these migration PRs.

**Phase 0 outcome — all 7 spikes returned GO.** The Phase 0 spikes ran in parallel over ~1 week and together de-risked the full architecture; every reasoned-but-untested claim in §4, §6, §7, §9, §14, and §18 now has empirical or analytical backing. The verdicts:

- **0.1 — `claude -p` end-to-end on Fargate: GO** (`docs/investigations/dispatcher-v2-spike-0.1.md`, #2683). All 4 scenarios (success, turn-limit, auth-fail, mcp-probe) booted and ran on Fargate in ~60s wall-clock; MCP tools propagate via explicit `--mcp-config`; one caveat (exit code 1 is ambiguous between auth-fail and turn-limit) folded into §8 as the tiered classifier.
- **0.2 — Hook → Postgres from a `claude -p` subprocess: GO** (`docs/investigations/dispatcher-v2-spike-0.2.md`, #2684). `emit_failure.py` direct-insert design validated at p50=179 ms / p95=200 ms / max=202 ms across 20 cold-connection hook fires; zero failed inserts; graceful fail-mode when DB is unreachable.
- **0.3 — Per-phase context budget: GO** (`docs/investigations/dispatcher-v2-spike-0.3.md`, #2685). All six `/task-v2-*` phases land at ≤21% of the 200k context window under realistic input from the #2513 long-tail; `/task-v2-ralph` stays bounded specifically because its worker/reviewer subagents run in fresh contexts; no sub-split required (follow-up #2714 schedules empirical Fargate re-measurement against production skills).
- **0.4 — Gemini CLI shadow: GO** (`docs/investigations/dispatcher-v2-spike-0.4.md`, #2686). `gemini -p` produces a schema-matching output for `/task-v2-summary`; hooks fire with Claude-shape JSON payloads (event names are `BeforeTool`/`AfterTool`, not `PreToolUse`/`PostToolUse`); exit code 53 on turn-limit confirmed via `settings.json.model.maxSessionTurns`.
- **0.5 — `.agents/skills/` cross-tool symlink: GO** (`docs/investigations/dispatcher-v2-spike-0.5.md`, #2687). Both Gemini CLI and OpenCode discover a SKILL.md via a relative symlink at `.agents/skills/<name>/ → ../../.claude/skills/<name>/`. OpenCode also reads `.claude/skills/` natively, producing a first-wins dedup WARN at `--log-level DEBUG` when both paths exist (cosmetic). Non-Claude frontmatter fields (`allowed-tools`, `model`) silently drop on both non-Claude runners.
- **0.6 — Worktree footprint at peak concurrency: GO** (`docs/investigations/dispatcher-v2-spike-0.6.md`, #2688). Typical 5-worktree Python load is ~3.4 GB; realistic mixed peak is ~10 GB; adversarial worst case 19.6 GB. 50 GB ephemeral storage (already budgeted in §14) gives 5× headroom on the realistic peak. No EFS, no shared venvs, no change from spec.
- **0.7 — Git + GitHub auth from Fargate: GO** (`docs/investigations/dispatcher-v2-spike-0.7.md`, #2689). Scoped PAT in Secrets Manager → `GITHUB_TOKEN` env var → `gh auth setup-git` → `git push` / `gh pr create` / `gh pr close` / `scripts/check-issue-author.sh` all succeed from inside the container. No GitHub App registration required for v1.

Phase 1 can begin without revisiting any Phase 0 question; the remaining §18 Open Questions are non-blocking (cost projection, diagnoser effectiveness — both measurable only post-launch).

**Phase 0: Spikes (parallel PRs, ~1 week).** Seven time-boxed experiments that verify the spec's reasoned-but-untested claims before any scaffolding lands. Failures here reshape the design, not just the schedule.

| # | Spike | Question it answers | Effort | What failure kills |
|---|---|---|---|---|
| 0.1 | **`claude -p` end-to-end on Fargate** | Can we spawn a Fargate task that runs `claude -p '/task-v2-plan 1'` with auth (via Secrets Manager), MCP config, writable `$HOME`, meaningful exit codes? | ~1d | The whole architecture |
| 0.2 | **Hook → Postgres from inside a `claude -p` subprocess** | Write `scripts/dispatcher/emit_failure.py`, register as `PreToolUse`, confirm a row lands in `dispatcher.failures` before the tool call resolves. Measure connection latency. | ~0.5d | §9 direct-write design; falls back to marker files |
| 0.3 | **Per-phase context budget measurement** | Run each `/task-v2-*` skill as `claude -p` against a representative past issue (candidates: #2513 at 108min, #2628 at 98min). Log token usage at exit. Does `/task-v2-ralph` stay bounded? | ~1d | The per-phase split; forces sub-splitting ralph |
| 0.4 | **`gemini -p` shadow against a past `/task-v2-summary` input** | Feed Gemini the same input the Claude runner would see. Compare output shape. Confirm hook registration via `settings.json` works and exit code 53 fires on turn-limit. | ~0.5d | The Gemini runner row in §6b; shifts the multi-runner story to OpenCode-only |
| 0.5 | **`.agents/skills/` cross-tool symlink sanity** | Symlink `.agents/skills/task-v2-plan/` → `.claude/skills/task-v2-plan/`. Confirm Gemini CLI and OpenCode both read it without balking at symlinks or non-standard frontmatter. | ~2h | "One skill, many runners"; forces per-runner duplication |
| 0.6 | **Worktree footprint at peak concurrency** | Measure current `/task` worktree size on disk (git checkout + `.venv` + ralph tmp + deps). Multiply by 5 concurrent. Does it fit in Fargate ephemeral-storage default (20GB), or do we need EFS or a higher ceiling (up to 200GB)? | ~1h | §14's 50GB ephemeral claim; may force EFS attachment |
| 0.7 | **Git + GitHub auth from Fargate** | Confirm `git push`, `gh pr create`, `gh pr merge`, and the issue-author trust check all work from inside the container using whichever auth we pick (scoped PAT in Secrets Manager, or a fresh GitHub App). | ~0.5d | §14 auth plan; may need a new GitHub App registration |

Sequence: 0.1 → 0.2 → 0.3 in series (each gates the next). 0.4–0.7 can run in parallel once 0.1–0.3 pass. Total wall-clock: ~1 week of focused spike work.

**Deferred spikes** (not blocking Phase 1):
- Cursor `-p` hang-bug repro — only matters if/when we add a Cursor runner.
- OpenCode hook DB-connection spike — only matters if/when we add an OpenCode runner.
- Diagnoser effectiveness measurement — can only be measured post-launch over weeks (already Open Question 5 in §18).
- Admin-page GraphQL authz — piggybacks on existing web app auth; no novel surface.
- Cost projection — covered as Open Question 4 in §18; informational, not a design gate.

Phase 0 does not require its own infrastructure — spikes run in the existing dev account using throwaway Fargate tasks, throwaway schema (`dispatcher_spike.*`), and a single test issue labeled `type/spike`. Every spike produces either a "go" note on the corresponding §18 Open Question or a design-change proposal.

**Phase 1: Scaffolding (1 PR, or a small series).**
- New `dispatcher.*` schema via migration.
- Empty `scripts/dispatcher/daemon.py` with scheduler + supervisor loop skeletons; no spawning yet.
- Terraform module with ECS service set to desiredCount=0.
- Admin GraphQL schema + admin page reading from empty tables.
- Create the per-phase `/task-v2-*` skills in `.claude/skills/` (see §6a). Each is a separate file under its own directory. The original `/task` skill is NOT touched — the new skills are additive. Initially these can be thin extractions from the original `/task` SKILL.md with the parts they don't own deleted.

Gate: admin page loads with "0 agents, daemon stopped" on dev. Each `/task-v2-*` skill can be invoked interactively end-to-end against a test issue (sanity check — not a full production run).

**Phase 2: Shadow mode (1 PR).**
- Daemon scales to 1, but `config.concurrency_cap=0`. Polls queue, writes state, but spawns nothing.
- Laptop `/dispatcher` continues running — its state is NOT mirrored into `dispatcher.*` (we don't want to double-book).

Gate: daemon has observed ≥20 queue-state update cycles without crashing; admin page queue depth matches `gh issue list` to within 1 at 3 consecutive checks.

**Phase 3: Single-slot production (1 PR).**
- `config.concurrency_cap=1`. Daemon spawns one agent at a time, driving the full phase state machine in §6 via `claude -p` invocations of `/task-v2-*`.
- Laptop dispatcher paused (user stops invoking `/dispatcher`). The skill file stays on disk.

Gate: ≥10 successful task completions via the daemon; zero stuck agents; all retries resolved correctly.

**Phase 4: Full handoff (1 PR).**
- `config.concurrency_cap=5`.
- CLAUDE.md "Autonomous sessions" section adds a pointer to the daemon as the default autonomous path, with the laptop `/dispatcher` documented as the manual/emergency alternative.
- Both the laptop `/dispatcher` and the original `/task` skills stay intact — no deprecation banner, no deletion. Deletion (if it happens at all) is the operator's manual follow-up once v2 has accumulated confidence.

Rollback plan: any phase can revert by scaling ECS to 0 and invoking laptop `/dispatcher` + original `/task` as normal. State in `dispatcher.*` is read-only in that mode.

## 17. Success Criteria

- Daemon runs ≥14 days with no human intervention. Any intervention is a bug.
- Retries resolve ≥80% of transient failures (stuck, 529, cwd drift) without human touch.
- Median dispatcher-to-merge time matches or beats laptop dispatcher. (Current: need to measure — this needs a baseline from `task-timings.jsonl` before cutover.)
- Admin page answers "what is the dispatcher doing right now" in under 2s.
- Daily SQL-based summary produced reliably for ≥7 consecutive days; the operator can spot the top 3 failure categories at a glance.

## 18. Risks & Open Questions (adversarial-review bait)

### Risks

1. **Daemon crash with in-flight agents.** Subprocesses inherit the daemon's pgid; on ECS container stop, they're SIGKILL'd. In-flight worktrees are lost. **Mitigation:** on next daemon boot, reconcile `dispatcher.agents` (any `status='running'` agents whose PID doesn't exist → mark `status='crashed'`, emit failure, queue retry). Unpushed work is gone — acceptable because `/task` commits per-phase.

2. **Double-daemon race during deploy.** ECS rolling deploy can briefly run two instances. Both would try to spawn agents. **Mitigation:** `dispatcher.runs` acts as a lease — daemon only spawns if its `run_id` matches the latest row. Second instance sees another is active, loops without spawning until the old one exits.

3. **Postgres outage.** Daemon can't read or write. **Mitigation:** if DB is unreachable for >5min, daemon stops spawning and writes a CloudWatch metric. Does NOT kill in-flight agents (they have their own git state). On DB recovery, reconciles.

4. **Subprocess runaway.** Two distinct risks:
   - (a) **Wall-clock hang** — a tool call (curl, pytest, `gh run watch`) blocks forever. `claude -p` has no default turn limit (verified: no `--max-turns` default), so a tool hang could run until the ECS task is killed by infra. **Mitigation:** hard 180-min wall-clock timeout on every subprocess. SIGTERM → SIGKILL. Emits `subprocess_timeout` failure. 180min accommodates the known long-tail (#2513 107min, #2628 98min) with safety margin; the supervisor's 30-min no-phase-transition signal catches truly stuck agents earlier anyway.
   - (b) **Context exhaustion without auto-compact.** Interactive `claude` auto-compacts at context limits; `claude -p` does not (per CLI docs). Long runs that previously completed interactively may hit context limits in print mode and degrade or fail silently. **Mitigation:** (i) pass `--max-turns 500` defensively so runaway loops exit with a distinguishable error instead of silently degrading; (ii) the `/task` skill already breaks work into the `/ralph` sub-skill — evaluate spawning each sub-skill as its own `claude -p` invocation so context resets at sub-skill boundaries. This is a Phase 1 investigation, not a blocker: measure end-to-end context usage on the first real `/task` runs and size from there.

5. **GitHub Actions runner queue depth.** If CI is backed up (common on busy days), PRs pile up waiting for checks. Daemon merges based on check rollup; if checks don't start for 20min, "recently merged" signal (for idle-mode `/audit`) fires incorrectly. **Mitigation:** treat `pending` as neutral, not green; only count SUCCESS for idle triggers.

6. **Admin page auth compromise.** Controls include force-stop (global and per-agent). Anyone with admin access can stop the daemon. **Original mitigation (superseded by #2884):** require fresh re-auth (MFA-style) for destructive actions. **Current mitigation:** admin role on the session is the only gate. The prior placeholder MFA check accepted any non-empty `X-MFA-Token` header — zero real safety, pure friction — and during a 2026-04-20 incident it actively prevented the operator from stopping a runaway pipeline for ~2 hours. Since admin-session auth is already required for any dispatcher control, adding a per-request friction layer without a real second factor was net-negative. Audit log in `dispatcher.commands.issued_by` remains the trail of record. If a real MFA second factor is ever implemented, apply it to login (every admin action) rather than per-control-button (which is the friction pattern that failed in 2026-04-20).

7. **Schema evolution.** Adding a new phase name requires a migration. **Mitigation:** `dispatcher.phase_transitions.phase` is a free-form string, not an enum. Validation is daemon-side, not DB-side.

### Open Questions

1. **Per-phase `claude -p` context budget verification** — **Resolved (Spike 0.3, #2685).** Measured peak context per `/task-v2-*` phase: every phase lands below ~42k tokens (≤21% of the 200k window). `/task-v2-ralph` specifically stays bounded at ~39k because its iterative worker/reviewer loop spawns each iteration as a fresh-context subagent — the outer ralph only accumulates verdicts across iterations. No sub-split of `/task-v2-ralph` is required as long as the subagent-isolation assumption (see §6a footnote) holds. The measurement was analytical (chars/3.5 heuristic + bounded tool-call estimates against real fixtures from the #2513 long-tail); follow-up #2714 schedules an empirical re-measurement against Phase 1 production skills on Fargate. See `docs/investigations/dispatcher-v2-spike-0.3.md`.

2. **MCP tool propagation to subprocesses** — **Resolved (Spike 0.1, #2683).** `claude -p` running on Fargate reads `--mcp-config /path/to/config.json` correctly, starts the declared MCP servers, and exposes their tools to the session. The `mcp_probe` scenario enumerated the full 26-tool `mcp__github__*` suite from inside a container. The failure mode observed in #2656 (Agent-tool subagent with no MCP access) was specific to the Agent-tool sandbox's config propagation — daemon-spawned `claude -p` invocations get exactly the MCP servers the daemon gives them via `--mcp-config`. Daemon-side plumbing: bake a container-local `mcp-config.json` into the image (or render from Secrets Manager at startup) and pass `--mcp-config` to every `claude -p` call. See `docs/investigations/dispatcher-v2-spike-0.1.md`.

3. **Worktree inside Fargate ephemeral storage** — **Resolved (Spike 0.6, #2688).** Measured: 5 concurrent worktrees at typical Python workload is ~3.4 GB; realistic mixed peak (one terraform-touching agent in the mix) is ~10 GB; adversarial worst case (all 5 touching web + terraform + both Python venvs) is ~19.6 GB. 50 GB ephemeral storage gives 5× headroom on the realistic peak; 20 GB default would fit typical load but leaves no margin. `.git/objects/` is shared across worktrees (fixed 75 MB, not multiplied). Ralph tmp dir growth is bounded at sub-MB. **Decision:** keep 50 GB as spec'd (§14); no EFS; no shared venvs. See `docs/investigations/dispatcher-v2-spike-0.6.md`.

4. **Cost comparison** — laptop dispatcher costs Anthropic tokens + user's time. Daemon costs tokens + ECS (~$30/mo task + CW logs) + dev DB load. Is the delta justified by self-healing? **Action:** project a month's run before building.

5. **Diagnoser net benefit — tiered version.** The tiered diagnoser (§8: mechanical first, diagnose only on recurrence or Tier 3) should see far fewer invocations than the always-diagnose original. Projected: ~2-3 invocations/week. At that volume, the effectiveness bar is low — we mainly need to verify the diagnoser doesn't make things worse than immediate human escalation. **Action:** first month of operation, dump every `dispatcher.diagnoses.recommendation` + its `outcome` daily. If the diagnoser's recommendation was "retry" and the retry succeeded, that's net-positive latency savings; if it escalated, the human got a pre-analyzed failure; if it was wrong, measure the added-damage rate.

## 19. Appendix — Schema DDL sketch

```sql
CREATE SCHEMA dispatcher;

CREATE TABLE dispatcher.runs (
  run_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at    timestamptz NOT NULL DEFAULT now(),
  stopped_at    timestamptz,
  heartbeat_ts  timestamptz NOT NULL DEFAULT now(),
  version_sha   text NOT NULL,
  host          text NOT NULL,
  pid           int NOT NULL
);

CREATE TABLE dispatcher.agents (
  agent_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_run_id    uuid REFERENCES dispatcher.runs(run_id),
  kind             text NOT NULL DEFAULT 'task',       -- task|audit|spotcheck|security-review
  issue_number     int NOT NULL,
  worktree_path    text NOT NULL,
  phase            text NOT NULL DEFAULT 'claiming',
  status           text NOT NULL DEFAULT 'running',    -- running|succeeded|failed|retrying|crashed
  started_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz,
  exit_code        int,
  pr_number        int,
  retries_used     int NOT NULL DEFAULT 0,
  pid              int,
  runner_override  jsonb,                               -- per-agent override of runner_by_phase; NULL = use config default
  model_override   jsonb                                -- per-agent override of model_by_phase; NULL = use config default
);
CREATE INDEX ON dispatcher.agents (status) WHERE status = 'running';
CREATE INDEX ON dispatcher.agents (issue_number);

CREATE TABLE dispatcher.phase_transitions (
  transition_id      bigserial PRIMARY KEY,
  agent_id           uuid NOT NULL REFERENCES dispatcher.agents(agent_id),
  phase              text NOT NULL,
  ts                 timestamptz NOT NULL DEFAULT now(),
  autocompact_count  int NOT NULL DEFAULT 0
);
CREATE INDEX ON dispatcher.phase_transitions (agent_id, ts DESC);

CREATE TABLE dispatcher.failures (
  failure_id    bigserial PRIMARY KEY,
  agent_id      uuid REFERENCES dispatcher.agents(agent_id),
  category      text NOT NULL,
  detected_by   text NOT NULL,
  details       jsonb NOT NULL DEFAULT '{}',
  ts            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON dispatcher.failures (ts DESC);
CREATE INDEX ON dispatcher.failures (category, ts DESC);

CREATE TABLE dispatcher.retry_markers (
  marker_id       bigserial PRIMARY KEY,
  agent_id        uuid NOT NULL REFERENCES dispatcher.agents(agent_id),
  reason          text NOT NULL,
  attempt         int NOT NULL CHECK (attempt BETWEEN 1 AND 3),
  retry_after_ts  timestamptz NOT NULL,
  resolved_at     timestamptz
);
CREATE INDEX ON dispatcher.retry_markers (retry_after_ts) WHERE resolved_at IS NULL;

CREATE TABLE dispatcher.commands (
  command_id    bigserial PRIMARY KEY,
  command       text NOT NULL,   -- start|stop|drain|pause|resume|retry|force_kill
  issued_by     text NOT NULL,
  issued_at     timestamptz NOT NULL DEFAULT now(),
  consumed_at   timestamptz,
  payload       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ON dispatcher.commands (consumed_at) WHERE consumed_at IS NULL;

CREATE TABLE dispatcher.config (
  key         text PRIMARY KEY,
  value       jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  updated_by  text NOT NULL
);

CREATE TABLE dispatcher.diagnoses (
  diagnosis_id    bigserial PRIMARY KEY,
  failure_id      bigint NOT NULL REFERENCES dispatcher.failures(failure_id),
  agent_id        uuid NOT NULL REFERENCES dispatcher.agents(agent_id),
  status          text NOT NULL DEFAULT 'pending',  -- pending | completed | failed
  context         jsonb NOT NULL,                   -- serialized bundle passed to the diagnoser
  recommendation  jsonb,                            -- {action, reasoning, hint?, new_scope?}
  outcome         jsonb,                            -- filled in after the retry resolves
  started_at      timestamptz NOT NULL DEFAULT now(),
  completed_at    timestamptz,
  actions_taken   jsonb,                            -- empowered-diagnoser side-effect audit log (#3458 / migration 46)
  next_directive  text                              -- 3-state daemon directive: respawn_at=<phase> | terminal | NULL (#3458 / migration 46)
);
CREATE INDEX ON dispatcher.diagnoses (status) WHERE status = 'pending';
CREATE INDEX ON dispatcher.diagnoses (agent_id);

CREATE TABLE dispatcher.phase_outputs (
  output_id    bigserial PRIMARY KEY,
  agent_id     uuid NOT NULL REFERENCES dispatcher.agents(agent_id),
  phase        text NOT NULL,                       -- plan|ralph|summary|fix_ci|verify|retro
  output_json  jsonb NOT NULL,                      -- structured output read from {worktree}/tmp/dispatcher-output/<phase>.json
  ts           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON dispatcher.phase_outputs (agent_id, phase);

CREATE TABLE dispatcher.notifications (
  notification_id  bigserial PRIMARY KEY,
  kind             text NOT NULL,                    -- stuck_timeout|deploy_failure|diagnoser_escalate|api_overload|claude_p_broken|boot|daily_summary
  severity         text NOT NULL,                    -- human_attention | status
  issue_number     int,                              -- populated when the notification targets a specific issue
  pr_number        int,                              -- populated when the notification targets a specific PR
  body             text NOT NULL,
  issue_url        text,                             -- denormalized for admin-page display; NULL on status-class messages
  sent_at          timestamptz,
  send_error       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON dispatcher.notifications (created_at DESC);
CREATE INDEX ON dispatcher.notifications (severity, sent_at) WHERE severity = 'human_attention';

-- Seed config
INSERT INTO dispatcher.config (key, value, updated_by) VALUES
  ('concurrency_cap',         '5',      'init'),  -- runtime cap; circuit breaker writes 0 on overnight-safety trip
  ('subprocess_timeout_s',    '10800',  'init'),  -- 180 min ceiling; covers known outliers (#2513 107min, #2628 98min)
  ('backoff_seconds',         '[60,300,900]', 'init'),
  ('idle_audit_every_n_prs',  '20',     'init'),
  ('idle_spotcheck_cron',     '"0 14 * * *"', 'init'),
  -- Runner selection: per-phase map. Overridable per-agent via dispatcher.agents.runner_override.
  ('runner_by_phase', '{"plan":"claude","ralph":"claude","summary":"claude","fix_ci":"claude","verify":"claude","retro":"claude","diagnose":"claude"}', 'init'),
  -- Model selection: per-phase map. Values are runner-native model IDs (Claude aliases, OpenCode provider/model strings, etc.).
  ('model_by_phase',  '{"plan":"opus","ralph":"sonnet","summary":"haiku","fix_ci":"sonnet","verify":"haiku","retro":"haiku","diagnose":"opus"}', 'init'),
  ('runner_shadow',    '{}',         'init');  -- e.g. {"summary":"gemini"} runs gemini in parallel, output diff-logged only

-- target_concurrency_cap (#3779) — written by operator via admin cockpit
-- or `breaker.sh set-target N`. The migration that seeds this row is
-- deferred to a follow-up PR while migration number 56 is contended
-- across multiple open PRs (see issue #2916 deadlock pattern). The
-- daemon falls back to 1 when the row is absent, which matches the
-- legacy `start` semantics.
```

---

**Next step:** adversarial review. I'll spawn (or you spawn) a reviewer with this file as input and a specific brief to attack the design (not accept it) — look for correctness bugs, race conditions, operational traps, and cost blow-ups. Anything in §18 is fair game; anything not yet listed is bonus.
