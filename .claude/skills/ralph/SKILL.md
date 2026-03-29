---
description: Ralph loop — iterative work-review cycle with fresh context each iteration. Spawns a worker subagent to implement, then runs three reviewers sequentially (Gemini standard, Gemini adversarial, Claude). Loops until all reviewers agree to SHIP or max iterations reached. Called by /task for implementation tasks.
argument-hint: ""
maxTurns: 200
---

# /ralph skill

Implement the current task using a ralph loop: an iterative work-then-review cycle where each iteration runs in fresh context, with state passed between iterations via files. This prevents context pollution from failed attempts and provides cross-perspective review.

**Prerequisites:** You must already be in a worktree with a claimed issue. The worktree path (`{worktree}`) and issue number must be known. Dependencies (venvs, node_modules) must already be installed.

**When to use:** Testable code tasks (Python, TypeScript) — anything where `/tdd` would apply. Called by `/task` Path A in place of the old A.2 + A.3 steps.

**When NOT to use:** Terraform, DB migrations, CI/CD, docs, investigation tasks. For those, implement directly per CLAUDE.md.

**Local dev iteration for ingestion/extraction tasks:** When the task involves the ingestion pipeline, scraper logic, LLM extraction, or enrichment, use the local dev stack for faster iteration. The local DB + S3 cache (`S3_CACHE_DIR=/tmp/judgemind-archive`) enables running the full pipeline locally. After implementing changes, run `scripts/rebuild_db.sh --skip-reset` to re-process documents and verify data correctness against source documents. The LLM result cache makes subsequent rebuilds near-instant. See CLAUDE.md §Local Development Stack. **Prioritize correctness over completeness** — verify extracted values match source documents.

Do not ask for confirmation. Work autonomously through every step.

