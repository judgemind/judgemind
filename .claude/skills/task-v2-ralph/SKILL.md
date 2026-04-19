---
description: Ralph phase for the per-phase /task-v2 pipeline. Iterative worker + reviewer loop, returns SHIP verdict plus uncommitted implementation diff in the worktree. Long-tail phase (~45-90 min internally).
argument-hint: "<agent-id>"
maxTurns: 500
model: sonnet
---

# /task-v2-ralph skill

Ralph phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Executes the plan produced by `/task-v2-plan` through an iterative worker + reviewer loop, and exits when the implementation is SHIP-ready (or blocked).

## Invocation context

This skill **MUST** be invoked from a context that has the `Task` tool available. Step 1 spawns `/ralph` as a Task-tool subagent, which in turn spawns its own worker and three reviewers as Task-tool subagents. Without the Task tool, this skill cannot run its core loop — it will exit cleanly with `verdict=BLOCKED` and a descriptive `block_reason`, but no implementation work happens.

**Supported invocation paths:**

- **Production (Phase 3+):** `claude -p /task-v2-ralph <agent-id>` from the dispatcher daemon running on Fargate. The daemon spawns each `/task-v2-*` phase as its own `claude -p` subprocess, so each phase gets a fresh top-level context with the full tool set (including `Task`).
- **Local smoke testing:** run `claude -p /task-v2-ralph <agent-id>` directly from a terminal (not from inside another `claude` session). Seed `{worktree}/tmp/dispatcher-input/ralph.json` manually by hand or via a fixture script before invocation.

**Unsupported — nested `Skill()` invocation.** Do not invoke this skill via the `Skill` tool from inside another `claude` session (e.g. from a general-purpose subagent running `Skill(skill="task-v2-ralph")`). Nested `Skill()` calls run in the parent agent's sub-context, which does **not** inherit the `Task` tool. The skill will detect the missing tool at Step 0.5 and exit with:

```
verdict=BLOCKED
block_reason="/task-v2-ralph requires Task tool — invoke via `claude -p`, not nested Skill()"
```

