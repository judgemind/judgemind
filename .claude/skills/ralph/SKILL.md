---
description: Ralph loop — iterative work-review cycle with fresh context each iteration. Spawns a worker subagent to implement, then runs up to three reviewers sequentially (Gemini standard, Gemini adversarial, Claude). Loops until all required reviewers agree to SHIP or max iterations reached. Called by /task for implementation tasks.
argument-hint: ""
maxTurns: 200
---

# /ralph skill

Implement the current task using a ralph loop: an iterative work-then-review cycle where each iteration runs in fresh context, with state passed between iterations via files. This prevents context pollution from failed attempts and provides cross-perspective review.

**Prerequisites:** You must already be in a worktree with a claimed issue. The worktree path (`{worktree}`) and issue number must be known. Dependencies (venvs, node_modules) must already be installed.

**When to use:** Code, docs, agent-skill, dx-tooling, or any other in-repo implementation task the calling workflow hands off. Ralph adapts its inner behavior to the change type — see §"Change-type-aware behavior" below.

- **Called by `/task` (laptop dispatcher)** Path A for testable code tasks (Python, TypeScript). The caller handles non-testable tasks itself in the legacy path; this is unchanged for laptop workflows.
- **Called by `/task-v2-ralph` (dispatcher v2)** for every change type. The per-phase pipeline has no short-circuit: plan is read-only, ralph always implements. Non-testable types (docs, db_migration, dx_tooling, no_deployed_component) take the single-reviewer, no-TDD branch.

**Local dev iteration for ingestion/extraction tasks:** When the task involves the ingestion pipeline, scraper logic, LLM extraction, or enrichment, use the local dev stack for faster iteration. The local DB + S3 cache (`S3_CACHE_DIR=/tmp/judgemind-archive`) enables running the full pipeline locally. After implementing changes, run `scripts/rebuild_db.sh --skip-reset` to re-process documents and verify data correctness against source documents. The LLM result cache makes subsequent rebuilds near-instant. See `docs/agent/local-dev.md`. **Prioritize correctness over completeness** — verify extracted values match source documents.

Do not ask for confirmation. Work autonomously through every step.

