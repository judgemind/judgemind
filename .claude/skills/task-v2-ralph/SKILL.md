---
description: Ralph phase for the per-phase /task-v2 pipeline. Iterative worker + reviewer loop, returns SHIP verdict plus a committed implementation diff on the worktree branch. Long-tail phase (~45-90 min internally).
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

**Goal:** Produce a committed implementation diff on the worktree branch (ralph commits directly — see Step 2.5), plus `{worktree}/tmp/dispatcher-output/ralph.json` with a SHIP/BLOCKED verdict.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, any Task tool call, or any other operation. `/task-v2-ralph` is already a dispatcher-spawned background subprocess — further backgrounding causes completion notifications to surface in the wrong context and loses results.

**IMPORTANT — Subagent isolation.** The context-budget analysis in spike 0.3 (`docs/investigations/dispatcher-v2-spike-0.3.md`) shows `/task-v2-ralph` stays inside the 200k-token window ONLY if each worker + each reviewer runs as a fresh-context subagent (Task tool). Inline workers break this guarantee. Do not inline.

**Implementation choice (per issue #2732):** This skill invokes the existing `/ralph` skill as its inner loop. `/ralph` already implements the worker + three-reviewer cycle with fresh-context Task-tool subagents and per-iteration state files under `{worktree}/tmp/ralph/`. `/task-v2-ralph` is the thin outer wrapper that (a) seeds `task.md` from `plan.json`, (b) invokes `/ralph`, (c) runs the local pre-push gate (Step 2.5), (d) parses `{worktree}/tmp/ralph/ralph-done.txt` into the output JSON. Keeps the implementation in one place and ensures parity with the current `/task` workflow's ralph behavior.

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
  "verdict": "SHIP" | "AC_INFEASIBLE" | "BLOCKED",
  "iterations_used": <int, 1..max_iterations>,
  "block_reason": null | "<string>",
  "changed_files": ["<path>", ...],
  "infeasible_acs": [ {"index": <int>, "evidence": "<paragraph>"} ],
  "summary": "<1-3 sentence implementation summary>",
  "ralph_done_path": "<worktree>/tmp/ralph/ralph-done.txt" | null,
  "review_log_path": "<worktree>/tmp/ralph/review-log.jsonl" | null
}
```

Always exit 0. BLOCKED and AC_INFEASIBLE are not subprocess errors — the daemon reads `verdict` from JSON.

`iterations_used` is `1..max_iterations` when the ralph loop runs (which is every non-BLOCKED path). The pre-#2845 short-circuit that emitted `iterations_used=0` for non-testable types no longer exists.

`ralph_done_path` and `review_log_path` are non-null whenever the ralph loop ran (every SHIP / AC_INFEASIBLE path). They are `null` only when Step 0's Task-tool availability check fails and the skill exits BLOCKED without spawning a worker.

`infeasible_acs` MUST be present and non-empty when `verdict == "AC_INFEASIBLE"`; absent or `[]` on SHIP/BLOCKED. Each entry is `{"index": <1-based into the issue body's acceptance-criteria list>, "evidence": "<one paragraph naming the missing symbol / contradicting AC / out-of-scope dependency>"}`. An array (not a single object) lets ralph flag multiple ACs in one pass — a single root cause (e.g. two ACs both referencing the same non-existent CLI flag) should not force sequential re-plan cycles. See the inner `/ralph` skill's §"AC_INFEASIBLE emit rules" for the positive triggers and negative guardrails the worker + reviewer apply.

On SHIP: ralph has committed the implementation diff to the worktree branch with the placeholder message `"WIP: ralph output"` (see Step 2.5), AND the local `.githooks/pre-push` hook passed against that commit. The daemon's `summary` phase reads the diff via `git diff origin/main...HEAD`; the daemon's `push_and_pr` phase amends ralph's commit with summary's conventional-commits message (`git commit --amend -F <file>`) and pushes. Issue #2971.

On AC_INFEASIBLE: ralph did not ship a diff. The daemon detects the verdict in post-exit parse, writes a `dispatcher.failures(category='ralph_ac_infeasible', details={infeasible_acs, agent_id, issue_number})` row, and routes the agent to the diagnoser (Tier 3 — spec §8). Summary and push_and_pr are skipped. Whatever partial work landed on the worktree branch is discarded when the daemon drops the worktree on diagnoser handoff.

On BLOCKED: the working tree may contain partial work (committed or uncommitted). The daemon decides whether to retry (fresh worktree) or diagnose (`§8` Tier 2/3 flow).

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

**If `Task` is available**, continue to Step 0.5.

---

## Step 0.5 — Load prior-attempt context (if present)

Before seeding the ralph state directory, check whether the daemon has left a prior-attempts file at `{worktree}/tmp/dispatcher-output/prior_attempts.md`.

- **If the file exists**: read it. Its content will be included in `task.md` under a `## Prior attempts` section (see Step 1) so the inner `/ralph` worker has explicit failure context from previous runs.
- **If the file does not exist** (first-attempt case): skip this step. Do NOT create the file, do NOT include a `## Prior attempts` section in `task.md`. The worker receives no prior-attempt context — this is the correct zero-cost path for new issues.

Store the file content (or empty string) for use in Step 1.

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

  ## Prior attempts (optional — omit this section if no prior_attempts.md exists)

  <verbatim content of {worktree}/tmp/dispatcher-output/prior_attempts.md if present>
  ```

  Include the `## Prior attempts` section only when `{worktree}/tmp/dispatcher-output/prior_attempts.md` exists (i.e. when Step 0.5 found the file). When the file is absent, omit the section entirely — the worker should not see an empty or placeholder section.

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

## Step 2.5 — Commit ralph's work and run local pre-push gate (MANDATORY before SHIP)

**Ralph is not done until (a) ralph's work is committed to the worktree branch, AND (b) `.githooks/pre-push` passes against that commit.**

The commit is the load-bearing change from issue #2971. Ralph commits directly with a placeholder message (`"WIP: ralph output"`); the daemon's `push_and_pr` phase later amends that commit with summary's conventional-commits message via `git commit --amend -F <file>`. Committing in place eliminates the pre-#2971 "stage + throwaway commit + hook + reset --soft + reset HEAD" juggling, which had a latent failure mode: an incomplete undo silently swallowed ralph's diff and produced `git_commit_failed exit_code=1 stderr_tail=""` on the daemon side (observed 2026-04-21 02:59 UTC on agent `cc6c5a07`, issue #2565).

**Skip condition.** If Step 2 read `ralph-done.txt` as `BLOCKED` or `REVISE` (i.e. the inner `/ralph` did not reach SHIP), skip this step entirely and continue to Step 3 with the existing verdict. The commit + pre-push gate only runs on the SHIP path.

### 2.5a — Commit ralph's work with the placeholder message

1. Capture the current branch name: `git -C {worktree} rev-parse --abbrev-ref HEAD`. Store as `<branch>`.
2. Capture the current `HEAD` SHA (the pre-push "remote_sha" baseline — the commit the daemon will push against): `git -C {worktree} rev-parse HEAD`. Store as `<base_sha>`.
3. Stage and commit everything in the working tree with the placeholder message. Write the placeholder message to a file first (no heredocs / quoted `;` — see CLAUDE.md Critical Rules):
   - Write `WIP: ralph output` to `{worktree}/tmp/ralph/prepush_commit_msg.txt`.
   - `git -C {worktree} add -A`
   - `git -C {worktree} commit -F {worktree}/tmp/ralph/prepush_commit_msg.txt`
4. Capture the new commit SHA as `<local_sha>`: `git -C {worktree} rev-parse HEAD`.

There is **no undo step**. The commit stays on the branch. The daemon's `push_and_pr` phase amends it in place (`git commit --amend -F <summary-message-file>`) — see `scripts/dispatcher/daemon.py::_push_and_open_pr` and issue #2971.

### 2.5b — Run the pre-push hook against the committed state

The pre-push hook reads its ref range from stdin, with one line per ref in the form `<local_ref> <local_sha> <remote_ref> <remote_sha>`. Run it against the committed SHAs captured in 2.5a:

- Write `refs/heads/<branch> <local_sha> refs/heads/<branch> <base_sha>` to `{worktree}/tmp/ralph/prepush_stdin.txt`.
- `bash {worktree}/.githooks/pre-push origin https://github.com/judgemind/judgemind.git < {worktree}/tmp/ralph/prepush_stdin.txt &> {worktree}/tmp/ralph/prepush-failure.txt`
- Capture the exit code.

### 2.5c — Handle the hook result

- **Exit 0 (hook passed):** the local pre-push gate is green. Ralph's commit stays in place; continue to Step 3 with the existing SHIP verdict.

- **Exit non-zero (hook failed):** treat the captured output as new reviewer feedback and iterate:
  1. Read `{worktree}/tmp/ralph/prepush-failure.txt`. Append it to `{worktree}/tmp/ralph/feedback.md` under a new heading `## Pre-push hook failure (local gate — Step 2.5)`, preserving the full output so the next worker iteration has exact error messages (ruff lines, pytest tracebacks, markdown-link failures, schema-drift diffs).
  2. Bump `iteration.txt` by one.
  3. **If the new iteration count exceeds `max_iterations`**, stop iterating. Emit `verdict=BLOCKED` with `block_reason="pre-push hook failed on iteration N; max_iterations reached — see {worktree}/tmp/ralph/prepush-failure.txt"`. Continue to Step 3 (Step 3 maps BLOCKED correctly). The ralph commit stays on the branch; the daemon does not advance past `push_and_pr` on BLOCKED so the commit does not reach main.
  4. **Otherwise**, re-invoke `/ralph` via the Task tool (same call as Step 2). The inner loop re-runs the worker with the new feedback, converges again, and writes a new `ralph-done.txt`. After `/ralph` returns, **collapse the new worker's changes into ralph's existing commit via amend**:
     - `git -C {worktree} add -A`
     - `git -C {worktree} commit --amend --no-edit`
  
     The `--no-edit` keeps the placeholder message intact (the daemon will rewrite it later). The `-a` is absorbed by the explicit `add -A` + `--amend`. The net effect is a single commit on the branch reflecting the latest iteration's work, ready for the next pre-push hook run. Then loop back to 2.5b — run the hook again. Repeat until the hook passes or `max_iterations` is exhausted.

This is intentionally identical to how the Claude reviewer's REVISE feedback is iterated on — the pre-push hook is just another reviewer whose verdict must be SHIP before the outer skill emits SHIP. A pre-push failure is not a dispatcher escalation; it is a signal that ralph's inner checks missed something the push-time hook catches (cross-package hygiene, schema drift, markdown links, CI-job-skipped footgun, dispatcher-image deps). The failure modes cited in issue #2962 (`push_and_pr failed (pre-push hook rejected): FAILED: tests for scraper-framework`, `FAILED: npm run lint for api`, schema drift) all converge by re-running the worker with the concrete stderr.

### 2.5d — No-op guardrail

If the inner `/ralph` produced no diff (e.g. trivially-no-op change, or a non-testable path that converged without touching any file), `git -C {worktree} status --porcelain` returns empty at the top of 2.5a. In that case:

- Skip the commit step in 2.5a (there is nothing to commit; `git commit` would fail with "nothing to commit, working tree clean").
- Skip 2.5b (no commit to push-check).
- Log "pre-push gate skipped — working tree clean; no commit created" and continue to Step 3 with the existing SHIP verdict.

A truly no-op SHIP is unusual; the daemon handles this case by emitting a no-op PR or escalating depending on the change type.

## Step 3 — Parse ralph output

Read `{worktree}/tmp/ralph/ralph-done.txt` and `{worktree}/tmp/ralph/review-log.jsonl` (optional, structured, may be empty).

- `ralph-done.txt` first line contains the final verdict token: `SHIP`, `REVISE` (≡ BLOCKED — max iterations), or `BLOCKED` (worker stuck or explicit abort).
- `review-log.jsonl` rows include per-iteration reviewer verdicts for audit.

Map to output `verdict`:

| ralph-done.txt | output.verdict | block_reason |
|---|---|---|
| `SHIP` | `SHIP` | `null` |
| `AC_INFEASIBLE` | `AC_INFEASIBLE` | `null` (populate `infeasible_acs` instead) |
| `REVISE` | `BLOCKED` | `max_iterations reached without SHIP` |
| `BLOCKED` | `BLOCKED` | `"<text from ralph-done.txt body>"` |

When `ralph-done.txt` is `AC_INFEASIBLE`, the inner `/ralph` skill has also written `{worktree}/tmp/ralph/infeasible-acs.json` — a JSON array of `{index, evidence}` objects (see the inner `/ralph` skill's AC_INFEASIBLE emit path). Read that file and pass it through verbatim as the `infeasible_acs` field of the output JSON. On any other verdict the file is absent — emit `"infeasible_acs": []` so downstream consumers can rely on the field's presence.

If Step 2.5 overrode the verdict to BLOCKED (pre-push hook failed and max_iterations was reached on the re-invocation path), use the Step 2.5 `block_reason` instead of the table above.

Count iterations from `{worktree}/tmp/ralph/iteration.txt`. Include the Step 2.5 re-invocations — each pre-push retry counts as an iteration because each one re-ran `/ralph` with new feedback.

Capture `changed_files` from `git -C {worktree} diff --name-only origin/main...HEAD` — compares the committed state (ralph's commit) to origin/main. On the no-op guardrail path (2.5d) the range is empty and `changed_files` is `[]`.

Compose `summary` as 1-3 sentences describing what was implemented. Pull from the worker's final status report or the Claude reviewer's SHIP justification.

## Step 4 — Write output JSON and exit

**Before writing the JSON**, run the iteration-feedback helper to capture any multi-iteration feedback into the result field:

```
python3 {worktree}/scripts/dispatcher/iteration_feedback.py {worktree}/tmp/ralph/feedback.md
```

Capture stdout. If the output is non-empty it is the `## Iteration feedback` section (starting with `---\n## Iteration feedback (from feedback.md)\n\n...`). Append this verbatim to the `summary` field or to a separate `iteration_feedback` key in the output JSON — either way the daemon's `_read_full_phase_log` captures the full terminal text (stdout + structured logs) into `phase_outputs.log_text`, so downstream retry spawns can query that column for the iteration narrative.

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

The Step 2.5 commit + pre-push gate is cheap on the green path — the commit is a single `git add -A && git commit -F` (sub-second), and the hook takes ~5-30s wall clock for a typical single-package diff (ruff + pytest + format). On the red path it re-invokes `/ralph`, which spends another iteration's worth of tokens; the re-invocation is bounded by `max_iterations` (same budget as the inner loop), so the worst case adds one iteration's tokens, not unbounded retry cost.

## What this skill does NOT do

- **Does not install dependencies.** Daemon already did that in the `setup` phase.
- **Does not push or open a PR.** Daemon owns remote git + GitHub. Ralph's local commit stays in the worktree until the daemon's `push_and_pr` phase amends + pushes it.
- **Does not post issue comments.** `/task-v2-summary` owns the pre-PR comment.
- **Does not watch CI or merge.** Daemon's `ci_watch` and `merge` phases handle that; if CI fails, the daemon spawns `/task-v2-fix-ci`.
- **Does not run deploy verification.** `/task-v2-verify` owns that post-deploy.

## Worked example — testable change, 1-iteration SHIP

Input `plan.json` has `change_type=scraper` — a one-file scraper fix. Step 0 confirms the Task tool is available, Step 1 seeds state (with `## Testable: yes`), Step 2 invokes `/ralph`, worker applies the one-line fix + regression test, all three reviewers agree SHIP on iteration 1. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1. Step 2.5a commits the diff with message `"WIP: ralph output"`. Step 2.5b runs `.githooks/pre-push` against the committed state — the hook's per-package ruff, pytest, and diff-coverage checks pass (the inner worker already ran them per-package in iteration 1). Step 3 parses `ralph-done.txt` = `SHIP`, captures `changed_files` via `git diff --name-only origin/main...HEAD`, emits output. The ralph commit stays in place; the daemon's `push_and_pr` phase amends it with summary's conventional-commits message.

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

Input `plan.json` has `change_type=docs`. Step 0 confirms the Task tool is available, Step 1 seeds state (with `## Testable: no`), Step 2 invokes `/ralph`. The worker reads `## Testable: no`, skips TDD, implements the plan's "What will change" section (e.g. edits `docs/agent/unattended-patterns.md`), runs `scripts/check-markdown-links.sh` on any touched markdown files, and writes `COMPLETE`. Only the Claude reviewer runs — verifies acceptance criteria against the diff, confirms no stale references remain, writes `SHIP` to `review-result.txt`. Ralph writes `ralph-done.txt` = `SHIP` at iteration 1. Step 2.5a commits with placeholder message. Step 2.5b runs `.githooks/pre-push` against the committed state — the hook re-runs `check-markdown-links.sh`, which passes. Step 3 emits SHIP.

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

## Worked example — pre-push gate catches a cross-package hygiene failure (Step 2.5 re-iteration)

Input `plan.json` has `change_type=api` — an API change that also touches a docs file. Step 2's inner `/ralph` loop converges on iteration 2 with SHIP (worker ran ruff + pytest + diff-coverage per-package, Claude reviewer approved). Step 2.5a commits with placeholder. Step 2.5b runs `.githooks/pre-push`, which executes `scripts/check-markdown-links.sh` across the whole push — a markdown pointer in the touched docs file is broken. Exit non-zero, stderr captures the broken-link line. Step 2.5c appends the stderr to `feedback.md`, bumps `iteration.txt` to 3, and re-invokes `/ralph`. The new worker reads the feedback, fixes the markdown link, re-runs the worker's internal pre-PR checks, and reviewers SHIP again. Back in 2.5c step 4, the new changes are folded into the existing commit via `git add -A && git commit --amend --no-edit`, and control returns to 2.5b. The hook runs again — this time it passes. Step 3 emits SHIP with `iterations_used=3`. The branch still has exactly one commit (the amended ralph commit); the daemon's `push_and_pr` phase will amend it one more time to replace the placeholder message with summary's conventional-commits text.

This is the failure mode cited in issue #2962 (push-time hygiene rejections like `FAILED: scripts/check-markdown-links.sh`, `FAILED: tests for scraper-framework` across packages the inner loop didn't spot-check, schema drift, CI-job-skipped footgun). Moving the check into Step 2.5 converts an expensive across-subprocess retry (daemon re-spawns the whole ralph phase in a fresh worktree) into a cheap in-session iteration.

## Worked example — BLOCKED on max iterations

Ralph runs 5 iterations, reviewers keep bouncing between SHIP and REVISE (fix-flipping). Ralph writes `ralph-done.txt` = `REVISE` + detail about the last iteration's feedback. Step 2.5 is skipped because the verdict is not SHIP.

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

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules. Step 2.5's stdin-ref line and the commit message both go through temp files, not heredocs.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Reviewers run synchronously via Task-tool subagents — no `run_in_background`.
- Pre-PR checks (ruff, pytest, lint/typecheck/build for testable types; markdown-link check, ruff on any touched `.py` scripts for non-testable types) run INSIDE ralph's worker or final verification step. **Step 2.5 then commits ralph's work and runs the full `.githooks/pre-push` hook as a final in-session gate before SHIP** — it catches the cross-package hygiene checks (markdown-links, schema-drift, ci-job-skipped, dispatcher-image-deps, filename-separator collisions) that the per-package worker checks don't cover. The daemon's `git push` still runs the same hook authoritatively; Step 2.5 just shifts the failure detection earlier so a fix iterates in-session instead of triggering a whole-phase retry. Issues #2962, #2971.
- Step 2.5 commits in place with the placeholder message `"WIP: ralph output"` and **does not undo the commit**. The daemon's `push_and_pr` phase amends it with summary's conventional-commits message (`git commit --amend -F <file>`) — never pattern-match or parse the placeholder elsewhere; it is purely a stopgap label that the daemon rewrites.
- `/ralph` writes status updates to `{repo_root}/tmp/agent-status/<agent-id>.txt` per its convention. `/task-v2-ralph` may read this file for monitoring but should not write to it — the dispatcher daemon owns agent status via `dispatcher.phase_transitions` rows.