**IMPORTANT — Ralph is NOT the end of the task.** When this skill completes, the calling `/task` workflow has 8 more mandatory steps remaining (A.2b through A.9: process summary, commit, push, PR, CI, merge, deploy, retrospective). Ralph completing means the code is ready — but the code has not been committed, pushed, reviewed by CI, or merged. Exiting after ralph is a known failure mode (#721).

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation anywhere in the ralph loop. All work runs synchronously in the foreground. Subagents (worker, reviewer) are already background tasks from the parent's perspective — further backgrounding causes completion notifications to surface in the wrong context and leads to lost results.

### Status file

The `/task` skill sets up a status file at `{repo_root}/tmp/agent-status/{agent-id}.txt`. The `/ralph` skill writes status updates to this file at each worker/reviewer phase transition using the Write tool. The format is defined in `/task` Step 0. Derive the status file path from the worktree path (e.g. `.claude/worktrees/agent-ab4722a2` -> `{repo_root}/tmp/agent-status/agent-ab4722a2.txt`, or `worktrees/worker-2` -> `{repo_root}/tmp/agent-status/worker-2.txt`).

---

## Step 0 — Set up ralph state directory

Create the state directory and seed the task file:

```
{worktree}/tmp/ralph/
├── task.md                    # issue body, acceptance criteria, relevant context
├── feedback.md                # reviewer feedback (empty initially, updated each cycle)
├── work-status.txt            # worker writes "COMPLETE" when done
├── review-result.txt          # Claude reviewer writes "SHIP" or "REVISE"
├── diff.txt                   # git diff (pre-generated before reviewers)
├── changed_files.txt          # full content of changed files (pre-generated before reviewers)
├── gemini-review-result.txt   # Gemini standard verdict: "SHIP", "REVISE", or "SKIPPED"
├── gemini-feedback.md         # Gemini standard detailed review feedback
├── adversarial-result.txt     # Gemini adversarial verdict: "SHIP", "REVISE", or "SKIPPED"
├── adversarial-feedback.md    # Gemini adversarial detailed findings
├── iteration.txt              # current iteration number (written before each iteration)
├── review-log.jsonl           # structured review log (appended by gemini_review.py and the loop)
└── ralph-done.txt             # completion signal for the calling /task workflow
```

Write `task.md` with:
- The full issue body
- Acceptance criteria extracted from the issue (list each `- [ ]` checkbox individually)
- Any issue comments from non-bot users (scope clarifications, additional criteria, implementation notes)
- Relevant file paths and patterns from your initial codebase exploration
- Which packages are involved and where their venvs/node_modules are
- Any relevant context from `docs/specs/`

Write `feedback.md` with: `No prior feedback. This is the first iteration.`

---

## Step 1 — Create todo list for the loop

Create todos using `TaskCreate` to track progress through the ralph loop:

1. "Ralph iteration 1 — worker" (activeForm: "Implementing iteration 1")
2. "Ralph iteration 1 — Gemini review" (activeForm: "Gemini reviewing iteration 1")
3. "Ralph iteration 1 — adversarial review" (activeForm: "Adversarial reviewing iteration 1")
4. "Ralph iteration 1 — Claude review" (activeForm: "Claude reviewing iteration 1")

Only create todos for the current iteration. When a REVISE triggers the next iteration, create new todos for that iteration at that time.

Mark each todo `in_progress` when starting and `completed` when done.

---

## Step 2 — The Loop

Set `iteration = 1` and `max_iterations = 5`.

### 2a — Worker phase

Write status: `phase: ralph-worker (iteration N)`, `summary: Worker implementing iteration N`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} "ralph-worker (N)"`

**Write the current iteration number** to `{worktree}/tmp/ralph/iteration.txt` (just the number, e.g. `1`). This file is read by `gemini_review.py` to tag its log records with the correct iteration.

Spawn a **worker subagent** (using the Agent tool) with this prompt structure:

> You are implementing a code task in a ralph loop (iteration N of max 5).
>
> Read these files for your task and any prior feedback:
> - `{worktree}/tmp/ralph/task.md`
> - `{worktree}/tmp/ralph/feedback.md`
>
> Then implement the task using TDD:
> 1. Read the task and feedback files.
> 2. Examine existing code and test patterns in the relevant packages.
> 3. If this is iteration 1, write failing tests first, then implement. If this is iteration 2+, focus on addressing the reviewer's feedback — read the existing implementation, apply the requested changes, and re-run tests.
> 4. Run ALL pre-PR checks for every package you touched:
>    - Python: `.venv/bin/ruff check src/ tests/`, `.venv/bin/ruff format --check src/ tests/`, `.venv/bin/pytest tests/ -v --tb=short`
>    - TypeScript: `npm run lint`, `npm run typecheck`, `npm test`
> 5. Fix any failures. Auto-fix lint with `.venv/bin/ruff check --fix src/ tests/` then `.venv/bin/ruff format src/ tests/`.
> 6. **Run diff-coverage check for every Python package you touched** (catches CI diff-coverage failures locally):
>    - Install diff-cover if not already available: `.venv/bin/pip install diff-cover --quiet`
>    - Ensure pytest generated `coverage.xml` (re-run with `--cov --cov-report=xml` if needed): `.venv/bin/pytest tests/ -v --tb=short --cov=src --cov-report=xml`
>    - Run: `.venv/bin/diff-cover coverage.xml --compare-branch=origin/main --fail-under=90`
>    - If diff-coverage is below 90%, add tests for the uncovered lines before proceeding. The output shows exactly which lines lack coverage — write tests that exercise those code paths.
>    - For TypeScript packages, run: `npx diff-cover coverage/lcov.info --compare-branch=origin/main --fail-under=90` (install with `npm install diff-cover` if needed).
> 6b. **Web Interface Guidelines self-check (conditional).** If any `.tsx` or `.css` files were created or modified in this iteration, read `~/.claude/commands/web-interface-guidelines.md` and check the changed files for clear mechanical violations. Fix any violations found — focus on: missing `aria-hidden` on decorative elements, `focus:outline-none` anti-pattern (use `focus-visible:` instead), `...` instead of `…` (use the Unicode ellipsis character), and missing `name` attributes on form inputs. Skip this step entirely if the diff contains no `.tsx` or `.css` files.
> 7. **Self-verify acceptance criteria before completing.** Read `{worktree}/tmp/ralph/task.md` and find every acceptance criterion (the `- [ ]` checkboxes). For EACH criterion, describe how your implementation satisfies it — reference the specific file, function, or test. If any criterion cannot be verified locally (e.g., requires deployed data), note it as "requires post-deploy verification." If any criterion is NOT addressed by your implementation, do NOT write COMPLETE — instead, note the gap and continue implementing.
> 8. Write your acceptance criteria self-check to `{worktree}/tmp/ralph/acceptance-check.txt` with a table mapping each criterion to its evidence.
> 9. When all checks pass (including diff-coverage >= 90%) AND all locally-verifiable acceptance criteria are addressed, write "COMPLETE" to `{worktree}/tmp/ralph/work-status.txt`.
> 10. If you cannot get checks passing after reasonable effort, write "STUCK" to `work-status.txt` and describe what's failing in `{worktree}/tmp/ralph/stuck-reason.txt`.
>
> Rules:
> - All work happens in `{worktree}`. All temp files go in `{worktree}/tmp/`.
> - No `$()` command substitution. No heredocs. No `python3 -c`. No quoted strings with `&&` or `;`.
> - Do not commit, push, or create PRs. Only implement and verify locally.
> - Follow existing code patterns. Type hints on all Python function signatures. Strict TypeScript mode.
> - **Do not use `run_in_background` on any command.** Run all commands (test suites, lint, format checks) in the foreground and wait for their results before proceeding. You are a subagent — backgrounding causes notifications to surface in the wrong context.

After the worker subagent completes, read `{worktree}/tmp/ralph/work-status.txt`.

- If **STUCK**: Stop the loop. Comment on the issue describing the blocker. Block the issue with `scripts/block-issue.sh <issue> <blocker>` (if a specific blocking issue exists) or add `status/blocked` manually. Return to the caller with failure status.
- If **COMPLETE**: Continue to the sequential review phase.

### 2b — Sequential review phase

Write status: `phase: ralph-reviewer (iteration N)`, `summary: Running three sequential reviewers for iteration N`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} "ralph-reviewer (N)"`

