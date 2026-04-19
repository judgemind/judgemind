---
description: Ralph phase for the per-phase /task-v2 pipeline. Iterative worker + reviewer loop, returns SHIP verdict plus uncommitted implementation diff in the worktree. Long-tail phase (~45-90 min internally).
argument-hint: "<agent-id>"
maxTurns: 500
model: sonnet
---

# /task-v2-ralph skill

Ralph phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Executes the plan produced by `/task-v2-plan` through an iterative worker + reviewer loop, and exits when the implementation is SHIP-ready (or blocked).

**Ralph runs for every change type.** There is no short-circuit. `/task-v2-plan` is read-only by contract (see `.claude/skills/task-v2-plan/SKILL.md`), so for non-testable change types — `docs`, `db_migration`, `dx_tooling`, `no_deployed_component` — ralph is still the phase that produces the committed diff. The inner `/ralph` skill adapts its worker and reviewer behavior based on `plan.change_type` (no TDD for docs-only edits, looser reviewer criteria, single-reviewer pass acceptable). See the §Design decision section below.

## Invocation context

This skill **MUST** be invoked from a context that has the `Task` tool available. Step 1 spawns `/ralph` as a Task-tool subagent, which in turn spawns its own worker and three reviewers as Task-tool subagents. Without the Task tool, this skill cannot run its core loop — it will exit cleanly with `verdict=BLOCKED` and a descriptive `block_reason`, but no implementation work happens.

**Supported invocation paths:**

- **Production (Phase 3+):** `claude -p /task-v2-ralph <agent-id>` from the dispatcher daemon running on Fargate. The daemon spawns each `/task-v2-*` phase as its own `claude -p` subprocess, so each phase gets a fresh top-level context with the full tool set (including `Task`).
- **Local smoke testing:** run `claude -p /task-v2-ralph <agent-id>` directly from a terminal (not from inside another `claude` session). Seed `{worktree}/tmp/dispatcher-input/ralph.json` manually by hand or via a fixture script before invocation.

**Unsupported — nested `Skill()` invocation.** Do not invoke this skill via the `Skill` tool from inside another `claude` session (e.g. from a general-purpose subagent running `Skill(skill="task-v2-ralph")`). Nested `Skill()` calls run in the parent agent's sub-context, which does **not** inherit the `Task` tool. The skill will detect the missing tool at Step 0 and exit with:

```
verdict=BLOCKED
block_reason="/task-v2-ralph requires Task tool — invoke via `claude -p`, not nested Skill()"
```

