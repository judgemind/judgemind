---
description: Diagnose a tier 2/3 dispatcher failure. Reads context from dispatcher.diagnoses.context, returns a structured recommendation that the daemon consumes deterministically.
argument-hint: "<diagnosis_id>"
maxTurns: 30
model: opus
---

# /diagnose-failure skill

Diagnoser for the dispatcher v2 tier-2/3 failure flow (`docs/specs/dispatcher-v2-spec.md` §8). Invoked as `claude -p '/diagnose-failure <diagnosis_id>'` by the daemon when a tier-2 failure recurs after its mechanical retry, or when a tier-3 failure fires on first occurrence (`ci_red_after_retries`). Reads the context bundle the daemon wrote to `dispatcher.diagnoses.context`, proposes one of five deterministic actions (`retry`, `retry_with_hint`, `reissue`, `escalate`, `close`), and writes a structured recommendation back to the same row. The daemon executes the chosen action — this skill does not write to GitHub directly.

**Prerequisites:** The daemon has already (a) written a `dispatcher.diagnoses` row with `status='pending'` and a serialized context bundle, (b) spawned this skill with the `diagnosis_id` as the argument.

**Goal:** Update `dispatcher.diagnoses.recommendation` (JSONB) with `{action, reasoning, hint?, new_scope?}` and set `status='completed'`, `completed_at=now()`. The daemon's next supervisor tick consumes the recommendation.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — No side effects on GitHub or the filesystem.** This skill does NOT comment on issues, edit labels, close issues, edit PRs, or modify the worktree. The daemon owns all of those operations — the skill's only write is the UPDATE on `dispatcher.diagnoses`. The skill MAY read from GitHub (`gh issue view`, `gh pr view`, `gh run view --log-failed`) and from local files in the failed agent's worktree when the context bundle references them.

**IMPORTANT — 5-minute wall-clock budget.** The daemon kills this subprocess after 5 minutes. Aim for the simplest viable recommendation within that budget; default to `escalate` when genuinely uncertain — a human can always re-classify. Do not chase rabbit holes.

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

- `agent_id` (str) — the failing agent's UUID.
- `failure_id` (int) — `dispatcher.failures.failure_id` for the triggering failure.
- `failure_category` (str) — one of `subprocess_turn_limit`, `stuck_timeout`, `gh_rate_exhausted`, `subprocess_crash`, `ci_red_after_retries`, or any other §8 category the daemon has routed here.
- `tier` (int) — `2` or `3`.
- `issue_number` (int).
- `issue_title` (str).
- `issue_body` (str).
- `recent_phase_transitions` (list of `{phase, ts}`) — last ~10 transitions for this agent, newest first.
- `prior_failures` (list of `{failure_id, category, ts, details}`) — prior `dispatcher.failures` rows on the same issue across all agents (not just this one).
- `ralph_done_content` (str | null) — contents of `{worktree}/tmp/ralph/ralph-done.txt` if present, else null.
- `pr_url` (str | null) — if a PR was opened.
- `pr_number` (int | null).
- `ci_log_url` (str | null) — URL of the most recent failing CI run (for `ci_red_after_retries` tier-3).
- `prior_mechanical_fix` (dict | null) — **tier 2 only**. What the daemon already tried that didn't stick. Shape: `{category, attempt, retry_after_ts, outcome}`. Null for tier 3.
- `worktree_path` (str) — absolute path to the failing agent's worktree (may or may not still exist; the daemon may have dropped it during retry processing).

---

## Output contract

Update the `dispatcher.diagnoses` row. Use a second tiny helper:

```bash
python3 {worktree}/tmp/dispatcher-diagnoser/write_recommendation.py <diagnosis_id> <recommendation_json_file>
```

where the helper runs:

```python
import json, os, sys
import psycopg

diagnosis_id = int(sys.argv[1])
with open(sys.argv[2], "r", encoding="utf-8") as f:
    recommendation = json.load(f)
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
    conn.commit()
```

The recommendation JSON has this shape:

```json
{
  "action": "retry" | "retry_with_hint" | "reissue" | "escalate" | "close",
  "reasoning": "<one paragraph — why this action fits>",
  "hint": "<optional — comment text to post on the issue before retry>",
  "new_scope": "<optional — rewritten issue body for action='reissue'>"
}
```

Field rules:

- `action` (required) — exactly one of the five strings above.
- `reasoning` (required) — a single paragraph (≤500 chars) explaining the choice in plain English. This gets surfaced in operator dashboards and the §8 weekly report.
- `hint` (conditional) — required when `action='retry_with_hint'`; the daemon posts it verbatim as an issue comment before enqueueing the retry marker. Ignored for other actions.
- `new_scope` (conditional) — required when `action='reissue'`; the daemon replaces the issue body with this string. Should be a full, well-formed issue body with acceptance criteria — not a diff.

Exit 0 regardless of recommendation. If the recommendation cannot be written (DB down, malformed JSON, subprocess error), exit non-zero so the daemon marks the diagnosis `status='failed'` and falls back to the fixed mechanical escalation policy.

---

## Action selection — decision tree

