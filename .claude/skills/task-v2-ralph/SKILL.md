---
description: Ralph phase for the per-phase /task-v2 pipeline. Iterative worker + reviewer loop, returns SHIP verdict plus uncommitted implementation diff in the worktree. Long-tail phase (~45-90 min internally).
argument-hint: "<agent-id>"
maxTurns: 500
model: sonnet
---

# /task-v2-ralph skill

Ralph phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Executes the plan produced by `/task-v2-plan` through an iterative worker + reviewer loop, and exits when the implementation is SHIP-ready (or blocked).

**Prerequisites:** The dispatcher daemon has already (a) installed dependencies per `dependencies_to_install` from the plan output, (b) written the input bundle to `{worktree}/tmp/dispatcher-input/ralph.json`.

**Goal:** Produce committed-ready code in the worktree's working tree (staged or unstaged — daemon stages + commits + pushes), plus `{worktree}/tmp/dispatcher-output/ralph.json` with a SHIP/BLOCKED verdict.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, any Task tool call, or any other operation. `/task-v2-ralph` is already a dispatcher-spawned background subprocess — further backgrounding causes completion notifications to surface in the wrong context and loses results.

**IMPORTANT — Subagent isolation.** The context-budget analysis in spike 0.3 (`docs/investigations/dispatcher-v2-spike-0.3.md`) shows `/task-v2-ralph` stays inside the 200k-token window ONLY if each worker + each reviewer runs as a fresh-context subagent (Task tool). Inline workers break this guarantee. Do not inline.

**Implementation choice (per issue #2732):** This skill invokes the existing `/ralph` skill as its inner loop. `/ralph` already implements the worker + three-reviewer cycle with fresh-context Task-tool subagents and per-iteration state files under `{worktree}/tmp/ralph/`. `/task-v2-ralph` is the thin outer wrapper that (a) seeds `task.md` from `plan.json`, (b) invokes `/ralph`, (c) parses `{worktree}/tmp/ralph/ralph-done.txt` into the output JSON. Keeps the implementation in one place and ensures parity with the current `/task` workflow's ralph behavior.

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
  "ralph_done_path": "<worktree>/tmp/ralph/ralph-done.txt",
  "review_log_path": "<worktree>/tmp/ralph/review-log.jsonl"
}
```

Always exit 0. BLOCKED is not a subprocess error — the daemon reads verdict from JSON.

On SHIP: the worktree working tree contains the implementation diff. It may be staged or unstaged. The daemon stages + commits + pushes in the `summary` and `push_and_pr` phases.

On BLOCKED: the working tree may contain partial work. The daemon decides whether to retry (fresh worktree) or diagnose (`§8` Tier 2/3 flow).

---

## Step 0 — Seed ralph state directory

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

## Step 1 — Invoke `/ralph`

Spawn the `/ralph` skill as a Task-tool subagent (so its own internal worker+reviewer subagents inherit fresh-context isolation). Pass:

- The absolute worktree path.
- The issue number.
- `max_iterations`.

`/ralph` handles the full worker → Gemini standard reviewer → Gemini adversarial reviewer → Claude reviewer cycle, with each sub-invocation spawning its own fresh-context subagent (Task tool). It writes `ralph-done.txt` when any of the following is true:

- All three reviewers returned SHIP → SHIP
- `max_iterations` reached without SHIP → REVISE/BLOCKED (ralph writes the final verdict)
- Worker returned STUCK on two consecutive iterations → BLOCKED

Wait synchronously for the subagent to complete. Do not time out from the outer skill — the dispatcher owns the subprocess-wide timeout.

## Step 2 — Parse ralph output

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

## Step 3 — Write output JSON and exit

Use the Write tool to emit `{worktree}/tmp/dispatcher-output/ralph.json` with the fields above. Exit 0.

---

## Context-budget note (spike 0.3)

Ralph is the long-tail phase by design. Each iteration (worker + 3 reviewers) is spawned with fresh context via Task-tool subagents, so the outer `/task-v2-ralph` context only accumulates:

- `plan.json` input (~5-10k tokens).
- Iteration transitions + verdict aggregation (~1-2k tokens per iteration, 5 iterations = ~10k).
- No raw file contents. No test logs. Those live in worker/reviewer subagent contexts and are discarded when those subagents exit.

Expected peak: ~30-45k tokens across 5 iterations. Well inside the 200k limit.

If real Fargate measurement (follow-up #2714) shows the outer ralph exceeds 100k tokens consistently, the spec §6a must sub-split into `/task-v2-ralph-worker` + `/task-v2-ralph-review`, with the daemon orchestrating the loop instead of a long-running outer `claude -p`. Spike 0.3 already pre-designed that sub-split; the shape is documented in `docs/investigations/dispatcher-v2-spike-0.3.md`.

## What this skill does NOT do

- **Does not install dependencies.** Daemon already did that in the `setup` phase.
- **Does not commit, push, or open a PR.** Daemon owns git + GitHub.
- **Does not post issue comments.** `/task-v2-summary` owns the pre-PR comment.
- **Does not watch CI or merge.** Daemon's `ci_watch` and `merge` phases handle that; if CI fails, the daemon spawns `/task-v2-fix-ci`.
- **Does not run deploy verification.** `/task-v2-verify` owns that post-deploy.

## Worked example — ralph output for a clean 1-iteration SHIP

Input `plan.json` has one-file scraper fix. Ralph invokes `/ralph`, worker applies the one-line fix + regression test, all three reviewers agree SHIP on iteration 1. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1, exit.

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

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Reviewers run synchronously via Task-tool subagents — no `run_in_background`.
- Pre-PR checks (ruff, pytest, lint/typecheck/build) run INSIDE ralph's worker or final verification step — the daemon does NOT re-run them before `git push`, but `.githooks/pre-push` will bail if anything is red. If your worker skipped pre-PR checks, ralph must run them at the end of the final iteration.
- `/ralph` writes status updates to `{repo_root}/tmp/agent-status/<agent-id>.txt` per its convention. `/task-v2-ralph` may read this file for monitoring but should not write to it — the dispatcher daemon owns agent status via `dispatcher.phase_transitions` rows.