This is the harness-limitation path surfaced by the Phase 1 gate smoke test (issue #2766). For smoke-testing, use `claude -p` directly; nested `Skill()` cannot exercise the ralph loop end-to-end.

---

**Prerequisites:** The dispatcher daemon has already (a) installed dependencies per `dependencies_to_install` from the plan output, (b) written the input bundle to `{worktree}/tmp/dispatcher-input/ralph.json`.

**Goal:** Produce committed-ready code in the worktree's working tree (staged or unstaged — daemon stages + commits + pushes), plus `{worktree}/tmp/dispatcher-output/ralph.json` with a SHIP/BLOCKED verdict.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, any Task tool call, or any other operation. `/task-v2-ralph` is already a dispatcher-spawned background subprocess — further backgrounding causes completion notifications to surface in the wrong context and loses results.

**IMPORTANT — Subagent isolation.** The context-budget analysis in spike 0.3 (`docs/investigations/dispatcher-v2-spike-0.3.md`) shows `/task-v2-ralph` stays inside the 200k-token window ONLY if each worker + each reviewer runs as a fresh-context subagent (Task tool). Inline workers break this guarantee. Do not inline.

**Implementation choice (per issue #2732):** This skill invokes the existing `/ralph` skill as its inner loop. `/ralph` already implements the worker + three-reviewer cycle with fresh-context Task-tool subagents and per-iteration state files under `{worktree}/tmp/ralph/`. `/task-v2-ralph` is the thin outer wrapper that (a) seeds `task.md` from `plan.json`, (b) invokes `/ralph`, (c) parses `{worktree}/tmp/ralph/ralph-done.txt` into the output JSON. Keeps the implementation in one place and ensures parity with the current `/task` workflow's ralph behavior.

---

## Design decision — ralph runs for every change type (supersedes #2767)

**Prior decision (superseded).** Issue #2767 landed Option B: a skill-side short-circuit in `/task-v2-ralph` that returned `SHIP` with `iterations_used=0` and `changed_files=[]` for `change_type ∈ {docs, db_migration, dx_tooling, no_deployed_component}`. The rationale assumed "plan phase already produced a documentation diff in the worktree." That rationale was factually wrong — the plan skill is explicitly read-only (`.claude/skills/task-v2-plan/SKILL.md` line 18: "This phase is read-only against the repo and GitHub. Do not edit code"). Every non-testable agent since the Phase 3 cutover produced zero work (see #2832, #2831, #2712 on 2026-04-19: three consecutive failures, all with `iterations_used=0` and `has_changed_files=false`).

**Current decision (#2845).** Ralph runs for every change type. The outer `/task-v2-ralph` skill no longer consults `change_type` for routing — it always seeds `task.md`, always invokes `/ralph`, always parses the result. The inner `/ralph` skill reads `## Change type` from `task.md` and adapts:

- **Testable change types** (`api`, `scraper`, `ingestion`, `web`, `backfill_script`, `agent_skill`): full TDD + 3-reviewer loop, same as today.
- **Non-testable change types** (`docs`, `db_migration`, `dx_tooling`, `no_deployed_component`): worker skips TDD + diff-coverage gates; reviewer step accepts "no tests added" when the diff is docs-only and performs a single Claude-only review (the Gemini code-review passes are skipped because they add no signal on markdown/config-only edits).

See `.claude/skills/ralph/SKILL.md` §"Change-type-aware behavior" for the inner branch.

**Why Design 1 over alternatives** (see #2845 issue body for the full design survey):

- Keeps the daemon's phase graph uniform (`plan → ralph → summary` for every task). No new phases, no new skill files, no new daemon routing.
- Reuses `/ralph`'s existing worker+reviewer+Task-tool infrastructure. The code-change surface is confined to two markdown files plus one contract test.
- The plan skill's read-only contract is preserved — no dual-purpose phases, no "plan-and-implement" mode that would blow the plan subprocess's context/timeout.

Three candidate designs were considered:

- **Design 1 (chosen):** Ralph always runs, inner branches on `change_type`.
- **Design 2:** Add a new `/task-v2-implement` skill; daemon routes testable vs. non-testable to different phases.
- **Design 3:** Fold implement into plan; make the plan skill conditionally write code.

Designs 2 and 3 were rejected: Design 2 adds a whole skill that is ~80% duplicated from ralph's worker path without reviewers; Design 3 violates the plan skill's stated read-only contract and contradicts the per-phase subprocess context budgeting in spike 0.3.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/ralph.json`. Required fields:

- `agent_id` (str) — your UUID for correlation.
- `issue_number` (int).
- `plan` (object) — the full output from `/task-v2-plan`, i.e. the `plan.json` body. Always includes `plan_text`, `acceptance_criteria`, `relevant_files`, `relevant_docs`, `change_type`, and optionally `scope_check`.
- `worktree_path` (str) — absolute path to your worktree root.
- `repo_root` (str).
- `max_iterations` (int) — defaults to 5.
- `dependencies_installed` (list of str) — packages the daemon already set up (mirrors `plan.dependencies_to_install`). Ralph may still check `.venv` presence but should not re-install.

If the file is missing or malformed, exit 0 with `verdict=BLOCKED, block_reason="input JSON missing or malformed"`.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/ralph.json` with these fields, then exit 0:

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "verdict": "SHIP" | "BLOCKED",
  "iterations_used": <int, 1..max_iterations>,
  "block_reason": null | "<string>",
  "changed_files": ["<path>", ...],
  "summary": "<1-3 sentence implementation summary>",
  "ralph_done_path": "<worktree>/tmp/ralph/ralph-done.txt" | null,
  "review_log_path": "<worktree>/tmp/ralph/review-log.jsonl" | null
}
```

Always exit 0. BLOCKED is not a subprocess error — the daemon reads verdict from JSON.

`iterations_used` is `1..max_iterations` when the ralph loop runs (which is every non-BLOCKED path). The pre-#2845 short-circuit that emitted `iterations_used=0` for non-testable types no longer exists.

`ralph_done_path` and `review_log_path` are non-null whenever the ralph loop ran (every SHIP path). They are `null` only when Step 0's Task-tool availability check fails and the skill exits BLOCKED without spawning a worker.

On SHIP: the worktree working tree contains the implementation diff (or is clean, if the change was trivially a no-op — unusual). It may be staged or unstaged. The daemon stages + commits + pushes in the `summary` and `push_and_pr` phases.

On BLOCKED: the working tree may contain partial work. The daemon decides whether to retry (fresh worktree) or diagnose (`§8` Tier 2/3 flow).

---

## Step 0 — Task-tool availability check

The ralph loop requires the `Task` tool to spawn `/ralph` as a fresh-context subagent. If the skill was invoked via a nested `Skill()` call from another `claude` session (see the **Invocation context** section above), the `Task` tool is not available and the loop cannot run.

Before Step 1, verify the `Task` tool is callable. The check is implementation-defined — a reasonable approach is to inspect the tool registry exposed to the skill runtime, or to attempt a trivial no-op Task call and catch the "tool not available" error. Do not spawn a real worker subagent just to probe availability.

**If `Task` is unavailable**, emit the following JSON to `{worktree}/tmp/dispatcher-output/ralph.json` and exit 0. Do NOT create `{worktree}/tmp/ralph/`, do NOT invoke `/ralph`.

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "verdict": "BLOCKED",
  "iterations_used": 0,
  "block_reason": "/task-v2-ralph requires Task tool — invoke via `claude -p`, not nested Skill()",
  "changed_files": [],
  "summary": "Skipped — Task tool unavailable in current invocation context.",
  "ralph_done_path": null,
  "review_log_path": null
}
```

**If `Task` is available**, continue to Step 1.

---

## Step 1 — Seed ralph state directory

Create `{worktree}/tmp/ralph/` if it does not exist. Write:

- `task.md` — the consolidated task brief. Format:

  ```
  # Issue #<N> — <title>

  ## Plan (from /task-v2-plan)

  <plan.plan_text verbatim>

  ## Acceptance criteria

  - [ ] <criterion 1>
  - [ ] <criterion 2>
  ...

  ## Relevant files

  - <path 1>
  - <path 2>

  ## Relevant docs

  - <doc path 1>

  ## Change type

  <plan.change_type>

  ## Testable

  <"yes" if change_type in {api, scraper, ingestion, web, backfill_script, agent_skill};
   "no" if change_type in {docs, db_migration, dx_tooling, no_deployed_component};
   "yes" (fail-open) for any unrecognized value — see taxonomy note below>

  ## Scope boundary

  <any items from plan.scope_check with in_scope=false — so worker does NOT touch them>
  ```

- `feedback.md` — `No prior feedback. This is the first iteration.`

- `iteration.txt` — `1`

**Taxonomy — which change types are testable:**

- **Testable:** `api`, `scraper`, `ingestion`, `web`, `backfill_script`, `agent_skill`. Worker writes failing tests first; reviewers require tests for every acceptance criterion; loop runs all three reviewers.
- **Non-testable:** `docs`, `db_migration`, `dx_tooling`, `no_deployed_component`. Worker implements the plan's "What will change" section directly; pre-PR checks still run on any Python/TypeScript files actually touched; diff-coverage is skipped; reviewers accept "no tests added" without a REVISE; only the Claude reviewer runs (Gemini passes are skipped).
- **Unknown:** treat as testable (fail-open). The plan author (`/task-v2-plan`) owns the `change_type` enum; this skill accepts unfamiliar values by running the full loop rather than silently skipping work.

The `## Testable` line exists so the inner `/ralph` worker and reviewer prompts have a single, explicit signal to branch on — avoids re-deriving the set from `## Change type` in every subagent.

## Step 2 — Invoke `/ralph`

Spawn the `/ralph` skill as a Task-tool subagent (so its own internal worker+reviewer subagents inherit fresh-context isolation). Pass:

- The absolute worktree path.
- The issue number.
- `max_iterations`.

`/ralph` handles the full worker → reviewer(s) cycle, with each sub-invocation spawning its own fresh-context subagent (Task tool). It branches on `## Testable` in `task.md`:

- **Testable:** worker runs TDD + pre-PR checks + diff-coverage. Reviewers: Gemini standard → Gemini adversarial → Claude. All three must SHIP (or the persistent-dissent override applies).
- **Non-testable:** worker implements the plan directly + runs pre-PR checks applicable to the touched file types. Reviewer: Claude only. No Gemini passes, no diff-coverage gate, no "missing tests" REVISE.

`/ralph` writes `ralph-done.txt` when any of the following is true:

- All required reviewers returned SHIP → SHIP
- `max_iterations` reached without SHIP → REVISE/BLOCKED (ralph writes the final verdict)
- Worker returned STUCK on two consecutive iterations → BLOCKED

Wait synchronously for the subagent to complete. Do not time out from the outer skill — the dispatcher owns the subprocess-wide timeout.

## Step 3 — Parse ralph output

Read `{worktree}/tmp/ralph/ralph-done.txt` and `{worktree}/tmp/ralph/review-log.jsonl` (optional, structured, may be empty).

- `ralph-done.txt` first line contains the final verdict token: `SHIP`, `REVISE` (≡ BLOCKED — max iterations), or `BLOCKED` (worker stuck or explicit abort).
- `review-log.jsonl` rows include per-iteration reviewer verdicts for audit.

Map to output `verdict`:

| ralph-done.txt | output.verdict | block_reason |
|---|---|---|
| `SHIP` | `SHIP` | `null` |
| `REVISE` | `BLOCKED` | `max_iterations reached without SHIP` |
| `BLOCKED` | `BLOCKED` | `"<text from ralph-done.txt body>"` |

Count iterations from `{worktree}/tmp/ralph/iteration.txt`.

Capture `changed_files` from `git -C {worktree} diff --name-only HEAD`. Do NOT commit — daemon owns that.

Compose `summary` as 1-3 sentences describing what was implemented. Pull from the worker's final status report or the Claude reviewer's SHIP justification.

## Step 4 — Write output JSON and exit

Use the Write tool to emit `{worktree}/tmp/dispatcher-output/ralph.json` with the fields above. Exit 0.

---

## Context-budget note (spike 0.3)

Ralph is the long-tail phase by design. Each iteration (worker + reviewer(s)) is spawned with fresh context via Task-tool subagents, so the outer `/task-v2-ralph` context only accumulates:

- `plan.json` input (~5-10k tokens).
- Iteration transitions + verdict aggregation (~1-2k tokens per iteration, 5 iterations = ~10k).
- No raw file contents. No test logs. Those live in worker/reviewer subagent contexts and are discarded when those subagents exit.

Expected peak: ~30-45k tokens across 5 iterations. Well inside the 200k limit.

For non-testable change types, the budget is tighter: worker runs without TDD and only one reviewer runs, so iterations typically converge in 1-2 cycles at ~10-15k tokens total.

The Task-tool availability check (Step 0) is cheap: a single tool-registry probe, no subprocess.

## What this skill does NOT do

- **Does not install dependencies.** Daemon already did that in the `setup` phase.
- **Does not commit, push, or open a PR.** Daemon owns git + GitHub.
- **Does not post issue comments.** `/task-v2-summary` owns the pre-PR comment.
- **Does not watch CI or merge.** Daemon's `ci_watch` and `merge` phases handle that; if CI fails, the daemon spawns `/task-v2-fix-ci`.
- **Does not run deploy verification.** `/task-v2-verify` owns that post-deploy.

## Worked example — testable change, 1-iteration SHIP

Input `plan.json` has `change_type=scraper` — a one-file scraper fix. Step 0 confirms the Task tool is available, Step 1 seeds state (with `## Testable: yes`), Step 2 invokes `/ralph`, worker applies the one-line fix + regression test, all three reviewers agree SHIP on iteration 1. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1, exit.

Output `ralph.json`:

```
{
  "agent_id": "<uuid>",
  "issue_number": 1234,
  "verdict": "SHIP",
  "iterations_used": 1,
  "block_reason": null,
  "changed_files": [
    "packages/scraper-framework/src/courts/ca/orange.py",
    "packages/scraper-framework/tests/courts/ca/test_orange.py"
  ],
  "summary": "Fixed leading-zero loss in Orange County department regex; added regression test asserting department='03' round-trips.",
  "ralph_done_path": ".claude/worktrees/agent-<id>/tmp/ralph/ralph-done.txt",
  "review_log_path": ".claude/worktrees/agent-<id>/tmp/ralph/review-log.jsonl"
}
```

## Worked example — non-testable change (docs), 1-iteration SHIP

Input `plan.json` has `change_type=docs`. Step 0 confirms the Task tool is available, Step 1 seeds state (with `## Testable: no`), Step 2 invokes `/ralph`. The worker reads `## Testable: no`, skips TDD, implements the plan's "What will change" section (e.g. edits `docs/agent/unattended-patterns.md`), runs `scripts/check-markdown-links.sh` on any touched markdown files, and writes `COMPLETE`. Only the Claude reviewer runs — verifies acceptance criteria against the diff, confirms no stale references remain, writes `SHIP` to `review-result.txt`. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1, exit.

Output `ralph.json`:

```
{
  "agent_id": "<uuid>",
  "issue_number": 2712,
  "verdict": "SHIP",
  "iterations_used": 1,
  "block_reason": null,
  "changed_files": [
    "docs/agent/unattended-patterns.md"
  ],
  "summary": "Added Telegram pairing pattern to unattended-operation patterns doc.",
  "ralph_done_path": ".claude/worktrees/agent-<id>/tmp/ralph/ralph-done.txt",
  "review_log_path": ".claude/worktrees/agent-<id>/tmp/ralph/review-log.jsonl"
}
```

Notice `iterations_used=1` (not 0) and `changed_files` is non-empty — the #2845 fix guarantees both properties for any non-BLOCKED verdict.

## Worked example — BLOCKED on max iterations

Ralph runs 5 iterations, reviewers keep bouncing between SHIP and REVISE (fix-flipping). Ralph writes `ralph-done.txt` = `REVISE` + detail about the last iteration's feedback.

Output `ralph.json`:

```
{
  "verdict": "BLOCKED",
  "iterations_used": 5,
  "block_reason": "max_iterations reached without SHIP; last iteration reviewers disagreed on whether the department regex should fall back to None or raise on parse failure — design decision needed",
  ...
}
```

The daemon consumes BLOCKED, routes to the diagnoser (§8 Tier 2/3), which may `retry_with_hint` (tighter acceptance criterion on error handling) or `escalate` (ask human).

## Worked example — BLOCKED on Task-tool unavailability (nested Skill())

A local smoke test invokes `Skill(skill="task-v2-ralph")` from inside another `claude` session (e.g. from a general-purpose subagent). Step 0 detects the Task tool is unavailable and emits:

```
{
  "agent_id": "<uuid>",
  "issue_number": 2766,
  "verdict": "BLOCKED",
  "iterations_used": 0,
  "block_reason": "/task-v2-ralph requires Task tool — invoke via `claude -p`, not nested Skill()",
  "changed_files": [],
  "summary": "Skipped — Task tool unavailable in current invocation context.",
  "ralph_done_path": null,
  "review_log_path": null
}
```

Elapsed time: under a second. The operator re-runs the smoke test via `claude -p /task-v2-ralph <agent-id>` directly in a terminal, which has the Task tool available, and the loop runs normally. This is the only path that emits `iterations_used=0`.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Reviewers run synchronously via Task-tool subagents — no `run_in_background`.
- Pre-PR checks (ruff, pytest, lint/typecheck/build for testable types; markdown-link check, ruff on any touched `.py` scripts for non-testable types) run INSIDE ralph's worker or final verification step — the daemon does NOT re-run them before `git push`, but `.githooks/pre-push` will bail if anything is red. If your worker skipped pre-PR checks, ralph must run them at the end of the final iteration.
- `/ralph` writes status updates to `{repo_root}/tmp/agent-status/<agent-id>.txt` per its convention. `/task-v2-ralph` may read this file for monitoring but should not write to it — the dispatcher daemon owns agent status via `dispatcher.phase_transitions` rows.