This is the harness-limitation path surfaced by the Phase 1 gate smoke test (issue #2766). For smoke-testing, use `claude -p` directly; nested `Skill()` cannot exercise the ralph loop end-to-end.

The non-testable change-type short-circuit in Step 0 runs **before** the Task-tool check, so `docs`, `db_migration`, `dx_tooling`, and `no_deployed_component` change types still SHIP cleanly even when invoked via nested `Skill()` (they don't need the Task tool because they don't run the loop).

---

**Prerequisites:** The dispatcher daemon has already (a) installed dependencies per `dependencies_to_install` from the plan output, (b) written the input bundle to `{worktree}/tmp/dispatcher-input/ralph.json`.

**Goal:** Produce committed-ready code in the worktree's working tree (staged or unstaged — daemon stages + commits + pushes), plus `{worktree}/tmp/dispatcher-output/ralph.json` with a SHIP/BLOCKED verdict.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, any Task tool call, or any other operation. `/task-v2-ralph` is already a dispatcher-spawned background subprocess — further backgrounding causes completion notifications to surface in the wrong context and loses results.

**IMPORTANT — Subagent isolation.** The context-budget analysis in spike 0.3 (`docs/investigations/dispatcher-v2-spike-0.3.md`) shows `/task-v2-ralph` stays inside the 200k-token window ONLY if each worker + each reviewer runs as a fresh-context subagent (Task tool). Inline workers break this guarantee. Do not inline.

**Implementation choice (per issue #2732):** This skill invokes the existing `/ralph` skill as its inner loop. `/ralph` already implements the worker + three-reviewer cycle with fresh-context Task-tool subagents and per-iteration state files under `{worktree}/tmp/ralph/`. `/task-v2-ralph` is the thin outer wrapper that (a) seeds `task.md` from `plan.json`, (b) invokes `/ralph`, (c) parses `{worktree}/tmp/ralph/ralph-done.txt` into the output JSON. Keeps the implementation in one place and ensures parity with the current `/task` workflow's ralph behavior.

**Routing choice (per issue #2767) — Option B, skill-side short-circuit.** The underlying `/ralph` skill documents "When NOT to use: Terraform, DB migrations, CI/CD, docs, investigation tasks. For those, implement directly per CLAUDE.md." The original `/task` workflow (`.claude/skills/task/SKILL.md` §A.2) enforced this by only calling `/ralph` for testable code tasks — non-testable changes went through a direct-implementation path.

In the dispatcher v2 per-phase pipeline, the equivalent routing must live somewhere. Two options were considered (see issue #2767):

- **Option A — daemon-side routing.** The daemon inspects `plan.change_type` and skips invoking `/task-v2-ralph` entirely for non-testable values. Requires documenting the routing rules in the spec §6a and in daemon docs.
- **Option B — skill-side short-circuit (chosen).** `/task-v2-ralph` itself checks `plan.change_type` in Step 0. If non-testable, it immediately emits a `SHIP` verdict with `iterations_used: 0` and exits. The daemon does not need to know about the distinction.

**Option B was chosen** because it keeps the routing logic co-located with the skill that owns the testable-vs-non-testable contract, it mirrors where the testable-only rule lives today (inside `/ralph`'s own docs), and it avoids spreading the change-type taxonomy across two layers (daemon + skill). The daemon's only responsibility is to invoke `/task-v2-ralph` — the skill decides whether a full ralph loop is warranted.

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
  "iterations_used": <int, 0..max_iterations>,
  "block_reason": null | "<string>",
  "changed_files": ["<path>", ...],
  "summary": "<1-3 sentence implementation summary>",
  "ralph_done_path": "<worktree>/tmp/ralph/ralph-done.txt" | null,
  "review_log_path": "<worktree>/tmp/ralph/review-log.jsonl" | null
}
```

Always exit 0. BLOCKED is not a subprocess error — the daemon reads verdict from JSON.

`iterations_used` is `0` when Step 0 short-circuits for a non-testable `change_type` (no worker ran). It is `1..max_iterations` when the ralph loop actually executed.

`ralph_done_path` and `review_log_path` are `null` when Step 0 short-circuits (the ralph state directory was never populated). They are non-null when the ralph loop ran.

On SHIP: the worktree working tree contains the implementation diff (or is clean, if the non-testable change was implemented directly by `/task-v2-plan` or is no-op). It may be staged or unstaged. The daemon stages + commits + pushes in the `summary` and `push_and_pr` phases.

On BLOCKED: the working tree may contain partial work. The daemon decides whether to retry (fresh worktree) or diagnose (`§8` Tier 2/3 flow).

---

## Step 0 — Change-type routing (short-circuit for non-testable types)

Before seeding the ralph state directory or invoking `/ralph`, read `plan.change_type` from the input bundle and decide whether a test-driven iteration loop is warranted.

**Non-testable change types** — short-circuit and emit an immediate SHIP:

- `db_migration`
- `docs`
- `dx_tooling`
- `no_deployed_component`

These mirror the original `/task` skill's "non-testable tasks" list (Terraform, DB migrations, CI/CD, docs, investigation) and the underlying `/ralph` skill's "When NOT to use" contract. For these change types, the worker + three-reviewer cycle adds no value — there is no test suite to iterate against, and the reviewers have no acceptance-criteria-mapped-to-tests signal to evaluate. Running ralph on these types burns ~5-20 minutes of wall clock and ~5-10 reviewer Task-tool subagent contexts with nothing to show for it.

**Testable change types** — proceed to Step 0.5 and then Step 1 and run the full worker/reviewer loop:

- `api`
- `scraper`
- `ingestion`
- `web`
- `backfill_script`
- `agent_skill`

For any value not in either list, treat it as testable and proceed to Step 0.5. The plan author (`/task-v2-plan`) owns the enumeration of valid `change_type` values in the spec; this skill fails open (runs the loop) when it sees an unfamiliar value rather than silently short-circuiting.

### Short-circuit implementation

If `plan.change_type` is one of the non-testable values above, emit the following JSON to `{worktree}/tmp/dispatcher-output/ralph.json` and exit 0. Do NOT create `{worktree}/tmp/ralph/`, do NOT invoke `/ralph`, do NOT spawn any worker or reviewer subagents.

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "verdict": "SHIP",
  "iterations_used": 0,
  "block_reason": null,
  "changed_files": [],
  "summary": "Skipped — change_type=<X> does not need test-driven iteration (per /task-v2-ralph routing).",
  "ralph_done_path": null,
  "review_log_path": null
}
```

Substitute `<X>` with the actual change type value. The `changed_files` list is empty because no worker ran; if the plan phase already applied diffs (e.g. pre-computed schema migration), the daemon picks them up directly from `git status` in its `summary` / `push_and_pr` phases.

The daemon consumes this verdict and routes directly to `/task-v2-summary`, which generates the commit message, PR title, PR body, and process-summary issue comment from the plan + diff without needing a reviewer-approved SHIP signal.

If `plan.change_type` is testable (or unrecognized), continue to Step 0.5.

---

## Step 0.5 — Task-tool availability check

The testable path (Step 1 onward) requires the `Task` tool to spawn `/ralph` as a fresh-context subagent. If the skill was invoked via a nested `Skill()` call from another `claude` session (see the **Invocation context** section above), the `Task` tool is not available and the loop cannot run.

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

Note: Step 0's non-testable short-circuit runs **before** this check, so `docs` / `db_migration` / `dx_tooling` / `no_deployed_component` change types still SHIP cleanly from a nested `Skill()` harness — only the testable loop path needs the Task tool.

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

  ## Scope boundary

  <any items from plan.scope_check with in_scope=false — so worker does NOT touch them>
  ```

- `feedback.md` — `No prior feedback. This is the first iteration.`

- `iteration.txt` — `1`

## Step 2 — Invoke `/ralph`

Spawn the `/ralph` skill as a Task-tool subagent (so its own internal worker+reviewer subagents inherit fresh-context isolation). Pass:

- The absolute worktree path.
- The issue number.
- `max_iterations`.

`/ralph` handles the full worker → Gemini standard reviewer → Gemini adversarial reviewer → Claude reviewer cycle, with each sub-invocation spawning its own fresh-context subagent (Task tool). It writes `ralph-done.txt` when any of the following is true:

- All three reviewers returned SHIP → SHIP
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

Ralph is the long-tail phase by design. Each iteration (worker + 3 reviewers) is spawned with fresh context via Task-tool subagents, so the outer `/task-v2-ralph` context only accumulates:

- `plan.json` input (~5-10k tokens).
- Iteration transitions + verdict aggregation (~1-2k tokens per iteration, 5 iterations = ~10k).
- No raw file contents. No test logs. Those live in worker/reviewer subagent contexts and are discarded when those subagents exit.

Expected peak: ~30-45k tokens across 5 iterations. Well inside the 200k limit.

If real Fargate measurement (follow-up #2714) shows the outer ralph exceeds 100k tokens consistently, the spec §6a must sub-split into `/task-v2-ralph-worker` + `/task-v2-ralph-review`, with the daemon orchestrating the loop instead of a long-running outer `claude -p`. Spike 0.3 already pre-designed that sub-split; the shape is documented in `docs/investigations/dispatcher-v2-spike-0.3.md`.

Non-testable short-circuit (Step 0) is effectively zero-cost: a single JSON read, a taxonomy check, and a single JSON write. No subagent spawn, no iteration overhead. The Task-tool availability check (Step 0.5) is similarly cheap: a single tool-registry probe, no subprocess.

## What this skill does NOT do

- **Does not install dependencies.** Daemon already did that in the `setup` phase.
- **Does not commit, push, or open a PR.** Daemon owns git + GitHub.
- **Does not post issue comments.** `/task-v2-summary` owns the pre-PR comment.
- **Does not watch CI or merge.** Daemon's `ci_watch` and `merge` phases handle that; if CI fails, the daemon spawns `/task-v2-fix-ci`.
- **Does not run deploy verification.** `/task-v2-verify` owns that post-deploy.

## Worked example — Step 0 short-circuit for a docs change

Input `plan.json` has `change_type=docs` (the plan phase already produced a documentation diff in the worktree). Step 0 matches `docs` in the non-testable list and emits:

```
{
  "agent_id": "<uuid>",
  "issue_number": 2767,
  "verdict": "SHIP",
  "iterations_used": 0,
  "block_reason": null,
  "changed_files": [],
  "summary": "Skipped — change_type=docs does not need test-driven iteration (per /task-v2-ralph routing).",
  "ralph_done_path": null,
  "review_log_path": null
}
```

Elapsed time: under a second. The daemon proceeds to `/task-v2-summary` and picks up the plan-phase diff from `git status` for the commit.

## Worked example — ralph output for a clean 1-iteration SHIP

Input `plan.json` has `change_type=scraper` — a one-file scraper fix. Step 0 falls through (testable), Step 0.5 confirms the Task tool is available, Step 1 seeds state, Step 2 invokes `/ralph`, worker applies the one-line fix + regression test, all three reviewers agree SHIP on iteration 1. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1, exit.

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

A local smoke test invokes `Skill(skill="task-v2-ralph")` from inside another `claude` session (e.g. from a general-purpose subagent). `plan.change_type=scraper` (testable), so Step 0 falls through. Step 0.5 detects the Task tool is unavailable and emits:

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

Elapsed time: under a second. The operator re-runs the smoke test via `claude -p /task-v2-ralph <agent-id>` directly in a terminal, which has the Task tool available, and the loop runs normally.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Reviewers run synchronously via Task-tool subagents — no `run_in_background`.
- Pre-PR checks (ruff, pytest, lint/typecheck/build) run INSIDE ralph's worker or final verification step — the daemon does NOT re-run them before `git push`, but `.githooks/pre-push` will bail if anything is red. If your worker skipped pre-PR checks, ralph must run them at the end of the final iteration.
- `/ralph` writes status updates to `{repo_root}/tmp/agent-status/<agent-id>.txt` per its convention. `/task-v2-ralph` may read this file for monitoring but should not write to it — the dispatcher daemon owns agent status via `dispatcher.phase_transitions` rows.