**IMPORTANT — Ralph is NOT the end of the task.** When this skill completes, the calling `/task` workflow has 8 more mandatory steps remaining (A.2b through A.9: process summary, commit, push, PR, CI, merge, deploy, retrospective). Ralph completing means the code is ready — but the code has not been committed, pushed, reviewed by CI, or merged. Exiting after ralph is a known failure mode (#721). (Under dispatcher v2, the equivalent steps are owned by the daemon's `summary`, `push_and_pr`, `ci_watch`, `merge`, `deploy_watch`, `verify`, and `retro` phases.)

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation anywhere in the ralph loop. All work runs synchronously in the foreground. Subagents (worker, reviewer) are already background tasks from the parent's perspective — further backgrounding causes completion notifications to surface in the wrong context and leads to lost results.

### Change-type-aware behavior

Ralph reads `## Testable` from `{worktree}/tmp/ralph/task.md` (seeded by the calling workflow — `/task` or `/task-v2-ralph`) and branches. If the file lacks a `## Testable` line, treat it as `yes` (fail-open — this preserves the existing `/task` laptop-dispatcher behavior, which never set this line).

| `## Testable` | Worker runs | Reviewer(s) run | Rationale |
|---|---|---|---|
| `yes` (testable) | Full TDD: failing tests first, then implement, then full pre-PR checks (ruff, pytest, lint/typecheck, diff-coverage ≥ 90%) | Gemini standard → Gemini adversarial → Claude (all three required, with persistent-dissent override) | Code change benefits from TDD and triple-reviewer cross-perspective. |
| `no` (non-testable: `docs`, `db_migration`, `dx_tooling`, `no_deployed_component`) | Implement the plan's "What will change" section directly; run pre-PR checks that apply to touched file types (ruff/format on any `.py`, markdown-link check on any `.md`, terraform fmt on any `.tf`, etc.); skip pytest and diff-coverage gates | Claude reviewer only (skip both Gemini passes) | Pure documentation, CI-config, or migration-SQL edits have no test suite to iterate against. The Gemini code-review passes add no signal on these diffs. |

When `## Testable: no`, the reviewer MUST NOT issue a REVISE for "no tests added" or "diff-coverage gate not satisfied" — those checks do not apply to the non-testable branch. Reviewer still verifies acceptance criteria, correctness, scope, stale references, and docs consistency.

### AC_INFEASIBLE emit rules — when the worker + reviewer should raise this verdict

The inner `/ralph` loop normally returns `SHIP` (reviewers approved) or `REVISE`/`BLOCKED` (max iterations / worker stuck). Starting with issue #3010, both the worker and the Claude reviewer may also surface `AC_INFEASIBLE` when one or more acceptance criteria cannot be satisfied as written — see [spec §6a `^ralph-ac-infeasible` footnote](../../../docs/specs/dispatcher-v2-spec.md). This is distinct from "hard to implement" (that stays a SHIP-after-iterations outcome) and from "max iterations reached" (that stays `ralph_max_iterations`).

**Positive triggers — any one is sufficient to raise the verdict:**

1. **Non-existent symbol.** The AC references a CLI flag, function, module, environment variable, file path, config key, or AWS resource that does NOT exist in the current codebase AND is NOT introduced by this PR's diff. Example: an AC reads `Verify: scripts/rebuild_db.py --court oc-riverside flushes the staging table` but `grep -n "\-\-court" scripts/rebuild_db.py` returns nothing and the PR doesn't add it. Cite the grep/find output as evidence.
2. **Self-contradiction between ACs.** Two ACs in the same issue demand mutually exclusive outcomes. Example: AC #2 says "extracted judge names must preserve middle initials" and AC #5 says "normalize judge names to `Last, First` form without initials". Cite both AC indices.
3. **Out-of-scope dependency.** The AC depends on work another not-yet-merged issue owns, AND the blocking relationship is not listed in the issue body's `Blocked by` section. Example: an AC depends on a new DB column `derived.rulings.enrichment_v2_confidence` that is owned by a sibling open issue; attempting to implement it here would duplicate schema changes. Cite the sibling issue number.

**Negative guardrails — do NOT raise `AC_INFEASIBLE` for any of these:**

- **"Hard to implement."** If the AC is legitimate but the implementation is tricky, keep iterating. Partial progress + clearer reviewer feedback is the right path; the verdict is `SHIP` (when done) or `ralph_max_iterations` (when budget is out), not `AC_INFEASIBLE`.
- **Max iterations exhausted.** Hitting `max_iterations` is its own failure mode (`ralph_max_iterations` → BLOCKED with that reason). An iteration-budget exhaustion is not structural impossibility — the remedy is a retry with narrower scope, not a diagnoser reissue.
- **Ambiguous wording.** If an AC's wording is ambiguous but reasonable implementations exist, pick the most defensible one and note the ambiguity in the PR body. Reserve `AC_INFEASIBLE` for ACs where NO implementation satisfies the literal text.
- **Missing test fixture that the worker can create.** If the AC requires a fixture file, the worker should create it. A "fixture doesn't exist yet" situation is worker scope, not structural impossibility.

**Emit mechanics.** Both the worker and the Claude reviewer may surface infeasibility. The worker signals it by writing:

- `AC_INFEASIBLE` to `{worktree}/tmp/ralph/work-status.txt` (in place of `COMPLETE` / `STUCK`), AND
- a JSON array of `{"index": <1-based>, "evidence": "<paragraph>"}` objects to `{worktree}/tmp/ralph/infeasible-acs.json`.

The Claude reviewer signals it by writing:

- `AC_INFEASIBLE` to `{worktree}/tmp/ralph/review-result.txt` (in place of `SHIP` / `REVISE`), AND
- the same JSON array shape to `{worktree}/tmp/ralph/infeasible-acs.json` (append or merge with the worker's entries — dedupe by `index`).

When either path writes the verdict, the outer loop (§2e) terminates with `ralph-done.txt` = `AC_INFEASIBLE` and the outer `/task-v2-ralph` wrapper reads `infeasible-acs.json` to populate its `infeasible_acs` output field. The daemon then routes to the diagnoser per spec §8.

**Evidence is load-bearing.** Every `infeasible_acs` entry MUST cite concrete evidence (grep result, file path, conflicting AC index) so the diagnoser can recommend `reissue` vs. `escalate` vs. `close` without re-running the investigation. Paragraphs like "this feels wrong" or "I don't think this is right" are not evidence.

---

### Status file

The `/task` skill sets up a status file at `{worktree}/tmp/agent-status.txt`. The `/ralph` skill writes status updates to this file at each worker/reviewer phase transition using the Write tool. The format is defined in `/task` Step 0.

Under dispatcher v2, `/task-v2-ralph` does not provision this status file — the daemon owns agent status via `dispatcher.phase_transitions` rows. If the status file does not exist when ralph starts, skip the status-write steps silently (best-effort observability, not a correctness gate).

---

## Step 0 — Set up ralph state directory

Create the state directory and seed the task file:

```
{worktree}/tmp/ralph/
├── task.md                    # issue body, acceptance criteria, relevant context, ## Testable line
├── feedback.md                # reviewer feedback (empty initially, updated each cycle)
├── work-status.txt            # worker writes "COMPLETE" when done
├── review-result.txt          # Claude reviewer writes "SHIP" or "REVISE"
├── diff.txt                   # git diff (pre-generated before reviewers)
├── changed_files.txt          # full content of changed files (pre-generated before reviewers)
├── gemini-review-result.txt   # Gemini standard verdict: "SHIP", "REVISE", or "SKIPPED" (testable only)
├── gemini-feedback.md         # Gemini standard detailed review feedback (testable only)
├── adversarial-result.txt     # Gemini adversarial verdict: "SHIP", "REVISE", or "SKIPPED" (testable only)
├── adversarial-feedback.md    # Gemini adversarial detailed findings (testable only)
├── iteration.txt              # current iteration number (written before each iteration)
├── review-log.jsonl           # structured review log (appended by gemini_review.py and the loop)
├── touched-packages.txt       # Python packages with code changes (written by worker step 4)
├── infeasible-acs.json        # written only on AC_INFEASIBLE verdict (worker / reviewer)
└── ralph-done.txt             # completion signal for the calling /task workflow
```

Write `task.md` with:
- The full issue body
- Acceptance criteria extracted from the issue (list each `- [ ]` checkbox individually)
- Any issue comments from non-bot users (scope clarifications, additional criteria, implementation notes)
- Relevant file paths and patterns from your initial codebase exploration
- Which packages are involved and where their venvs/node_modules are
- Any relevant context from `docs/specs/`
- A `## Testable` line with `yes` or `no` (see §"Change-type-aware behavior"). `/task-v2-ralph` always writes this line; the legacy `/task` caller may omit it (ralph treats missing as `yes`).
- A `## Prior attempts (optional)` section if `{worktree}/tmp/dispatcher-output/prior_attempts.md` exists — verbatim content of that file. Omit the section entirely when the file is absent (first-attempt case).

Write `feedback.md` with: `No prior feedback. This is the first iteration.`

---

## Step 1 — Create todo list for the loop

Create todos using `TaskCreate` to track progress through the ralph loop:

**Testable branch (`## Testable: yes` or missing):**

1. "Ralph iteration 1 — worker" (activeForm: "Implementing iteration 1")
2. "Ralph iteration 1 — Gemini review" (activeForm: "Gemini reviewing iteration 1")
3. "Ralph iteration 1 — adversarial review" (activeForm: "Adversarial reviewing iteration 1")
4. "Ralph iteration 1 — Claude review" (activeForm: "Claude reviewing iteration 1")

**Non-testable branch (`## Testable: no`):**

1. "Ralph iteration 1 — worker" (activeForm: "Implementing iteration 1")
2. "Ralph iteration 1 — Claude review" (activeForm: "Claude reviewing iteration 1")

Only create todos for the current iteration. When a REVISE triggers the next iteration, create new todos for that iteration at that time.

Mark each todo `in_progress` when starting and `completed` when done.

---

## Step 2 — The Loop

Set `iteration = 1` and `max_iterations = 5`.

### 2a — Worker phase

Write status: `phase: ralph-worker (iteration N)`, `summary: Worker implementing iteration N`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} "ralph-worker (N)"`

**Write the current iteration number** to `{worktree}/tmp/ralph/iteration.txt` (just the number, e.g. `1`). This file is read by `gemini_review.py` to tag its log records with the correct iteration.

Before spawning the worker, read `{worktree}/tmp/ralph/task.md` and check for `## Testable`. If the value is `no`, spawn the **non-testable worker** (below). Otherwise (value is `yes` or the line is missing), spawn the **testable worker** (below).

#### Testable worker prompt

Spawn a **worker subagent** (using the Agent tool) with this prompt structure:

> You are implementing a code task in a ralph loop (iteration N of max 5). This is a **testable** change — tests are expected.
>
> Read these files for your task and any prior feedback:
> - `{worktree}/tmp/ralph/task.md`
> - `{worktree}/tmp/ralph/feedback.md`
>
> **If `task.md` contains a `## Prior attempts` section**, read it carefully. It lists failures from previous ralph runs for this same issue — including the failure category, push-time stderr tail, and ralph iteration narrative. Address the listed failures directly rather than re-discovering them. The absence of a `## Prior attempts` section means this is the first attempt.
>
> Then implement the task using TDD:
> 1. Read the task and feedback files.
> 2. Examine existing code and test patterns in the relevant packages.
> 3. If this is iteration 1, write failing tests first, then implement. If this is iteration 2+, focus on addressing the reviewer's feedback — read the existing implementation, apply the requested changes, and re-run tests.
> 4. **Derive touched packages and run the full test suite for each.**
>    - Run `python3 {worktree}/scripts/ralph_touched_packages.py {worktree}` and write the output to `{worktree}/tmp/ralph/touched-packages.txt`.
>    - Run `python3 {worktree}/scripts/ralph_touched_packages.py --ts {worktree}` to get the list of touched TypeScript packages.
>    - For each Python package listed in `touched-packages.txt`: if no `.venv` exists, run `{worktree}/scripts/install-package-venv.sh <pkg>` first. Then run the full test suite: `packages/<pkg>/.venv/bin/pytest packages/<pkg>/tests/ -v --tb=short`.
>    - For each touched TypeScript package: run `npm run lint`, `npm run typecheck`, and `npm test` in that package directory.
>    - Aggregate test results to `{worktree}/tmp/ralph/pre-push-preview.txt` (one line per package: `PASS packages/<pkg>` or `FAIL packages/<pkg> — <reason>`).
>    - Any failure → do NOT write COMPLETE. Fix the failures, then re-run.
>    - **Also run ALL pre-PR checks for every package you touched:**
>      - Python: `.venv/bin/ruff check src/ tests/`, `.venv/bin/ruff format --check src/ tests/`
>      - TypeScript: `npm run lint`, `npm run typecheck`, `npm test`
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
> 11. **If during steps 1-8 you determine one or more acceptance criteria are structurally infeasible** per the §"AC_INFEASIBLE emit rules" above (non-existent symbol, self-contradiction, out-of-scope dependency), do NOT keep iterating and do NOT write COMPLETE. Instead, write `AC_INFEASIBLE` to `{worktree}/tmp/ralph/work-status.txt` AND write a JSON array of `{"index": <1-based>, "evidence": "<paragraph citing the grep output / file path / conflicting AC index>"}` entries to `{worktree}/tmp/ralph/infeasible-acs.json`. Apply the negative guardrails — do NOT raise this verdict for "hard to implement", max iterations, ambiguous wording, or missing fixtures you can create yourself.
>
> Rules:
> - All work happens in `{worktree}`. All temp files go in `{worktree}/tmp/`.
> - No `$()` command substitution. No heredocs. No `python3 -c`. No quoted strings with `&&` or `;`.
> - Do not commit, push, or create PRs. Only implement and verify locally.
> - Follow existing code patterns. Type hints on all Python function signatures. Strict TypeScript mode.
> - **Do not use `run_in_background` on any command.** Run all commands (test suites, lint, format checks) in the foreground and wait for their results before proceeding. You are a subagent — backgrounding causes notifications to surface in the wrong context.

#### Non-testable worker prompt

Spawn a **worker subagent** (using the Agent tool) with this prompt structure:

> You are implementing a **non-testable** task in a ralph loop (iteration N of max 5). This task is a docs, db_migration, dx_tooling, or no_deployed_component change — there is no test suite to iterate against, and TDD does not apply.
>
> Read these files for your task and any prior feedback:
> - `{worktree}/tmp/ralph/task.md`
> - `{worktree}/tmp/ralph/feedback.md`
>
> **If `task.md` contains a `## Prior attempts` section**, read it carefully. It lists failures from previous ralph runs for this same issue — including the failure category, push-time stderr tail, and ralph iteration narrative. Address the listed failures directly rather than re-discovering them. The absence of a `## Prior attempts` section means this is the first attempt.
>
> Then implement the task directly:
> 1. Read the task and feedback files. Focus on the "## Plan (from /task-v2-plan)" section's "What will change" subsection — this is the concrete list of edits to make.
> 2. Examine the existing structure of the files you will edit. For docs, follow the existing headings and tone. For scripts, follow the existing `# venv:` / `# one-off:` / `# permanent:` header conventions (see `docs/agent/code-standards.md` §Python scripts). For migrations, follow the existing migration file naming and schema patterns.
> 3. Apply the edits. If this is iteration 2+, focus on addressing the reviewer's feedback from `feedback.md` — read the existing implementation, apply the requested changes.
> 4. **Derive touched packages and run pre-PR checks for each.**
>    - Run `python3 {worktree}/scripts/ralph_touched_packages.py {worktree}` and write the output to `{worktree}/tmp/ralph/touched-packages.txt`.
>    - Run the pre-PR checks that apply to the files you actually touched:
>      - Any `.py` file touched → for each package in `touched-packages.txt`: `.venv/bin/ruff check <file>`, `.venv/bin/ruff format --check <file>` in the owning package. (Pytest and diff-coverage are **skipped** — this is a non-testable change.)
>      - Any `.md` file touched → `scripts/check-markdown-links.sh` (reads every `.md` in the push, not just the diff).
>      - Any `.tf` file touched → `terraform fmt -check -recursive` in the touched module.
>      - Any `.yml` / `.yaml` under `.github/workflows/` touched → `scripts/check-ci-job-skipped.sh` (the #2410/#2505 footgun guard).
>      - Any `.sql` migration touched → verify the filename numbering and schema match existing migrations; no runtime check required.
>      - No matching file type → no pre-PR checks needed. Skip to step 5.
>    - Aggregate check results to `{worktree}/tmp/ralph/pre-push-preview.txt` (one line per package or check: `PASS <check>` or `FAIL <check> — <reason>`).
>    - Any failure → do NOT write COMPLETE. Fix the failures, then re-run.
> 5. **Self-verify acceptance criteria before completing.** Read `{worktree}/tmp/ralph/task.md` and find every acceptance criterion (the `- [ ]` checkboxes). For EACH criterion, describe how your implementation satisfies it — reference the specific file, section, or edit. If any criterion cannot be verified locally (e.g., requires post-deploy or post-merge observation), note it as "requires post-deploy verification." If any criterion is NOT addressed by your implementation, do NOT write COMPLETE — instead, note the gap and continue implementing.
> 6. Write your acceptance criteria self-check to `{worktree}/tmp/ralph/acceptance-check.txt` with a table mapping each criterion to its evidence.
> 7. When all applicable pre-PR checks pass AND all locally-verifiable acceptance criteria are addressed, write "COMPLETE" to `{worktree}/tmp/ralph/work-status.txt`.
> 8. If you cannot get the plan implemented after reasonable effort (e.g. the plan's "What will change" section is unclear, or a required file doesn't exist), write "STUCK" to `work-status.txt` and describe what's failing in `{worktree}/tmp/ralph/stuck-reason.txt`.
> 9. **If during steps 1-6 you determine one or more acceptance criteria are structurally infeasible** per the §"AC_INFEASIBLE emit rules" above (non-existent symbol, self-contradiction, out-of-scope dependency), do NOT keep iterating and do NOT write COMPLETE. Instead, write `AC_INFEASIBLE` to `{worktree}/tmp/ralph/work-status.txt` AND write a JSON array of `{"index": <1-based>, "evidence": "<paragraph citing the grep output / file path / conflicting AC index>"}` entries to `{worktree}/tmp/ralph/infeasible-acs.json`. Apply the negative guardrails — do NOT raise this verdict for "hard to implement" or ambiguous wording.
>
> Rules:
> - All work happens in `{worktree}`. All temp files go in `{worktree}/tmp/`.
> - No `$()` command substitution. No heredocs. No `python3 -c`. No quoted strings with `&&` or `;`.
> - Do not commit, push, or create PRs. Only implement and verify locally.
> - **Do not write tests for non-testable changes.** If the plan's "What will change" section genuinely requires a test (e.g., you're tightening a docstring that the plan author misclassified as docs when it actually affects runtime behavior), write "STUCK" with an explanation — the plan author should re-classify rather than ralph silently mixing testable and non-testable work.
> - **Do not use `run_in_background` on any command.** Run all commands in the foreground and wait for their results before proceeding. You are a subagent — backgrounding causes notifications to surface in the wrong context.

After the worker subagent completes, read `{worktree}/tmp/ralph/work-status.txt`.

- If **STUCK**: Stop the loop. Comment on the issue describing the blocker. Block the issue with `scripts/block-issue.sh <issue> <blocker>` (if a specific blocking issue exists) or add `status/blocked` manually. Return to the caller with failure status.
- If **AC_INFEASIBLE**: Stop the loop. Do NOT comment on the issue (the diagnoser will own the follow-up action — reissue, escalate, or close). Do NOT block the issue. Write `AC_INFEASIBLE` to `{worktree}/tmp/ralph/ralph-done.txt` along with the current iteration count, leaving `{worktree}/tmp/ralph/infeasible-acs.json` in place for the outer `/task-v2-ralph` wrapper to read. Return to the caller — the daemon routes to the diagnoser from here.
- If **COMPLETE**: Continue to the review phase (2b for testable, 2b' for non-testable).

### 2b — Sequential review phase (testable branch)

Run this sub-step only when `## Testable: yes` (or the line is missing). For `## Testable: no`, skip to 2b' below.

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
   (Run Claude reviewer agent — see §"Claude reviewer prompt" below)
   ```
   date +%s
   ```
   Compute: `claude_secs = end - start`

**After all three reviewers complete**, end the reviewer phase with per-reviewer timing detail:
```
python3 {worktree}/scripts/phase_timer.py end {worktree} --detail '{"gemini_standard": <gemini_standard_secs>, "gemini_adversarial": <gemini_adversarial_secs>, "claude": <claude_secs>}'
```

### 2b' — Single-reviewer phase (non-testable branch)

Run this sub-step only when `## Testable: no`.

Write status: `phase: ralph-reviewer (iteration N)`, `summary: Running Claude reviewer for iteration N (non-testable)`.
Also start the phase timer: `python3 {worktree}/scripts/phase_timer.py start {worktree} "ralph-reviewer (N)"`

**Pre-generate diff and changed files before launching the reviewer.** Same procedure as 2b — write `{worktree}/tmp/ralph/diff.txt` and `{worktree}/tmp/ralph/changed_files.txt`.

**Skip the two Gemini reviews** — they are code-review-oriented and add no signal on docs / db_migration / dx_tooling / no_deployed_component diffs. The non-testable branch accepts a single Claude review.

Write `SKIPPED` to both `{worktree}/tmp/ralph/gemini-review-result.txt` and `{worktree}/tmp/ralph/adversarial-result.txt` so the decision logic in §2e can read them uniformly. Write `Skipped: non-testable change type (see task.md ## Testable: no).` to each of `gemini-feedback.md` and `adversarial-feedback.md` for audit.

**Run the Claude reviewer** — capture start time, spawn via the Agent tool (foreground), capture end time:
```
date +%s
```
(Run Claude reviewer agent — see §"Claude reviewer prompt" below)
```
date +%s
```
Compute: `claude_secs = end - start`

End the reviewer phase with per-reviewer timing detail (Gemini entries marked zero since they were skipped):
```
python3 {worktree}/scripts/phase_timer.py end {worktree} --detail '{"gemini_standard": 0, "gemini_adversarial": 0, "claude": <claude_secs>}'
```

### Claude reviewer prompt

The Claude reviewer prompt applies to both branches:

> You are reviewing a code change in a ralph loop (iteration N of max 5). Your job is to evaluate whether the implementation is ready to ship or needs revision. You are a fresh pair of eyes — you did not write this code.
>
> **First, read `{worktree}/tmp/ralph/task.md` and check for `## Testable`.** The line will be `yes`, `no`, or absent. If `no`, this is a **non-testable** change (docs, db_migration, dx_tooling, no_deployed_component); apply the non-testable rules below. Otherwise, apply the testable rules.
>
> **Also check `task.md` for a `## Prior attempts` section.** If present, it lists failures from previous ralph runs for this same issue. When reviewing, verify that the current implementation addresses those prior failure causes — flagging the same issue as REVISE that a prior attempt already fixed is a loop deadlock. The absence of a `## Prior attempts` section means this is the first attempt.
>
> **Both Gemini reviews either ran (testable) or were skipped (non-testable) before you.** Read their feedback files before starting your own review:
> - `{worktree}/tmp/ralph/gemini-feedback.md` (Gemini standard review — may say "Skipped: non-testable change type")
> - `{worktree}/tmp/ralph/adversarial-feedback.md` (Gemini adversarial review — may say "Skipped: non-testable change type")
>
> 1. Read the task requirements: `{worktree}/tmp/ralph/task.md`
> 2. Read the Gemini feedback files (both should exist — either real feedback or a SKIPPED placeholder).
> 3. Review the complete diff:
>    ```
>    git -C {worktree} diff
>    git -C {worktree} diff --cached
>    git -C {worktree} status
>    ```
>    Also read the actual changed files to understand context beyond the diff.
> 4. **Verify acceptance criteria (MANDATORY).** Extract every acceptance criterion from `task.md` (the `- [ ]` checkboxes). For EACH criterion:
>    - Check whether the implementation satisfies it — look at the code, tests (if any), and the worker's self-check in `{worktree}/tmp/ralph/acceptance-check.txt`.
>    - Mark it as **met**, **not met**, or **requires post-deploy verification**.
>    - If ANY locally-verifiable criterion is **not met**, the review MUST be **REVISE**, regardless of code quality. List the unmet criteria in your feedback.
> 5. Evaluate against these additional criteria:
>    - **Correctness**: Does the implementation satisfy the acceptance criteria in task.md?
>    - **Full-package test coverage (MANDATORY for testable changes):** Read `{worktree}/tmp/ralph/pre-push-preview.txt`. Verify that every package listed in `{worktree}/tmp/ralph/touched-packages.txt` has a `PASS packages/<pkg>` entry in `pre-push-preview.txt`. If `pre-push-preview.txt` is missing, or if any touched package has a `FAIL` entry or is absent from the preview, issue **REVISE** and explain which package's full test suite was not run or did not pass. "I only ran tests for the new file" is not sufficient — the worker must run the full `packages/<pkg>/tests/` suite for every touched package and document the result. Skip this check entirely for non-testable changes (`## Testable: no`).
>    - **Test coverage** (testable only — skip for non-testable): Are there tests for each acceptance criterion and obvious edge cases? For **non-testable** changes (`## Testable: no`), do **not** flag "no tests added" as a REVISE reason. Tests are not expected on a docs-only or migration-only diff. Verify acceptance criteria against the diff, the plan's "What will change" section, and any updated documentation instead.
>    - **Scope completeness**: Does the change need to be applied in other locations too? Search the codebase (using Grep) for other files that use, render, or implement the same pattern being changed. If the fix or feature was applied to one file but the same pattern exists elsewhere without the change, flag it as a REVISE reason. For example, if a rendering fix was applied to `ComponentA.tsx`, check whether `ComponentB.tsx` or other components render the same data and need the same fix.
>    - **Scope creep**: Are there changes unrelated to the issue (extra refactors, unrelated fixes)?
>    - **Code quality**: Does it follow existing patterns? Any debug code, hardcoded values, or forgotten TODOs?
>    - **Missing pieces**: Are there files that should have been created or modified but wouldn't?
>    - **Stale references**: Do comments, imports, or docstrings reference things that changed?
>    - **Documentation consistency**: If the change modifies behavior, configuration, or interfaces — do related docs (`docs/`, `CLAUDE.md`, `.claude/skills/`, `README.md`, `CONTRIBUTING.md`) need corresponding updates? Flag any docs that reference the old behavior.
>    - **Performance** (testable only — skip for non-testable): Are there obvious bottlenecks? Sequential I/O that could be parallelized? O(n^2) patterns (e.g. LIMIT/OFFSET pagination, nested loops over large datasets)? Missing connection pooling or batching for network calls (DB, S3, HTTP)?
>    - **Unchecked test plan items**: If the PR includes a test plan with checkboxes, any unchecked items are **merge blockers**. An unchecked item means something was not verified — flag it as a REVISE reason. The author must either check the item (verify it) or remove it (not applicable).
>    - **Diff-coverage gate** (testable only — skip for non-testable): confirm the worker's self-check reports diff-coverage ≥ 90%.
>    - **Web Interface Guidelines (conditional)**: If any changed files have `.tsx` or `.css` extensions, read `~/.claude/commands/web-interface-guidelines.md` and check those files against its rules. Report violations as REVISE reasons with `file:line` format. Skip this criterion entirely if no `.tsx` or `.css` files appear in the diff.
> 6. Make a decision — **one of three verdicts**:
>    - **SHIP**: The implementation is correct, well-tested (or, for non-testable changes, implements the plan's "What will change" section faithfully), properly scoped, ALL locally-verifiable acceptance criteria are met, and ready for PR. Write "SHIP" to `{worktree}/tmp/ralph/review-result.txt`.
>    - **REVISE**: Something needs to change. Write "REVISE" to `{worktree}/tmp/ralph/review-result.txt`. Then write specific, actionable feedback to `{worktree}/tmp/ralph/feedback.md` — describe exactly what needs to change and why. Be concrete: reference specific files, functions, and line numbers. **If any acceptance criterion is unmet, list it first in your feedback.**
>    - **AC_INFEASIBLE**: One or more acceptance criteria are structurally impossible per the §"AC_INFEASIBLE emit rules" section (non-existent symbol, self-contradiction, out-of-scope dependency) — NOT "hard to implement", not "max iterations", not "ambiguous". Write "AC_INFEASIBLE" to `{worktree}/tmp/ralph/review-result.txt`. Write the JSON array of `{"index": <1-based>, "evidence": "<paragraph>"}` to `{worktree}/tmp/ralph/infeasible-acs.json` (merge with any entries the worker already wrote; dedupe by `index`). Err on the side of REVISE when uncertain — AC_INFEASIBLE requires citable evidence (grep output, file path, conflicting AC index), not a hunch. When you pick AC_INFEASIBLE, do NOT write feedback.md — the diagnoser owns the follow-up action.
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
> - **Missing or failed full-package test run is always REVISE (testable only).** If `pre-push-preview.txt` is absent or any touched package's full test suite did not pass, issue REVISE. The worker must document passing results for every touched package.
> - **Do not REVISE for "no tests added" on non-testable changes.** The `## Testable: no` branch does not require tests; applying a test-coverage standard to a docs-only or migration-only diff is a category error that would deadlock the loop.
> - If you say REVISE, your feedback must be specific enough that the worker can act on it without guessing.
> - **Unchecked test plan items are always blockers.** Never approve a PR with unchecked test plan checkboxes.
> - **Do not use `run_in_background` on any command.** You are a subagent — all commands must run in the foreground.

### 2c — Collect results

After the reviewer(s) complete, read:
- `{worktree}/tmp/ralph/gemini-review-result.txt` (Gemini standard — `SKIPPED` on non-testable branch)
- `{worktree}/tmp/ralph/adversarial-result.txt` (Gemini adversarial — `SKIPPED` on non-testable branch)
- `{worktree}/tmp/ralph/review-result.txt` (Claude — always runs)

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

**Required-reviewer agreement to SHIP** depends on the branch:

- **Testable branch:** all three reviewers must agree to SHIP (with persistent-dissent override). Claude **SHIP** AND (Gemini standard **SHIP** or **SKIPPED**) AND (Gemini adversarial **SHIP** or **SKIPPED**) → SHIP.
- **Non-testable branch:** Claude alone must SHIP. Both Gemini entries are always **SKIPPED** on this branch, so the same predicate — Claude SHIP AND Gemini standard SHIP-or-SKIPPED AND Gemini adversarial SHIP-or-SKIPPED — reduces to Claude SHIP. The predicate is uniform across branches.

If the predicate is satisfied: the loop is done. Continue to Step 3.

**AC_INFEASIBLE short-circuit.** If the worker wrote `AC_INFEASIBLE` to `work-status.txt` OR the Claude reviewer wrote `AC_INFEASIBLE` to `review-result.txt`, the loop terminates immediately with verdict `AC_INFEASIBLE`. Do NOT run subsequent reviewers. Do NOT run the next iteration. Do NOT invoke the persistent-dissent override (it does not apply to AC_INFEASIBLE). Write `AC_INFEASIBLE` to `{worktree}/tmp/ralph/ralph-done.txt` along with the iteration count, leave `infeasible-acs.json` in place, and return to the caller — the outer `/task-v2-ralph` wrapper will read both files and emit the dispatcher-facing JSON.

- **Persistent-dissent override** (testable branch only): If ANY reviewer says **REVISE**, first check whether this is a persistent solo-dissent pattern. The override logic only applies when multiple reviewers actually ran — on the non-testable branch where only Claude runs, there is no dissent to override. Write and run a small Python script (`{worktree}/tmp/ralph/check_dissent.py`):

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

- If the required reviewer(s) say **REVISE** (and no persistent-dissent override applies): Increment iteration. If `iteration > max_iterations`, stop the loop and comment on the issue that the ralph loop hit its max iterations — block the issue with `scripts/block-issue.sh <issue> <blocker>` (if applicable) or add `status/blocked` manually, and return with failure. Otherwise:
  - Consolidate feedback from ALL reviewers that said REVISE into `{worktree}/tmp/ralph/feedback.md`. Include feedback from `gemini-feedback.md`, `adversarial-feedback.md`, and/or `feedback.md` as appropriate. On the non-testable branch, only Claude's `feedback.md` is relevant (the Gemini files are SKIPPED placeholders).
  - Bump `iteration.txt` to the next value and create new todos for the next iteration (using the branch-appropriate todo list from Step 1), then return to 2a.

### 2f — Per-iteration patch persistence (daemon-owned, #3042)

Per-iteration patches are saved to `dispatcher.ralph_patches` **by the dispatcher daemon**, not by this skill. The daemon polls `git -C <worktree> rev-parse HEAD` every 30s during the ralph phase and writes an intermediate row on each observed SHA change (inferring `iteration_n` from `{worktree}/tmp/ralph/iteration.txt`). Ralph's `git commit --amend --no-edit` idiom (#2971) rewrites HEAD per iteration, so the SHA signal is a reliable iteration boundary.

No action required from this skill. The resume path — `DispatcherDaemon._apply_prior_ralph_patch` at the top of the next ralph phase — picks up the most-recent row within the 7-day TTL automatically. On SHIP, `DispatcherDaemon._capture_and_persist_ralph_patch`'s DELETE-by-issue_number supersede wipes the intermediate rows so the authoritative SHIP row stays the single post-ralph state.

Historical note: #3026 shipped this as a skill-invoked CLI helper (`scripts/dispatcher/persist_ralph_iteration.py`). The helper was never reliably called from the LLM-driven ralph loop (issue #3042 evidence: 13 ralph SHIPs post-#3028, 3 multi-iteration, zero rows with `iteration_n IS NOT NULL`), so #3042 moved the capture into the daemon and removed the helper.

---

## Step 3 — Log summary and return to caller

The reviewers have approved the implementation. Before returning, log a summary record.

### 3a — Log review summary

Write the following to `{worktree}/tmp/ralph/log_summary.py` (substituting actual paths for `{worktree}` and `{repo_root}`):

```python
import sys
import re
from pathlib import Path

sys.path.insert(0, "<repo_root>/scripts")
from ralph_review_log import log_summary

state_dir = Path("<worktree>/tmp/ralph")
worktree = Path("<worktree>")
iteration = int(state_dir.joinpath("iteration.txt").read_text(encoding="utf-8").strip())

# Derive agent_id from the worktree basename (e.g. "agent-ab4722a2").
_wt_name = worktree.name
agent_id = _wt_name if re.match(r"^agent-[0-9a-f]+$", _wt_name) else None

# Derive issue_number from the first line of task.md (format: "# Issue #<N> — <title>").
issue_number = None
_task_md = state_dir / "task.md"
if _task_md.exists():
    try:
        _first = _task_md.read_text(encoding="utf-8").splitlines()[0]
        _m = re.match(r"^#\s+Issue\s+#(\d+)", _first)
        if _m:
            issue_number = int(_m.group(1))
    except (OSError, IndexError, ValueError):
        pass

log_summary(
    state_dir,
    total_iterations=iteration,
    final_verdict="SHIP",
    agent_id=agent_id,
    issue_number=issue_number,
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

**AC_INFEASIBLE completion signal (issue #3010).** When the §2e short-circuit fires, write this content to `ralph-done.txt` instead (again, substituting the actual iteration count):

```
status: AC_INFEASIBLE
iterations: <N>
next-steps: The daemon will read infeasible-acs.json, write a dispatcher.failures(category='ralph_ac_infeasible') row, and route to the diagnoser. No commit, no push, no PR. The outer /task-v2-ralph wrapper propagates the infeasible_acs array to the dispatcher.
```

Leave `{worktree}/tmp/ralph/infeasible-acs.json` in place so the outer wrapper can pass it through. Do NOT commit, push, or open a PR. Return to the caller.

The code is ready for commit. Return control to the calling workflow (`/task` Path A or `/task-v2-ralph`), which handles process summary, staging, committing, pushing, PR creation, CI monitoring, and cleanup.

**Do not commit, push, or open a PR from this skill.**

**DO NOT EXIT OR STOP after writing this file.** The `/task` workflow has 8 more mandatory steps (A.2b through A.9) that MUST execute after ralph completes. Ralph completing is the HALFWAY POINT of the task, not the end. The code is implemented but has not been committed, pushed, or merged. Exiting here is a known failure mode — see issue #721. You MUST return control to the calling `/task` workflow so it can continue with process summary, commit, push, PR creation, CI monitoring, merge, deployment verification, and retrospective.

Under dispatcher v2, the calling `/task-v2-ralph` skill parses `ralph-done.txt` and emits the final `ralph.json`, then exits — the daemon takes over for the remaining phases.

---

## Guardrails

- **Max 5 iterations.** If the loop doesn't converge, escalate to human via issue comment and `scripts/block-issue.sh` (or `status/blocked` label).
- **Worker and reviewers are separate.** Never combine them — the cross-perspective review is the point.
- **File-based state only.** No information passes between iterations except through the ralph state files.
- **All standard rules apply.** No `$()`, no heredocs, no inline Python, temp files in `{worktree}/tmp/`.
- **Gemini reviews are best-effort** (testable branch only). If the Google API key is unavailable or an API call fails, the loop continues with the remaining reviewers. Gemini reviews never block the loop.
- **Gemini reviews are skipped entirely on the non-testable branch** (`## Testable: no`). This is by design — they produce no signal on docs-only or migration-only diffs. The gemini result files are written as `SKIPPED` placeholders so the §2e decision logic reads uniformly.
- **Ralph is not task completion.** Ralph handles implementation and review only. The calling `/task` workflow (or `/task-v2-ralph`) handles process summary, commit, push, PR, CI, merge, deploy, and cleanup. Never exit after ralph without completing the full workflow.
- **Unchecked test plan items are merge blockers.** Reviewers must flag unchecked test plan checkboxes as REVISE reasons. A PR with unchecked items is not ready to ship.
- **Unmet acceptance criteria are always REVISE.** Reviewers must verify every acceptance criterion individually. Code quality alone is not sufficient for SHIP — all locally-verifiable acceptance criteria must be met.
- **"No tests added" is not a REVISE reason on the non-testable branch.** Applying test-coverage standards to a docs-only or migration-only diff is a category error that deadlocks the loop. The reviewer must distinguish the branches by reading `## Testable` in `task.md`.
- **AC_INFEASIBLE requires citable evidence (issue #3010).** The worker or Claude reviewer may surface `AC_INFEASIBLE` when an AC references a non-existent symbol, self-contradicts another AC, or depends on out-of-scope work — see §"AC_INFEASIBLE emit rules". "Hard to implement", "max iterations exhausted", and ambiguous wording are NOT triggers. When uncertain, prefer REVISE — the diagnoser cannot reason about a hunch.
- **Persistent-dissent override** (testable branch only). If one reviewer says REVISE for 2+ consecutive iterations while the other two say SHIP, and the detection function (`detect_persistent_dissent`) confirms the pattern, the loop treats it as SHIP. This prevents a single reviewer from blocking convergence on theoretical grounds that the other reviewers have already evaluated and dismissed. The override is logged to `review-log.jsonl` with type `dissent_override` for audit. This override only applies when exactly one reviewer dissents — if two reviewers REVISE, that is a genuine concern and the override does not trigger. On the non-testable branch, only Claude reviews, so the override is inapplicable and the REVISE path always re-runs the worker.
- **Do not use `run_in_background` anywhere in the ralph loop.** All commands — test suites, lint, format checks, git commands, reviewer invocations, and worker subagents — must run in the foreground. Subagents are already running as background tasks from the parent's perspective. Further backgrounding causes completion notifications to route to the wrong context, leading to confusion and lost results.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
- **No `run_in_background`.** All work runs synchronously — see Guardrails above.
