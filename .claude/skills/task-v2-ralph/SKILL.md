---
description: (WIP — dispatcher v2 spike 0.3 stub) Ralph phase for the per-phase /task-v2 pipeline. Iterative worker + reviewer loop, returns SHIP verdict + committed-ready diff in the worktree. Long-tail phase (~45-90 min internally).
argument-hint: ""
maxTurns: 500
model: opus
---

# /task-v2-ralph skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` + `.claude/skills/ralph/SKILL.md` for dispatcher v2 spike 0.3 (per-phase context budget measurement, issue #2685). The long-tail phase that this spike stress-tests.

**Goal:** Implement the plan produced by `/task-v2-plan`, iterate with reviewer feedback, return when reviewers agree SHIP (or give up with a blocker signal).

**Input:** `{worktree}/tmp/dispatcher-input/ralph.json`:
- `plan` (object from `/task-v2-plan` output)
- `issue_number` (int)
- `worktree_path` (str)
- `repo_root` (str)
- `max_iterations` (int, default 5)

**Output:** `{worktree}/tmp/dispatcher-output/ralph.json`:
- `verdict` (str) — `SHIP` or `BLOCKED`
- `iterations_used` (int)
- `block_reason` (str or null) — if verdict=BLOCKED
- `changed_files` (list of paths)
- `summary` (str, 1-3 sentences) — what was implemented

On success the worktree has uncommitted changes in working tree; the daemon stages, commits, and pushes.

---

## Step 0 — Set up ralph state directory

Create `{worktree}/tmp/ralph/` to hold iteration artifacts. Seed `task.md` from the plan:
```
task.md           # plan + acceptance criteria + relevant paths
feedback.md       # reviewer feedback (empty initially, updated each cycle)
iteration.txt     # current iteration number
diff.txt          # pre-generated git diff before each review
changed_files.txt # full content of changed files (pre-generated before review)
```

## Step 1 — Iteration loop

For iteration 1..max_iterations:

1. **Worker phase.** Spawn a worker subagent (Task tool) with `task.md` + previous `feedback.md` as input. Worker writes failing tests first, implements until green, runs pre-PR checks (ruff, pytest, lint). Worker writes `work-status.txt` = `COMPLETE` when done.

2. **Pre-review artifact generation.** Compute `git diff` into `diff.txt` and capture full content of changed files into `changed_files.txt`. These go to reviewers as input; reviewers receive git state, not raw Read calls.

3. **Reviewer phase.** Spawn three reviewers in sequence (synchronous — no backgrounding):
   - Gemini standard (`scripts/gemini_review.py` mode=standard) — normal code review.
   - Gemini adversarial (`scripts/gemini_review.py` mode=adversarial) — devil's advocate: "find what's wrong".
   - Claude reviewer (Task tool) — final verdict.
   Each writes verdict `SHIP | REVISE | SKIPPED` and a feedback file.

4. **Verdict check.** If all three reviewers return SHIP, loop exits with `verdict=SHIP`.
   If any reviewer says REVISE, aggregate feedback into `feedback.md` and continue to iteration N+1.
   If iteration == max_iterations and we still don't have SHIP, exit with `verdict=BLOCKED`, `block_reason="max_iterations reached without SHIP"`.

5. **STUCK detection.** If worker returns with the same error on two consecutive iterations without improvement, exit with `verdict=BLOCKED`, `block_reason="worker stuck on <error>"`.

## Step 2 — Write output JSON

Write `{worktree}/tmp/dispatcher-output/ralph.json` with the final verdict and iteration count. Exit 0 regardless of SHIP or BLOCKED — the daemon reads the verdict from the JSON, not from the exit code.

---

## Context budget note (spike 0.3)

Ralph is the long-tail phase by design. Each iteration (worker + reviewers) is already spawned with fresh context via Task-tool subagents, so the outer `/task-v2-ralph` context only accumulates:
- `plan.json` input (~5-10k tokens)
- iteration transitions + verdict aggregation (~1-2k tokens per iteration)
- no raw file contents, no test logs (those live in worker/reviewer subagent context)

If spike 0.3 measurements show the outer ralph exceeds 150k tokens across 5 iterations, the spec §6a must sub-split into `/task-v2-ralph-worker` + `/task-v2-ralph-review`, with the daemon orchestrating the loop instead of a long-running outer claude -p.

## Reminders

- No backgrounding. Reviewers run synchronously.
- No `$()`, no heredocs, no `python -c`. See CLAUDE.md Critical Rules.
- All temp files go in `{worktree}/tmp/`.