**All three reviewers run sequentially in the foreground.** This eliminates background `<task-notification>` noise that disrupts the dispatcher when multiple agents are running. Do **not** use `run_in_background` for any reviewer.

**Pre-generate diff and changed files before launching reviewers.** This must happen once, before any reviewer starts. Run these two commands sequentially (as separate Bash tool calls):

1. Generate `diff.txt`:
   ```
   git -C {worktree} diff > {worktree}/tmp/ralph/diff.txt
   ```
   Then append staged changes:
   ```
   git -C {worktree} diff --cached >> {worktree}/tmp/ralph/diff.txt
   ```

2. Generate `changed_files.txt` — run `git -C {worktree} diff --name-only HEAD`, `git -C {worktree} diff --cached --name-only`, and `git -C {worktree} ls-files --others --exclude-standard` to get the list of changed files, then write each file's full content to `{worktree}/tmp/ralph/changed_files.txt` using the Read tool and Write tool (or a small script written to `{worktree}/tmp/`). The format is:
   ```
   === path/to/file.py ===
   <full file content>

   === path/to/other.ts ===
   <full file content>
   ```

Both files must exist and be non-empty before launching reviewers. The `gemini-review.sh` script will skip its own diff generation when it detects these files already exist.

**Run all three reviewers sequentially (foreground only — no `run_in_background`), capturing per-reviewer wall-clock time:**

Before each reviewer starts, note the current time (run `date +%s` to get epoch seconds). After each reviewer completes, run `date +%s` again and compute the delta. Store the per-reviewer seconds for the timing detail.

1. **Gemini standard review** — Capture start time, run in the foreground and wait for completion, capture end time:
   ```
   date +%s
   scripts/gemini-review.sh {worktree}
   date +%s
   ```
   Compute: `gemini_standard_secs = end - start`

2. **Gemini adversarial review** — Capture start time, run in the foreground and wait for completion, capture end time:
   ```
   date +%s
   scripts/gemini-review.sh {worktree} --adversarial
   date +%s
   ```
   Compute: `gemini_adversarial_secs = end - start`

3. **Claude reviewer subagent** — Capture start time, spawn via the Agent tool (foreground, after both Gemini reviews have completed), capture end time after the agent completes:
   ```
   date +%s
   ```
   (Run Claude reviewer agent)
   ```
   date +%s
   ```
   Compute: `claude_secs = end - start`

The Claude reviewer prompt should be:

> You are reviewing a code change in a ralph loop (iteration N of max 5). Your job is to evaluate whether the implementation is ready to ship or needs revision. You are a fresh pair of eyes — you did not write this code.
>
> **Both Gemini reviews have already completed.** Read their feedback before starting your own review:
> - `{worktree}/tmp/ralph/gemini-feedback.md` (Gemini standard review)
> - `{worktree}/tmp/ralph/adversarial-feedback.md` (Gemini adversarial review)
>
> 1. Read the task requirements: `{worktree}/tmp/ralph/task.md`
> 2. Read the Gemini feedback files (both should exist since they ran before you).
> 3. Review the complete diff:
>    ```
>    git -C {worktree} diff
>    git -C {worktree} diff --cached
>    git -C {worktree} status
>    ```
>    Also read the actual changed files to understand context beyond the diff.
> 4. **Verify acceptance criteria (MANDATORY).** Extract every acceptance criterion from `task.md` (the `- [ ]` checkboxes). For EACH criterion:
>    - Check whether the implementation satisfies it — look at the code, tests, and the worker's self-check in `{worktree}/tmp/ralph/acceptance-check.txt`.
>    - Mark it as **met**, **not met**, or **requires post-deploy verification**.
>    - If ANY locally-verifiable criterion is **not met**, the review MUST be **REVISE**, regardless of code quality. List the unmet criteria in your feedback.
> 5. Evaluate against these additional criteria:
>    - **Correctness**: Does the implementation satisfy the acceptance criteria in task.md?
>    - **Test coverage**: Are there tests for each acceptance criterion and obvious edge cases?
>    - **Scope completeness**: Does the change need to be applied in other locations too? Search the codebase (using Grep) for other files that use, render, or implement the same pattern being changed. If the fix or feature was applied to one file but the same pattern exists elsewhere without the change, flag it as a REVISE reason. For example, if a rendering fix was applied to `ComponentA.tsx`, check whether `ComponentB.tsx` or other components render the same data and need the same fix.
>    - **Scope creep**: Are there changes unrelated to the issue (extra refactors, unrelated fixes)?
>    - **Code quality**: Does it follow existing patterns? Any debug code, hardcoded values, or forgotten TODOs?
>    - **Missing pieces**: Are there files that should have been created or modified but weren't?
>    - **Stale references**: Do comments, imports, or docstrings reference things that changed?
>    - **Documentation consistency**: If the change modifies behavior, configuration, or interfaces — do related docs (`docs/`, `CLAUDE.md`, `.claude/skills/`, `README.md`, `CONTRIBUTING.md`) need corresponding updates? Flag any docs that reference the old behavior.
>    - **Performance**: Are there obvious bottlenecks? Sequential I/O that could be parallelized? O(n^2) patterns (e.g. LIMIT/OFFSET pagination, nested loops over large datasets)? Missing connection pooling or batching for network calls (DB, S3, HTTP)?
>    - **Unchecked test plan items**: If the PR includes a test plan with checkboxes, any unchecked items are **merge blockers**. An unchecked item means something was not verified — flag it as a REVISE reason. The author must either check the item (verify it) or remove it (not applicable).
>    - **Web Interface Guidelines (conditional)**: If any changed files have `.tsx` or `.css` extensions, read `~/.claude/commands/web-interface-guidelines.md` and check those files against its rules. Report violations as REVISE reasons with `file:line` format. Skip this criterion entirely if no `.tsx` or `.css` files appear in the diff.
> 6. Make a binary decision:
>    - **SHIP**: The implementation is correct, well-tested, properly scoped, ALL locally-verifiable acceptance criteria are met, and ready for PR. Write "SHIP" to `{worktree}/tmp/ralph/review-result.txt`.
>    - **REVISE**: Something needs to change. Write "REVISE" to `{worktree}/tmp/ralph/review-result.txt`. Then write specific, actionable feedback to `{worktree}/tmp/ralph/feedback.md` — describe exactly what needs to change and why. Be concrete: reference specific files, functions, and line numbers. **If any acceptance criterion is unmet, list it first in your feedback.**
>
> Scope boundaries:
> - **Only flag issues introduced or modified by this diff.** Pre-existing code patterns that were not changed in this PR are out of scope — even if they look questionable. The worker is not responsible for fixing code they did not touch.
> - **Check the architecture spec before flagging missing functionality.** The system has multiple pipeline stages (capture, transcription, enrichment). If you think a feature is missing, it may be handled by a different stage. Do not flag "missing extraction" in a transcription module if extraction is an enrichment responsibility, for example.
> - **Do not flag cross-concern gaps** unless the diff explicitly claims to address them. If the task requirements say "implement X," do not REVISE because Y is also missing — Y is a separate task.
>
> Rules:
> - Be rigorous but not pedantic. Don't request style changes that don't affect correctness or readability.
> - Don't request changes outside the scope of the task.
> - Don't flag pre-existing patterns not introduced by this diff.
> - **Unmet acceptance criteria are always REVISE.** Never SHIP if any locally-verifiable acceptance criterion is not satisfied.
> - If you say REVISE, your feedback must be specific enough that the worker can act on it without guessing.
> - **Unchecked test plan items are always blockers.** Never approve a PR with unchecked test plan checkboxes.
> - **Do not use `run_in_background` on any command.** You are a subagent — all commands must run in the foreground.

