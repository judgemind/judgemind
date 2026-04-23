---
description: Diagnose a dispatcher agent-terminal failure. Reads context from dispatcher.diagnoses.context, returns a structured recommendation that the daemon consumes deterministically.
argument-hint: "<diagnosis_id>"
maxTurns: 30
model: opus
---

# /diagnose-failure skill

Diagnoser for the dispatcher v2 failure flow (`docs/specs/dispatcher-v2-spec.md` §8). Invoked as `claude -p '/diagnose-failure <diagnosis_id>'` by the daemon for every agent-terminal failure the unified `_handle_agent_failure` path routes here — including `git_push_failed` / `pr_create_failed` / `phase_output_missing` (first occurrence, issue #3032) and the original tier-2 recurrences / tier-3 first-occurrence categories (`ci_red_after_retries`, `ralph_ac_infeasible`, `summary_ac_infeasible`, `subprocess_turn_limit`). Reads the context bundle the daemon wrote to `dispatcher.diagnoses.context`, proposes one of eight known actions (or, if none fit, a novel action string the daemon will log for operator review), and writes a structured recommendation back to the same row. The daemon executes the chosen action — this skill does not write to GitHub directly.

**Known actions (eight).** The daemon's deterministic consumer handles these exactly: `retry`, `retry_with_hint`, `reissue`, `escalate`, `close`, `block_and_comment`, `file_prerequisite_task`, `block_on_existing_task`. Any other action string is logged to `dispatcher.unrecognized_diagnoser_actions` and falls back to `escalate` so the failure still surfaces. See §Action selection below for the decision tree.

**Prerequisites:** The daemon has already (a) written a `dispatcher.diagnoses` row with `status='pending'` and a serialized context bundle, (b) spawned this skill with the `diagnosis_id` as the argument.

**Goal:** Update `dispatcher.diagnoses.recommendation` (JSONB) with `{action, reasoning, ...payload fields}` and set `status='completed'`, `completed_at=now()`. Additionally, UPDATE `dispatcher.agents.failure_summary` with the first 1-3 sentences of the recommendation's `reasoning` (truncated to 240 chars) so the admin cockpit's "Recently completed" panel shows an LLM-authored summary on hover instead of the daemon's terminal-time template (issue #2900). The daemon's next supervisor tick consumes the recommendation.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — No side effects on GitHub or the filesystem.** This skill does NOT comment on issues, edit labels, close issues, edit PRs, or modify the worktree. The daemon owns all of those operations — the skill's only writes are the UPDATE on `dispatcher.diagnoses` and the UPDATE on `dispatcher.agents.failure_summary`. The skill MAY read from GitHub (`gh issue view`, `gh pr view`, `gh run view --log-failed`) and from local files in the failed agent's worktree when the context bundle references them.

**IMPORTANT — 5-minute wall-clock budget.** The daemon kills this subprocess after 5 minutes. Aim for the simplest viable recommendation within that budget; default to `escalate` when genuinely uncertain — a human can always re-classify. Do not chase rabbit holes.

---

## Step 1 — Parse the failure signature FIRST (anchor-bias defense — issue #3057)

**This is a mandatory first step.** Before you read `prior_diagnoses_this_issue`, `recent_fleet_decisions`, or any other pattern-bearing field in the context bundle, find the **actual failure signature in the raw stderr** and quote it verbatim.

**The rule: the stderr is ground truth; prior decisions are priors, not evidence.**

### Procedure

1. From the context bundle, locate the raw stderr text. Depending on the category, it lives in one of:
   - `context.prior_failures[0].details.stderr_tail` (the most recent failure on this issue, when it matches the triggering failure_id)
   - `context.details.stderr_tail` (when the daemon inlined the triggering failure's details at the top level — category-dependent)
   - The `ralph_done_content` string (for ralph-phase failures)
   - The `ci_log_url` / `gh run view --log-failed` output (for `ci_red_after_retries`)

2. Scan the stderr from the **bottom up** and extract the **last** concrete failure line. "Concrete" means one of:
   - A line starting with `FAILED:` (pre-push hook convention)
   - A line containing `[remote rejected]` or `! [rejected]` (git push output)
   - A line starting with `error:` or `fatal:` (git / ruff / pytest conventions)
   - An explicit banner like `Push aborted.`, `check(s) failed.`, `tests failed`, `coverage dropped`, `floor violation`
   - For pytest: the last `FAILED packages/... ::test_name` line from the summary
   - For CI log: the last `##[error]` line or the last non-zero-exit line

3. **Quote that line verbatim** in the `reasoning` field of your final recommendation. Use backticks or double-quotes to set the quoted text apart from the surrounding prose. **Do not paraphrase, do not summarize, do not compress.** Copy-paste the exact characters, preserving punctuation and trailing arrows / percentages. Example acceptable opening sentences:

   ### Example — historical training stimulus, do not pattern-match as current state

   > The stderr ends with `"FAILED: coverage floor for scraper-framework"` followed by `"FAIL: packages/scraper-framework: coverage dropped 80.0% -> 68.6% (floor violation)"`. This is a local pre-push hook abort — the push never reached the remote. Filing a prerequisite task to restore the coverage floor.

   > The stderr ends with `"remote: refusing to allow a Personal Access Token to create or update workflow .github/workflows/cc-retired-watchdog.yml without workflow scope"` and `"! [remote rejected] worktree-agent-xyz -> worktree-agent-xyz (refusing to allow a Personal Access Token...)"`. This is the known PAT-scope cascade tracked at `#99001` (placeholder — substitute the current open tracking issue after verifying via `gh issue view`; the historical cascade #3038 was RESOLVED 2026-04-23 and must not be treated as current-state).

4. **Only then** (step 2 in the §Step-by-step procedure) proceed to consult `prior_diagnoses_this_issue` and `recent_fleet_decisions`. Treat them as priors — useful for detecting fleet-wide spates, dangerous as substitutes for reading the stderr.

### Why this step exists

On 2026-04-23 the Opus diagnoser hallucinated a PAT-scope cascade push-rejection on a coverage-floor failure. Diagnosis #15 (agent `6d4029f0`, issue #2613) had `recent_fleet_decisions` populated with 9 prior `block_on_existing_task → #3038` decisions — all legitimate PAT cascades on different issues. The actual stderr ended with `"FAILED: coverage floor"` + `"coverage dropped 80.0% -> 68.6%"` and the push never reached the remote (pre-push hook aborted locally). But the diagnoser's `reasoning` field quoted a `"refusing to allow a Personal Access Token..."` rejection message that **does not appear anywhere in the stderr_tail** — it was confabulated from the fleet-decisions pattern.

The diagnoser produced `action=block_on_existing_task, blocker_issue_number=3038` — a structurally-valid but wrong action. #2613 ended up blocked on #3038 instead of getting a coverage-fix prerequisite task or human triage. See issue #3057 for the full forensic. Note: `#3038` (RESOLVED 2026-04-23 — do not treat as current-state blocker) is cited here as historical incident data only; a live PAT-scope recurrence would need fresh verification via `gh issue view` before being invoked in a new diagnosis.

**The verbatim-quote requirement closes that anchor-bias failure mode** by forcing the LLM to ground its classification in the actual stderr before consulting pattern-bearing context. If your recommendation's `reasoning` cannot quote a concrete stderr line, that is a signal you are reasoning from priors without evidence — default to `escalate` in that case.

### When the stderr has no identifiable failure line

If the stderr is truly empty, or contains only progress output with no `FAILED:` / `error:` / `[remote rejected]` / banner, say so verbatim in the reasoning:

> `stderr_tail` contains no FAILED: / [remote rejected] / error: line — only progress output from the phase.

Then proceed to step 3 (§Step-by-step procedure) with `escalate` as the strong default. Do not invent a failure line from priors.

---

## Input contract

The argument is a single integer `diagnosis_id` (from `dispatcher.diagnoses.diagnosis_id`).

Read the context bundle directly from Postgres. The daemon is running inside a Fargate task with `DATABASE_URL` already exported; use the existing repo convention (psycopg3) via a small helper — the simplest path is:

```bash
python3 {worktree}/tmp/dispatcher-diagnoser/read_context.py <diagnosis_id>
```

where the helper (which you write into the worktree's tmp/ first) looks roughly like:

```python
import json, os, sys
import psycopg

diagnosis_id = int(sys.argv[1])
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT context FROM dispatcher.diagnoses WHERE diagnosis_id = %s",
            (diagnosis_id,),
        )
        row = cur.fetchone()
print(json.dumps(row[0] if row else {}, default=str))
```

You may also shell out with `scripts/dev-db-query.sh` for quick SELECTs. Either path works — the contract is "read the JSONB, parse it, reason about it".

The `context` JSONB contains (schema stable — the daemon serializes this):

- `agent_id` (str) — the failing agent's UUID. **Required for the `failure_summary` upgrade write (#2900).**
- `failure_id` (int) — `dispatcher.failures.failure_id` for the triggering failure.
- `failure_category` (str) — one of `subprocess_turn_limit`, `stuck_timeout`, `gh_rate_exhausted`, `subprocess_crash`, `ci_red_after_retries`, `push_failed`, `pre_push_hook_rejected`, `git_push_network`, `pr_create_failed`, `phase_output_missing`, `ralph_ac_infeasible`, `summary_ac_infeasible`, or any other §8 category the daemon has routed here.
- `tier` (int) — `2` or `3`.
- `issue_number` (int).
- `issue_title` (str).
- `issue_body` (str).
- `recent_phase_transitions` (list of `{phase, ts}`) — last ~10 transitions for this agent, newest first.
- `prior_failures` (list of `{failure_id, category, ts, details}`) — prior `dispatcher.failures` rows on the same issue across all agents (not just this one).
- `prior_diagnoses_this_issue` (list of `{diagnosis_id, failure_category, recommendation, completed_at}`) — prior completed diagnoses on the same `issue_number`. **Use this to avoid repeating a decision that already failed** — e.g. if `retry` was recommended twice and the failure recurred twice, escalate or change strategy. (Added in #3032.)
- `recent_fleet_decisions` (list of `{diagnosis_id, agent_id, issue_number, failure_category, action, reasoning, completed_at}`) — the diagnoser's most-recent decisions across ALL issues in the past 6 hours. Capped at 3 entries by default (tunable via `dispatcher.config.diagnoser_fleet_decisions_cap`; see issue #3057). **Use this to detect fleet-wide spates** — if several different issues hit the same failure class today, the right action may be `file_prerequisite_task` or `escalate`, not patch-per-issue. **But treat these as priors, not evidence** — always cross-check against the verbatim stderr quote from §Step 1. (Added in #3032. The PAT-scope cascade on 2026-04-22/23 is the canonical example; the cap reduction from 20 → 3 is the anchor-bias defense for #3057.)
- `ralph_done_content` (str | null) — contents of `{worktree}/tmp/ralph/ralph-done.txt` if present, else null.
- `pr_url` (str | null) — if a PR was opened.
- `pr_number` (int | null).
- `ci_log_url` (str | null) — URL of the most recent failing CI run (for `ci_red_after_retries` tier-3).
- `prior_mechanical_fix` (dict | null) — **tier 2 only**. What the daemon already tried that didn't stick. Shape: `{category, attempt, retry_after_ts, outcome}`. Null for tier 3.
- `worktree_path` (str) — absolute path to the failing agent's worktree (may or may not still exist; the daemon may have dropped it during retry processing).

---

## Output contract

Update the `dispatcher.diagnoses` row AND upgrade `dispatcher.agents.failure_summary` (issue #2900) in a single transaction. Use a second tiny helper:

```bash
python3 {worktree}/tmp/dispatcher-diagnoser/write_recommendation.py <diagnosis_id> <agent_id> <recommendation_json_file>
```

where the helper runs:

```python
import json, os, re, sys
import psycopg

diagnosis_id = int(sys.argv[1])
agent_id = sys.argv[2]  # UUID string from context.agent_id
with open(sys.argv[3], "r", encoding="utf-8") as f:
    recommendation = json.load(f)

# Issue #2900: upgrade dispatcher.agents.failure_summary with the first
# 1-3 sentences of recommendation.reasoning, truncated to 240 chars.
# The daemon wrote a templated fallback at terminal-time; this upgrade
# replaces it with LLM-authored prose for tier-2/3 failures where we
# already paid for the analysis. Best-effort — a write failure here
# must not block the diagnosis row write.
def _summary_from_reasoning(reasoning: str, cap: int = 240) -> str:
    text = (reasoning or "").strip()
    if not text:
        return ""
    # Take the first 1-3 sentences. Split on sentence terminators (.!?)
    # followed by whitespace or end-of-string. Rejoin up to 3.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    head = " ".join(sentences[:3]).strip()
    if len(head) > cap:
        head = head[: cap - 1].rstrip() + "…"
    return head

summary = _summary_from_reasoning(recommendation.get("reasoning", ""))

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dispatcher.diagnoses "
            "SET recommendation = %s, "
            "    status = 'completed', "
            "    completed_at = now() "
            "WHERE diagnosis_id = %s",
            (json.dumps(recommendation), diagnosis_id),
        )
        if summary:
            cur.execute(
                "UPDATE dispatcher.agents "
                "SET failure_summary = %s WHERE agent_id = %s",
                (summary, agent_id),
            )
    conn.commit()
```

The recommendation JSON has this shape:

```json
{
  "action": "retry" | "retry_with_hint" | "reissue" | "escalate" | "close" | "block_and_comment" | "file_prerequisite_task" | "block_on_existing_task" | "<novel-action-string>",
  "reasoning": "<one paragraph — why this action fits>",
  "hint": "<conditional — retry_with_hint only>",
  "new_scope": "<conditional — reissue only>",
  "title": "<conditional — file_prerequisite_task only>",
  "body": "<conditional — file_prerequisite_task only>",
  "block_labels": ["<conditional — file_prerequisite_task only>"],
  "blocker_issue_number": 42
}
```

Field rules:

- `action` (required) — one of the eight known strings above, OR a novel action string if none fit. Novel actions persist to `dispatcher.unrecognized_diagnoser_actions` and fall back to `escalate` — use only when genuinely needed and the `reasoning` paragraph makes the intended behavior unambiguous.
- `reasoning` (required) — a single paragraph (≤500 chars) explaining the choice in plain English. **The first sentence MUST include the verbatim stderr failure line you identified in §Step 1** (in backticks or double-quotes), unless the stderr genuinely had no failure line — in which case the first sentence must say so explicitly. This gets surfaced in operator dashboards and the §8 weekly report. The first 1-3 sentences are also written to `dispatcher.agents.failure_summary` (issue #2900) so the admin cockpit can show an LLM-authored tooltip on hover over the outcome glyph — keep the opening sentences self-contained ("what happened + why this action"), not mid-argument.
- `hint` (conditional) — required when `action='retry_with_hint'`; the daemon posts it verbatim as an issue comment before enqueueing the retry marker. Ignored for other actions.
- `new_scope` (conditional) — required when `action='reissue'`. **The daemon replaces the issue body wholesale** via `gh issue edit --body-file` (no splicing, no patching — Python writes the string to a file and `gh` edits the body). MUST be a complete, well-formed issue body with `## Goal`, `## Scope`, `## Acceptance criteria`, `## Priority`, `## References` sections and any `Parent: #N` / `Blocked by #N` lines. A diff, patch, or partial body will truncate the issue. Issue #3010.
- `title` + `body` (conditional) — required when `action='file_prerequisite_task'`. Daemon runs `gh issue create --title <title> --body-file <body>`. The title must be conventional-commits style (e.g. `chore(dispatcher): add workflow scope to dispatcher PAT`); the body must be a well-formed issue body with acceptance criteria and verify lines.
- `block_labels` (optional) — applies to `file_prerequisite_task`. List of label names to apply to the newly-created issue (e.g. `["priority/p1", "area/infra", "agent/ready"]`).
- `blocker_issue_number` (conditional) — required when `action='block_on_existing_task'`. Positive integer issue number that the daemon will validate is open + append to the current issue body as `Blocked by #<N>`.

Exit 0 regardless of recommendation. If the recommendation cannot be written (DB down, malformed JSON, subprocess error), exit non-zero so the daemon marks the diagnosis `status='failed'` and falls back to the fixed mechanical escalation policy.

---

## Action selection — decision tree

Work through these questions in order. The first "yes" determines the action. **Remember §Step 1: every answer must be consistent with the verbatim stderr signature you already quoted. If a prior-decisions pattern suggests one category but your stderr quote contradicts it, trust the quote.**

1. **Is this failure caused by an external dependency outage or a transient GitHub/Anthropic/AWS hiccup?** (Signs: `subprocess_crash` category, stderr mentions 5xx / timeouts / DNS / network errors, prior failures on unrelated issues in the same window but NOT a fleet-wide spate on the same category.)
   - → **`retry`**. The mechanical retry already ran once (tier 2), but if the root cause was a transient that has since cleared, a second attempt may succeed. No comment needed.

2. **Is the failure caused by a scope ambiguity or a missing piece of context the agent needed?** (Signs: `subprocess_turn_limit` with ralph spinning on the same scope item; `ci_red_after_retries` where the fix-CI phase kept trying to fix symptoms of a larger design issue.)
   - → **`retry_with_hint`**. Write a short, concrete `hint` that tells the next agent what to focus on or narrow to. Example hints:
     - "ralph hit max turns trying to fix a rebase conflict. Resolve the conflict first (`git rebase origin/main`), then re-run the implementation."
     - "Three fix-CI attempts failed because the test fixture is stale. Regenerate the fixture by running `scripts/regenerate_fixtures.sh` before re-running the implementation."

3. **Is the issue's scope wrong — i.e. the agent correctly implemented what was asked, but the acceptance criteria no longer match reality?** (Signs: ralph completed with SHIP but CI caught a drift; the issue was filed against an older codebase state; the AC uses a field/endpoint that has been renamed/removed.)
   - → **`reissue`**. Write a `new_scope` issue body with corrected acceptance criteria. Include `Parent: #<parent>` if the original had one. Keep the `Verify:` lines concrete.

4. **Does the failure require a human decision that the agent cannot make safely AND you cannot easily file a specific tracking issue?** (Signs: `subprocess_auth_fail`, missing secret, security question, architectural decision, vendor billing concern, any issue label with `type/decision`.)
   - → **`escalate`**. The daemon will add `status/needs-human` + `priority/p1` and post the reasoning as a comment. No retry.

5. **Is the issue itself invalid — duplicate, already-completed, out-of-date, or not actually reproducible?** (Signs: a PR already merged that Closes this issue; the behavior described is the current behavior; the issue is a duplicate of another open issue.)
   - → **`close`**. The daemon will close with `status/invalid` and post the reasoning as the close comment.

6. **Is the blocker a deterministic, operator-action dependency (PAT scope, missing secret, branch protection mis-config, infra gap) that is visible in stderr / PR status AND the right next step is "wait for operator, don't retry"?**
   - → **`block_and_comment`** if the blocker is acknowledged / in-flight and doesn't need its own tracking issue.
   - → **`file_prerequisite_task`** if the blocker is new, deserves its own backlog item, and will likely affect multiple agents. Provide a focused `title` and `body`. The daemon files the issue, appends `Blocked by #<new>` to the current issue, and applies `status/blocked`. Example (training illustration — issue numbers are synthetic placeholders): a fleet-wide infrastructure cascade would file a p1 issue "add workflow scope to dispatcher PAT" and block `#99001` and `#99002` on it.

7. **Is there an already-open tracking issue for this blocker?** (Search via `gh issue list --search "<keywords>" --state open`.)
   - → **`block_on_existing_task`** with `blocker_issue_number = <that issue>`. Avoids duplicate tickets. The daemon validates the target is open, appends `Blocked by #<N>`, applies `status/blocked`.

8. **Does `recent_fleet_decisions` show this same failure class hitting 3+ different issues in the last 6 hours?** (Regardless of which single action category above fits. **Cross-check: does your verbatim stderr quote match the failure class the fleet decisions describe?** If not, the pattern is a coincidence — do not anchor on it. See §Step 1.)
   - → **`file_prerequisite_task`** or **`escalate`** — the pattern is fleet-wide, a per-issue patch won't fix it. Prefer `file_prerequisite_task` if the root cause is trackable; `escalate` if it needs a human to even diagnose.

**When uncertain, prefer `escalate` over a wrong guess.** A human re-classification is cheap; a wrong `close` or `reissue` can destroy context, and a wrong `block_*` may leave the issue stuck until an operator notices.

**Novel actions.** If none of the eight fit — for example, you want "split_task" because the issue's acceptance criteria are actually two independent issues — you MAY emit a novel action string. The daemon will persist `{action_name, payload}` to `dispatcher.unrecognized_diagnoser_actions` and fall back to `escalate` so operators can review. Use novel actions sparingly; the reasoning paragraph MUST make the intended behavior unambiguous.

---

## Per-category guidance — AC-infeasibility categories (issue #3010)

Two categories land in this skill via the post-exit parse path added for #3010:

### `ralph_ac_infeasible` — ralph surfaced infeasibility

**Context bundle extras.** In addition to the shared bundle shape, the daemon populates:

- `infeasible_acs` (list of `{index, evidence}`) — the full array ralph emitted. `index` is 1-based into the issue body's acceptance-criteria list. `evidence` is a paragraph with the citable reason (grep output, file path, conflicting AC index).
- `issue_acceptance_criteria` (list of str, 1-based-addressable) — the issue body's AC list extracted by the daemon so you can correlate `index` → criterion text without re-fetching.

**Default action selection:**

| Situation | Action | Why |
|---|---|---|
| One or two ACs cite a non-existent symbol, but the issue's core intent is clear and the AC can be rewritten | `reissue` | Author wrote ACs against an older codebase state; rewrite the offending AC(s) and let a fresh plan→ralph run satisfy the corrected list. `new_scope` MUST be the full rewritten issue body (wholesale replace — see §Output contract). |
| The whole issue's premise is broken (e.g. it asks to remove a feature that was already removed, or to add a column that already exists) | `close` | The issue is invalid. The reasoning becomes the close comment. |
| One AC is out-of-scope work (depends on a sibling open issue), but the rest of the issue is well-formed | `reissue` | Drop the blocking AC from the rewritten body and add `Blocked by #<sibling>` if appropriate. |
| You cannot tell whether the AC is infeasible or just tricky, and the evidence paragraphs are hand-wavy | `escalate` | Prefer a human re-read over a wrong `reissue`. A human can re-label or rewrite; a wrong `reissue` destroys context. |

**Do not pick `retry` or `retry_with_hint` for this category.** Ralph already evaluated the AC and found it structurally impossible — a second attempt with the same AC will hit the same wall. If the AC text is fine but ralph misread it, prefer `reissue` with a clarified `new_scope` over `retry_with_hint`.

### `summary_ac_infeasible` — summary surfaced infeasibility

**Context bundle extras.** Same as `ralph_ac_infeasible`, plus:

- `ralph_diff` (str) — the full committed diff from ralph's SHIP run (ralph shipped, summary caught the structural impossibility downstream). Useful for aligning the `reissue` rewrite with what ralph already built.
- `summary_ac_mapping` (list) — summary's `ac_mapping` array with `{index, criterion, status, evidence}` for every AC. Lets you see which ACs summary marked deferred / met / unmet_shape_mismatch / infeasible without re-running the classifier.
- `deferred_acs` (list) — the `deferred_acs` list summary emitted (so you can distinguish deferred ACs from infeasible ones when reasoning about scope).

**Default action selection:** same table as `ralph_ac_infeasible` with one addition:

| Situation | Action | Why |
|---|---|---|
| The AC is fine as written; ralph shipped code that matches a different but valid reading of it | `reissue` | Rewrite the AC to match ralph's reading. Because `ralph_diff` is available, the `new_scope` body can explicitly reference "the implementation in commit `<sha>` satisfies this AC" and keep the PR alive via a downstream retry. This is the specific win `summary_ac_infeasible` enables: salvage ralph's work. |
| Ralph shipped something out of scope AND summary correctly flagged the AC as infeasible | `reissue` or `close` — judgment call. `reissue` if the corrected AC is a small rewrite and ralph's diff is mostly reusable; `close` if the corrected AC would require a fundamentally different implementation. | Wasting ralph's diff is OK when the AC is structurally wrong; salvaging is OK when the AC is close to the right one. |
| Summary flagged AC_INFEASIBLE on a single AC out of five, and the other four are met + deferred | `reissue` with a tightened AC | Same salvage path — ralph's diff covers the other four, so the rewritten body keeps the successful work intact and only rewrites the offending AC. |

**`new_scope` semantics — applies to both categories.** `new_scope` is **always the complete rewritten issue body**. The daemon runs `gh issue edit --body-file <path>` with zero parsing or splicing — your output is the full body verbatim. Preserve the structure: `## Goal`, `## Scope`, `## Acceptance criteria`, `## Priority`, `## References`, plus any `Parent: #N` / `Blocked by #N` lines. Do NOT emit a diff, a patch, a list of "changes", or a partial body — the body-file path is authoritative and partial content will truncate the issue.

---

## Per-category guidance — post-merge verify failure (issue #3071)

### `verify_failed_post_merge` — post-merge regression signal

**This category is fundamentally different from pre-merge failures.** The PR is already merged; the code is on `main`; the dispatcher has already posted "merged" state to the cockpit. `/task-v2-verify` then ran against the deployed service and returned `verdict='FAILED'` — which means the deployed behavior did not match the issue's acceptance criteria. Your diagnosis is about a live regression, not a blocked implementation.

**Context bundle extras.** In addition to the shared bundle shape, the daemon populates:

- `pr_number` + `merge_sha` — the merged PR and its merge commit.
- `failure_reason` (str) — the one-line summary from `/task-v2-verify`'s output.
- `evidence_md` (str) — the verify phase's per-AC evidence markdown that was ALSO posted as an issue comment. Usually has "PASS:" / "FAIL:" lines per criterion.

**Default action selection:**

| Situation | Action | Why |
|---|---|---|
| The deployed behavior is clearly broken (a specific AC shows a concrete failure — wrong output, 500 response, missing field), the regression is visible to users, and the root cause is not obvious from the diff alone | `file_prerequisite_task` with `priority/p1` | Open a focused regression issue so a fresh agent (or a human) can investigate on a clean baseline. Title should be conventional-commits style; body should paste the verify `evidence_md` verbatim so the new agent has a reproducer. The daemon applies `Blocked by #<new>` to the current issue — but note that the current issue is already closed-via-merge, so the block is a tracking breadcrumb, not an active gate. |
| The verify failure is ambiguous (intermittent, flaky external dep, unclear whether deployed code or test infra) | `block_and_comment` | Post the verify evidence as a comment and mark `status/needs-human`. A human decides whether to re-run verify, roll back, or file a fresh regression ticket. Do NOT auto-rollback — the daemon never touches `main`. |
| The verify reason is "the deploy workflow succeeded but the service isn't healthy yet" / "DNS cached" / "ECS task still draining" — i.e. it's the verify phase racing the deployment | `escalate` | `retry` is a no-op in this flow (the daemon doesn't re-run verify post-done); `escalate` at least surfaces the race to a human. A future follow-up could add a verify-phase retry loop, but that's not this skill's decision. |

**Do NOT pick `retry` or `retry_with_hint` for this category.** `retry` is a no-op in the post-merge flow (the daemon does not re-run `/task-v2-verify` from the retry-marker path; `phase='done'` is terminal in the post-merge pipeline). Recommending `retry` here will cause the daemon to enqueue a marker that never runs, and the agent will sit forever in `phase='done' status='failed'`. If the failure genuinely looks transient, prefer `escalate` and let a human manually re-invoke verify.

**Do NOT pick `reissue` for this category.** The issue is already closed via merge; editing its body with a new scope will not re-open the PR or revert the merge. `reissue` is a pre-merge remedy.

**Do NOT pick `close` for this category.** The issue is already closed. Re-closing is a no-op that destroys context.

---

## Investigation steps — only as needed

The context bundle should usually be enough. Shell out sparingly:

- `gh issue view <N> --repo judgemind/judgemind --json number,title,body,state,labels,comments` — when the context is stale and the issue may have been edited or commented on since the daemon fetched it.
- `gh pr view <PR> --repo judgemind/judgemind --json statusCheckRollup,files,commits,mergeable,mergeStateStatus` — for tier-3 `ci_red_after_retries` where the failing checks tell you the fix approach.
- `gh run view <run_id> --repo judgemind/judgemind --log-failed` — to read the specific failing CI log. Cap at ~200 lines; the relevant signal is usually at the start or end.
- `gh issue list --search "<keywords>" --state open --repo judgemind/judgemind` — before emitting `file_prerequisite_task`, check whether an open tracking issue already exists; if so, prefer `block_on_existing_task` with that number.
- `git -C {worktree} log --oneline -20` — to see the commit history of the failing agent's branch.
- `git -C {worktree} diff origin/main...HEAD` — the full PR diff. Only needed when deciding between `retry_with_hint` and `reissue`.

**Do NOT:**
- Edit files, run tests, or try to implement the fix yourself.
- Post comments, edit labels, or close issues via `gh`. The daemon owns all writes based on your recommendation.
- Read unrelated parts of the codebase. The context bundle exists so you don't have to.

---

## Step-by-step procedure

1. **Set up.** Write `{worktree}/tmp/dispatcher-diagnoser/read_context.py` and `{worktree}/tmp/dispatcher-diagnoser/write_recommendation.py` helpers (code above). Run the reader with the `diagnosis_id` argument to pull the JSONB context into memory.

2. **Parse the failure signature FIRST (§Step 1 above, issue #3057).** Locate the raw `stderr_tail` in the context bundle, find the last concrete failure line (`FAILED:` / `[remote rejected]` / `error:` / banner), and write it verbatim into the `reasoning` field you will build. **Do this before reading any pattern-bearing field.** The stderr is ground truth; prior decisions are priors, not evidence.

3. **Classify.** Identify `failure_category` and `tier` from the context. Read `prior_mechanical_fix` (tier 2) or `ci_log_url` (tier 3) to understand what already failed. Now — and only now — scan `prior_diagnoses_this_issue` + `recent_fleet_decisions` for patterns. If a pattern suggests a category that conflicts with your verbatim quote from step 2, trust the quote; the pattern is noise.

4. **Decide.** Walk the decision tree above. Do not fetch anything you don't need — context bundle first, GitHub reads only when a specific question remains.

5. **Write recommendation.** Serialize the recommendation dict to `{worktree}/tmp/dispatcher-diagnoser/recommendation.json`, then run the writer helper with the `diagnosis_id`, the `agent_id` (from `context.agent_id`), and the JSON file path. The helper writes both the diagnosis row AND the `dispatcher.agents.failure_summary` upgrade in one transaction — issue #2900.

6. **Exit 0.** Done. The daemon picks up the recommendation on the next supervisor tick.

---

## Examples

### Example 1 — tier 3 `ci_red_after_retries`, reissue

```json
{
  "action": "reissue",
  "reasoning": "The failing test was checking a field name that has since been renamed from `ruling_text` to `ruling_text_html`. Ralph correctly implemented against the issue's AC (which used the old name), and fix-CI kept trying to revert the field name change. The AC is outdated, not the code.",
  "new_scope": "## Goal\n\nExpose `ruling_text_html` on the /api/rulings/{id} endpoint.\n\n## Acceptance criteria\n- [ ] GraphQL `Ruling.rulingTextHtml` returns the HTML variant.\n  **Verify:** `curl dev.api.judgemind.org/graphql` with a sample ruling id returns `rulingTextHtml` populated.\n\nParent: #2782"
}
```

### Example 2 — tier 2 `subprocess_turn_limit`, retry_with_hint

```json
{
  "action": "retry_with_hint",
  "reasoning": "Ralph hit max turns on a merge conflict in `packages/api/src/graphql/schema.ts` that was introduced while this agent was running. A fresh worktree and a rebase-first hint will fix the next attempt.",
  "hint": "Previous attempt exhausted turns on a merge conflict. Run `git -C {worktree} rebase origin/main` first, resolve conflicts in packages/api/src/graphql/schema.ts, then proceed with the implementation."
}
```

### Example 3 — tier 2 `stuck_timeout` recurring, escalate

```json
{
  "action": "escalate",
  "reasoning": "This agent got stuck in phase=ralph for 30 min on both the original attempt and the retry. Both times the last phase transition was on a specific test that depends on a flaky external API (`httpbin.org`). Needs human to either mark the test xfail or replace with a local fixture.",
  "hint": null
}
```

### Example 4 — `pre_push_hook_rejected` with fleet-wide PAT-scope pattern, file_prerequisite_task

Training example — issue numbers are synthetic placeholders (`#99001`, `#99002`). Do not pattern-match these as current-state infrastructure problems; always verify any cited issue via `gh issue view` before treating it as a live blocker.

```json
{
  "action": "file_prerequisite_task",
  "reasoning": "Six consecutive git-push failures in the last 6 hours across #99001 and #99002 all hit 'refusing to allow a Personal Access Token to create or update workflow .* without workflow scope'. This is a dispatcher PAT configuration gap — the secret in AWS Secrets Manager needs the `workflow` scope added. Filing a prerequisite task rather than blocking per-issue because the fix affects every in-flight agent.",
  "title": "chore(infra): add workflow scope to dispatcher PAT",
  "body": "## Goal\n\nAdd the `workflow` OAuth scope to the dispatcher's GitHub PAT so agents can push commits that add/modify `.github/workflows/*` files.\n\n## Acceptance criteria\n- [ ] Dispatcher PAT in AWS Secrets Manager has `workflow` scope.\n  **Verify:** a test dispatcher agent can `git push` a branch that adds a file under `.github/workflows/` without hitting 'refusing to allow a Personal Access Token' rejection.\n\n## Priority\n\np1 — blocks the entire dispatcher fleet.",
  "block_labels": ["priority/p1", "area/infra", "type/chore", "agent/ready"]
}
```

### Example 5 — `push_failed` with existing tracking issue, block_on_existing_task

Training example — the `#99003` issue number is a synthetic placeholder. In a real diagnosis, `gh issue list --search` returns the actual open tracking issue and that number is used here.

```json
{
  "action": "block_on_existing_task",
  "reasoning": "The PAT-scope blocker is already tracked at #99003 (filed 2 hours ago by the dispatcher when it caught the same pattern). Filing a duplicate would be noise — block this issue on the existing one instead.",
  "blocker_issue_number": 99003
}
```

### Example 6 — `phase_output_missing` on ralph, block_and_comment

```json
{
  "action": "block_and_comment",
  "reasoning": "Ralph's subprocess exited 0 but `{worktree}/tmp/dispatcher-output/ralph.json` is missing. The last 200 lines of the phase log show Claude Code's internal harness dumped a JSON parse error before the skill could write. This is a dispatcher-image bug that needs a Claude Code version bump — not something a fresh retry will fix. Marking blocked so no other agent picks this up while the operator investigates."
}
```

### Example 7 — coverage-floor pre-push abort, `file_prerequisite_task` (anchor-bias regression, #3057)

```json
{
  "action": "file_prerequisite_task",
  "reasoning": "The stderr ends with `\"FAILED: coverage floor for scraper-framework\"` followed by `\"FAIL: packages/scraper-framework: coverage dropped 80.0% -> 68.6% (floor violation)\"` and `\"1 check(s) failed. Push aborted.\"`. This is a local pre-push hook abort — the push never reached the remote. Despite several PAT-cascade decisions in recent_fleet_decisions, the verbatim stderr signature is coverage regression, not PAT scope. Filing a prerequisite task to restore the coverage floor before retrying.",
  "title": "chore(scraper-framework): restore coverage floor after watchdog expansion",
  "body": "## Goal\n\nRestore the `packages/scraper-framework` coverage floor to 80.0% (currently 68.6%) after the cc-retired watchdog expansion added uncovered branches.\n\n## Acceptance criteria\n- [ ] `scripts/update-coverage-baselines.py --package packages/scraper-framework` exits 0 at 80.0%+.\n  **Verify:** run pre-push hook against a no-op push on the worktree; the coverage-floor check passes.\n\n## Priority\n\np1 — blocks merge of the cc-retired watchdog work.",
  "block_labels": ["priority/p1", "area/scraping", "agent/ready"]
}
```

---

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules. Write helper scripts to `{worktree}/tmp/dispatcher-diagnoser/` first, then invoke them.
- All temp files go under `{worktree}/tmp/`, never `/tmp/`.
- This skill is Opus-tier per spec §18 — but the task is narrow. Do NOT over-investigate. The decision tree is intentionally short.
- **Known actions (the daemon recognizes these eight):** `retry`, `retry_with_hint`, `reissue`, `escalate`, `close`, `block_and_comment`, `file_prerequisite_task`, `block_on_existing_task`. You MAY propose a novel action string when none of the known set fits the situation — the daemon will log the action name and payload to `dispatcher.unrecognized_diagnoser_actions` and fall back to `escalate` so an operator can review it. Novel actions are an explicit escape hatch; prefer a known action when one fits.
- Exit 0 means "recommendation written". Exit non-zero means "I could not diagnose" — the daemon falls back to fixed mechanical escalation.
- **§Step 1 is mandatory — always quote the verbatim stderr failure line before consulting priors (issue #3057).**