Work through these questions in order. The first "yes" determines the action.

1. **Is this failure caused by an external dependency outage or a transient GitHub/Anthropic/AWS hiccup?** (Signs: `subprocess_crash` category, stderr mentions 5xx / timeouts / DNS / network errors, prior failures on unrelated issues in the same window.)
   - → **`retry`**. The mechanical retry already ran once (tier 2), but if the root cause was a transient that has since cleared, a second attempt may succeed. No comment needed.

2. **Is the failure caused by a scope ambiguity or a missing piece of context the agent needed?** (Signs: `subprocess_turn_limit` with ralph spinning on the same scope item; `ci_red_after_retries` where the fix-CI phase kept trying to fix symptoms of a larger design issue.)
   - → **`retry_with_hint`**. Write a short, concrete `hint` that tells the next agent what to focus on or narrow to. Example hints:
     - "ralph hit max turns trying to fix a rebase conflict. Resolve the conflict first (`git rebase origin/main`), then re-run the implementation."
     - "Three fix-CI attempts failed because the test fixture is stale. Regenerate the fixture by running `scripts/regenerate_fixtures.sh` before re-running the implementation."

3. **Is the issue's scope wrong — i.e. the agent correctly implemented what was asked, but the acceptance criteria no longer match reality?** (Signs: ralph completed with SHIP but CI caught a drift; the issue was filed against an older codebase state; the AC uses a field/endpoint that has been renamed/removed.)
   - → **`reissue`**. Write a `new_scope` issue body with corrected acceptance criteria. Include `Parent: #<parent>` if the original had one. Keep the `Verify:` lines concrete.

4. **Does the failure require a human decision that the agent cannot make safely?** (Signs: `subprocess_auth_fail`, missing secret, security question, architectural decision, vendor billing concern, any issue label with `type/decision`.)
   - → **`escalate`**. The daemon will add `status/needs-human` + `priority/p1` and post the reasoning as a comment. No retry.

5. **Is the issue itself invalid — duplicate, already-completed, out-of-date, or not actually reproducible?** (Signs: a PR already merged that Closes this issue; the behavior described is the current behavior; the issue is a duplicate of another open issue.)
   - → **`close`**. The daemon will close with `status/invalid` and post the reasoning as the close comment.

**When uncertain, prefer `escalate` over a wrong guess.** A human re-classification is cheap; a wrong `close` or `reissue` can destroy context.

---

## Investigation steps — only as needed

The context bundle should usually be enough. Shell out sparingly:

- `gh issue view <N> --repo judgemind/judgemind --json number,title,body,state,labels,comments` — when the context is stale and the issue may have been edited or commented on since the daemon fetched it.
- `gh pr view <PR> --repo judgemind/judgemind --json statusCheckRollup,files,commits,mergeable,mergeStateStatus` — for tier-3 `ci_red_after_retries` where the failing checks tell you the fix approach.
- `gh run view <run_id> --repo judgemind/judgemind --log-failed` — to read the specific failing CI log. Cap at ~200 lines; the relevant signal is usually at the start or end.
- `git -C {worktree} log --oneline -20` — to see the commit history of the failing agent's branch.
- `git -C {worktree} diff origin/main...HEAD` — the full PR diff. Only needed when deciding between `retry_with_hint` and `reissue`.

**Do NOT:**
- Edit files, run tests, or try to implement the fix yourself.
- Post comments, edit labels, or close issues via `gh`. The daemon owns all writes based on your recommendation.
- Read unrelated parts of the codebase. The context bundle exists so you don't have to.

---

## Step-by-step procedure

1. **Set up.** Write `{worktree}/tmp/dispatcher-diagnoser/read_context.py` and `{worktree}/tmp/dispatcher-diagnoser/write_recommendation.py` helpers (code above). Run the reader with the `diagnosis_id` argument to pull the JSONB context into memory.

2. **Classify.** Identify `failure_category` and `tier` from the context. Read `prior_mechanical_fix` (tier 2) or `ci_log_url` (tier 3) to understand what already failed.

3. **Decide.** Walk the decision tree above. Do not fetch anything you don't need — context bundle first, GitHub reads only when a specific question remains.

4. **Write recommendation.** Serialize the recommendation dict to `{worktree}/tmp/dispatcher-diagnoser/recommendation.json`, then run the writer helper with the `diagnosis_id` and the JSON file path.

5. **Exit 0.** Done. The daemon picks up the recommendation on the next supervisor tick.

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

---

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules. Write helper scripts to `{worktree}/tmp/dispatcher-diagnoser/` first, then invoke them.
- All temp files go under `{worktree}/tmp/`, never `/tmp/`.
- This skill is Opus-tier per spec §18 — but the task is narrow. Do NOT over-investigate. The decision tree is intentionally short.
- **The five actions are exhaustive.** Do not invent a sixth (`defer`, `split`, `merge`, etc.) — the daemon's consumer has five deterministic branches and will fall back to escalation on any other string.
- Exit 0 means "recommendation written". Exit non-zero means "I could not diagnose" — the daemon falls back to fixed mechanical escalation.