**After all three reviewers complete**, end the reviewer phase with per-reviewer timing detail:
```
python3 {worktree}/scripts/phase_timer.py end {worktree} --detail '{"gemini_standard": <gemini_standard_secs>, "gemini_adversarial": <gemini_adversarial_secs>, "claude": <claude_secs>}'
```

### 2c — Collect results

After all three reviewers complete, read:
- `{worktree}/tmp/ralph/gemini-review-result.txt` (Gemini standard)
- `{worktree}/tmp/ralph/adversarial-result.txt` (Gemini adversarial)
- `{worktree}/tmp/ralph/review-result.txt` (Claude)

### 2d — Log Claude review record

After reading the Claude reviewer's verdict and feedback, log a structured review record by writing and running a small Python script.

Write the following to `{worktree}/tmp/ralph/log_claude_review.py` (substituting actual paths for `{worktree}` and `{repo_root}`):

```python
import sys
sys.path.insert(0, "<repo_root>/scripts")
from ralph_review_log import log_review, compute_diff_stats
from pathlib import Path

state_dir = Path("<worktree>/tmp/ralph")
worktree = Path("<worktree>")

feedback = state_dir.joinpath("feedback.md").read_text(encoding="utf-8") if state_dir.joinpath("feedback.md").exists() else ""
verdict = state_dir.joinpath("review-result.txt").read_text(encoding="utf-8").strip()
iteration = int(state_dir.joinpath("iteration.txt").read_text(encoding="utf-8").strip())
diff_stats = compute_diff_stats(worktree)

log_review(
    state_dir,
    iteration=iteration,
    model="claude",
    verdict=verdict,
    feedback=feedback,
    diff_stats=diff_stats,
)
```

Run this script with any available Python 3 interpreter (e.g. a venv python from one of the packages, or `python3`). The `ralph_review_log` module is stdlib-only and does not need a venv.

### 2e — Decision logic

**All three reviewers must agree to SHIP (with persistent-dissent override):**

- If Claude says **SHIP** AND (Gemini standard says **SHIP** or **SKIPPED**) AND (Gemini adversarial says **SHIP** or **SKIPPED**): The loop is done. Continue to Step 3.

- **Persistent-dissent override:** If ANY reviewer says **REVISE**, first check whether this is a persistent solo-dissent pattern. Write and run a small Python script (`{worktree}/tmp/ralph/check_dissent.py`):

  ```python
  import sys
  sys.path.insert(0, "<repo_root>/scripts")
  from ralph_review_log import detect_persistent_dissent, log_dissent_override
  from pathlib import Path
  import json

  state_dir = Path("<worktree>/tmp/ralph")
  iteration = int(state_dir.joinpath("iteration.txt").read_text(encoding="utf-8").strip())

  result = detect_persistent_dissent(state_dir, current_iteration=iteration)
  if result:
      log_dissent_override(
          state_dir,
          iteration=iteration,
          dissenter=result["dissenter"],
          consecutive_count=result["consecutive_count"],
          dissent_iterations=result["iterations"],
      )
      print(json.dumps(result))
  else:
      print("NONE")
  ```

  - If the script outputs a JSON object (not "NONE"), a persistent solo-dissent was detected. **Treat the loop as SHIP** — the dissenter has been overriding the same concern for 2+ consecutive iterations while the other reviewers agreed to ship. The override has been logged to `review-log.jsonl` for audit. Continue to Step 3.
  - If the script outputs "NONE", no persistent dissent was detected. Proceed with the normal REVISE logic below.

- If ANY reviewer says **REVISE** (and no persistent-dissent override applies): Increment iteration. If `iteration > max_iterations`, stop the loop and comment on the issue that the ralph loop hit its max iterations — block the issue with `scripts/block-issue.sh <issue> <blocker>` (if applicable) or add `status/blocked` manually, and return with failure. Otherwise:
  - Consolidate feedback from ALL reviewers that said REVISE into `{worktree}/tmp/ralph/feedback.md`. Include feedback from `gemini-feedback.md`, `adversarial-feedback.md`, and/or `feedback.md` as appropriate.
  - Create new todos for the next iteration ("Ralph iteration N — worker", "Ralph iteration N — Gemini review", "Ralph iteration N — adversarial review", "Ralph iteration N — Claude review"), then return to 2a.

