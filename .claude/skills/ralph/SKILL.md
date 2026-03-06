---
description: Ralph loop — iterative work-review cycle with fresh context each iteration. Spawns a worker subagent to implement, then a reviewer subagent to evaluate. Loops until the reviewer says SHIP or max iterations reached. Called by /task for implementation tasks.
argument-hint: ""
---

# /ralph skill

Implement the current task using a ralph loop: an iterative work-then-review cycle where each iteration runs in fresh context, with state passed between iterations via files. This prevents context pollution from failed attempts and provides cross-perspective review.

**Prerequisites:** You must already be in a worktree with a claimed issue. The worktree path (`{worktree}`) and issue number must be known. Dependencies (venvs, node_modules) must already be installed.

**When to use:** Testable code tasks (Python, TypeScript) — anything where `/tdd` would apply. Called by `/task` Path A in place of the old A.2 + A.3 steps.

**When NOT to use:** Terraform, DB migrations, CI/CD, docs, investigation tasks. For those, implement directly per CLAUDE.md.

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Set up ralph state directory

Create the state directory and seed the task file:

```
{worktree}/tmp/ralph/
├── task.md            # issue body, acceptance criteria, relevant context
├── feedback.md        # reviewer feedback (empty initially, updated each cycle)
├── work-status.txt    # worker writes "COMPLETE" when done
└── review-result.txt  # reviewer writes "SHIP" or "REVISE"
```

Write `task.md` with:
- The full issue body
- Acceptance criteria extracted from the issue
- Relevant file paths and patterns from your initial codebase exploration
- Which packages are involved and where their venvs/node_modules are
- Any relevant context from `docs/specs/`

Write `feedback.md` with: `No prior feedback. This is the first iteration.`

---

## Step 1 — The Loop

Set `iteration = 1` and `max_iterations = 5`.

### 1a — Worker phase

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
> 6. When all checks pass, write "COMPLETE" to `{worktree}/tmp/ralph/work-status.txt`.
> 7. If you cannot get checks passing after reasonable effort, write "STUCK" to `work-status.txt` and describe what's failing in `{worktree}/tmp/ralph/stuck-reason.txt`.
>
> Rules:
> - All work happens in `{worktree}`. All temp files go in `{worktree}/tmp/`.
> - No `$()` command substitution. No heredocs. No `python3 -c`. No quoted strings with `&&` or `;`.
> - Do not commit, push, or create PRs. Only implement and verify locally.
> - Follow existing code patterns. Type hints on all Python function signatures. Strict TypeScript mode.

After the worker subagent completes, read `{worktree}/tmp/ralph/work-status.txt`.

- If **STUCK**: Stop the loop. Comment on the issue describing the blocker. Add `status/blocked`. Return to the caller with failure status.
- If **COMPLETE**: Continue to the review phase.

### 1b — Review phase

Spawn a **reviewer subagent** (using the Agent tool) with this prompt structure:

> You are reviewing a code change in a ralph loop (iteration N of max 5). Your job is to evaluate whether the implementation is ready to ship or needs revision. You are a fresh pair of eyes — you did not write this code.
>
> 1. Read the task requirements: `{worktree}/tmp/ralph/task.md`
> 2. Review the complete diff:
>    ```
>    git -C {worktree} diff
>    git -C {worktree} diff --cached
>    git -C {worktree} status
>    ```
>    Also read the actual changed files to understand context beyond the diff.
> 3. Evaluate against these criteria:
>    - **Correctness**: Does the implementation satisfy the acceptance criteria in task.md?
>    - **Test coverage**: Are there tests for each acceptance criterion and obvious edge cases?
>    - **Scope**: Are there changes unrelated to the issue (scope creep, extra refactors, unrelated fixes)?
>    - **Code quality**: Does it follow existing patterns? Any debug code, hardcoded values, or forgotten TODOs?
>    - **Missing pieces**: Are there files that should have been created or modified but weren't?
>    - **Stale references**: Do comments, imports, or docstrings reference things that changed?
> 4. Make a binary decision:
>    - **SHIP**: The implementation is correct, well-tested, properly scoped, and ready for PR. Write "SHIP" to `{worktree}/tmp/ralph/review-result.txt`.
>    - **REVISE**: Something needs to change. Write "REVISE" to `{worktree}/tmp/ralph/review-result.txt`. Then write specific, actionable feedback to `{worktree}/tmp/ralph/feedback.md` — describe exactly what needs to change and why. Be concrete: reference specific files, functions, and line numbers.
>
> Rules:
> - Be rigorous but not pedantic. Don't request style changes that don't affect correctness or readability.
> - Don't request changes outside the scope of the task.
> - If tests pass and the acceptance criteria are met, lean toward SHIP.
> - If you say REVISE, your feedback must be specific enough that the worker can act on it without guessing.

After the reviewer subagent completes, read `{worktree}/tmp/ralph/review-result.txt`.

- If **SHIP**: The loop is done. Continue to Step 2.
- If **REVISE**: Increment iteration. If `iteration > max_iterations`, stop the loop and comment on the issue that the ralph loop hit its max iterations — add `status/blocked` and return with failure. Otherwise, return to 1a.

---

## Step 2 — Done — Return to Caller

The reviewer has approved the implementation. The code is ready for commit.

Return control to the calling workflow (`/task` Path A), which handles staging, committing, pushing, PR creation, CI monitoring, and cleanup.

**Do not commit, push, or open a PR from this skill.**

---

## Guardrails

- **Max 5 iterations.** If the loop doesn't converge, escalate to human via issue comment + `status/blocked`.
- **Worker and reviewer are separate subagents.** Never combine them — the cross-perspective review is the point.
- **File-based state only.** No information passes between iterations except through the ralph state files.
- **All standard rules apply.** No `$()`, no heredocs, no inline Python, temp files in `{worktree}/tmp/`.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