---

## Step 3 — Log summary and return to caller

The reviewers have approved the implementation. Before returning, log a summary record.

### 3a — Log review summary

Write the following to `{worktree}/tmp/ralph/log_summary.py` (substituting actual paths):

```python
import sys
sys.path.insert(0, "<repo_root>/scripts")
from ralph_review_log import log_summary
from pathlib import Path

state_dir = Path("<worktree>/tmp/ralph")
iteration = int(state_dir.joinpath("iteration.txt").read_text(encoding="utf-8").strip())

log_summary(
    state_dir,
    total_iterations=iteration,
    final_verdict="SHIP",
)
```

Run this script with any available Python 3 interpreter.

If the loop ended due to max iterations (not SHIP), change `final_verdict` to `"MAX_ITERATIONS"` before running.

### 3b — Write completion signal and return to caller

**CRITICAL:** Write a completion signal file so the calling `/task` workflow can verify ralph finished and knows what to do next. This file serves as a handoff contract between the two skills.

Write `{worktree}/tmp/ralph/ralph-done.txt` with exactly this content (substituting the actual iteration count):

```
status: SHIP
iterations: <N>
next-steps: The /task workflow MUST now continue with: A.2b (process summary), A.3 (stage, commit, push), A.4 (verify no merge conflicts), A.5 (monitor CI), A.6 (update PR test plan), A.7 (merge PR), A.8 (verify deployment, functional health, and acceptance criteria if applicable), A.9 (retrospective). THE TASK IS NOT COMPLETE UNTIL ALL THESE STEPS FINISH.
```

The code is ready for commit. Return control to the calling workflow (`/task` Path A), which handles process summary, staging, committing, pushing, PR creation, CI monitoring, and cleanup.

**Do not commit, push, or open a PR from this skill.**

**DO NOT EXIT OR STOP after writing this file.** The `/task` workflow has 8 more mandatory steps (A.2b through A.9) that MUST execute after ralph completes. Ralph completing is the HALFWAY POINT of the task, not the end. The code is implemented but has not been committed, pushed, or merged. Exiting here is a known failure mode — see issue #721. You MUST return control to the calling `/task` workflow so it can continue with process summary, commit, push, PR creation, CI monitoring, merge, deployment verification, and retrospective.

---

## Guardrails

- **Max 5 iterations.** If the loop doesn't converge, escalate to human via issue comment and `scripts/block-issue.sh` (or `status/blocked` label).
- **Worker and reviewers are separate.** Never combine them — the cross-perspective review is the point.
- **File-based state only.** No information passes between iterations except through the ralph state files.
- **All standard rules apply.** No `$()`, no heredocs, no inline Python, temp files in `{worktree}/tmp/`.
- **Gemini reviews are best-effort.** If the Google API key is unavailable or an API call fails, the loop continues with the remaining reviewers. Gemini reviews never block the loop.
- **Ralph is not task completion.** Ralph handles implementation and review only. The calling `/task` workflow handles process summary, commit, push, PR, CI, merge, deploy, and cleanup. Never exit after ralph without completing the full `/task` workflow.
- **Unchecked test plan items are merge blockers.** Reviewers must flag unchecked test plan checkboxes as REVISE reasons. A PR with unchecked items is not ready to ship.
- **Unmet acceptance criteria are always REVISE.** Reviewers must verify every acceptance criterion individually. Code quality alone is not sufficient for SHIP — all locally-verifiable acceptance criteria must be met.
- **Persistent-dissent override.** If one reviewer says REVISE for 2+ consecutive iterations while the other two say SHIP, and the detection function (`detect_persistent_dissent`) confirms the pattern, the loop treats it as SHIP. This prevents a single reviewer from blocking convergence on theoretical grounds that the other reviewers have already evaluated and dismissed. The override is logged to `review-log.jsonl` with type `dissent_override` for audit. This override only applies when exactly one reviewer dissents — if two reviewers REVISE, that is a genuine concern and the override does not trigger.
- **Do not use `run_in_background` anywhere in the ralph loop.** All commands — test suites, lint, format checks, git commands, reviewer invocations, and worker subagents — must run in the foreground. Subagents are already running as background tasks from the parent's perspective. Further backgrounding causes completion notifications to route to the wrong context, leading to confusion and lost results.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
- **No `run_in_background`.** All work runs synchronously — see Guardrails above.
