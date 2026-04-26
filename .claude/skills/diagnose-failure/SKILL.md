---
description: Diagnose a dispatcher agent-terminal failure. Reads context from dispatcher.diagnoses.context, takes whatever action is needed (patch, file issue, comment, etc.) with the same authority as a /task agent, and writes a 3-state directive the daemon consumes deterministically.
argument-hint: "<diagnosis_id>"
model: opus
---

# /diagnose-failure skill — empowered diagnoser (issue #3366)

Last-ditch resolution point for any failure the per-phase pipeline can't auto-recover from. Invoked as `claude -p '/diagnose-failure <diagnosis_id>'` by the daemon when an agent terminates with a tier-2/3 failure category. Reads the failure context, **does whatever needs doing** (patch the agent's branch, file a prerequisite issue, comment, edit labels), and writes a 3-state directive (`respawn_at=<phase>` / `terminal` / NULL) that the daemon's reaper consumes.

This is the v1 mental model that v2 lost: one smart agent with broad context handles the long tail. The diagnoser has the same authority surface as a `/task` agent or the operator-laptop dispatcher session — bounded only by the bright lines below.

**No turn cap, no wall-clock cap (within reason).** The daemon's `DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS` is set to 90 minutes as a sanity ceiling for sub-skill invocations and real diagnostic work; operator visibility on long-running diagnoses comes via the structured-log stream, not a hard kill.

**Recommendation contract preserved (8 known actions).** Issue #3032's recommendation field stays for audit / operator review and the weekly report — the daemon's deterministic consumer still reads it and runs the matching action (`retry`, `retry_with_hint`, `reissue`, `escalate`, `close`, `block_and_comment`, `file_prerequisite_task`, `block_on_existing_task`). What's new in #3366 is that you ALSO write a `next_directive` column AND can take direct side effects (commit/push, gh issue create/edit) when the situation calls for it.

**Prerequisites:** The daemon has already (a) written a `dispatcher.diagnoses` row with `status='pending'` and a serialized context bundle, (b) spawned this skill with the `diagnosis_id` as the argument AND with the `JUDGEMIND_DIAGNOSER_RUN=1` env var set (the bright-line hook reads this).

**Goal — three writes to the `dispatcher.diagnoses` row before exit:**

1. `recommendation` (JSONB) — the 8-action shape (or a novel action string for operator review). Same schema as #3032.
2. `next_directive` (TEXT) — `respawn_at=<phase>` | `terminal` | NULL.
3. `actions_taken` (JSONB array) — append-only audit log of every side-effect you took.

Plus the existing `dispatcher.agents.failure_summary` upgrade (first 1-3 sentences of `recommendation.reasoning` ≤240 chars, issue #2900).

**Important — `status` ownership:** Do NOT set `status='completed'` in `write_recommendation.py`. Leave the row at `status='pending'` on SKILL exit. The daemon's reaper queries `WHERE status='pending'` and calls `_consume_diagnosis`; the daemon then writes `status='completed'` after the directive action runs. If the SKILL sets `status='completed'` before exiting, the reaper skips the row and the directive is silently dropped (issue #3422).

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task. All sub-skills run synchronously.

## Capabilities — same as a peer agent

You can use the full toolset of a `/task` agent or the operator's dispatcher session:

- **Bash** — `git`, `gh`, `aws`, `psql` (via `scripts/dev-db-query.sh`), any other shell command. Set `timeout: 1200000` on long-running commands per `CLAUDE.md`.
- **Edit / Write / Read / Glob / Grep** — full filesystem access. Edit files in the failed agent's worktree, write helper scripts to `{worktree}/tmp/dispatcher-diagnoser/`, read PR diffs, etc.
- **Agent (sub-skill invocation, uncapped)** — call `/task-v2-fix-conflict`, `/tdd`, `/ralph`, `/audit`, etc. as many times as judgment requires. Sub-skills run with their own normal contracts.
- **MCP servers** — `github`, `awslabs_cloudwatch-mcp-server`, `awslabs_ecs-mcp-server`, `plugin:telegram` (read/notify only — see Telegram Integration in `CLAUDE.md`).
- **gh / git / aws CLI** — full operator-tier authority. You may commit and push to the failed agent's branch, file new issues, edit issue/PR bodies, add/remove labels, post comments. AWS reads (CloudWatch logs, ECS describe-tasks) plus same writes the daemon already has.

## Bright lines — irreducible (hook-enforced)

These are policy, not parsimony. Same lines that bound `/task` agents and the operator-laptop preflight. The diagnoser env var (`JUDGEMIND_DIAGNOSER_RUN=1`, set by the daemon when spawning this skill) lets `.claude/hooks/preflight-bash.sh` enforce them automatically:

1. **No production deploy.** `terraform apply` against `environments/production/`, ECS service writes against `*-production` clusters. Human-only per `CLAUDE.md`. Hook blocks any matching command.
2. **No PAT rotation / `gh auth switch`.** Operator-only. Hook blocks `gh auth switch`.
3. **No force-push to main, no amending merged commits.** Destructive across all agents. Hook blocks `git push --force` / `--force-with-lease` to `main`/`master` and blocks `git commit --amend` against merged commits.
4. **No recursive `/diagnose-failure` invocation.** Depth-1 cap. If a sub-action fails, escalate — don't re-enter. Hook blocks `claude -p '/diagnose-failure ...'` from within a diagnoser run.

If a hook blocks a command, do NOT try to work around it. The hooks ARE policy. Default to `escalate` instead.

## Audit trail — `dispatcher.diagnoses.actions_taken`

Every side-effect action you take must be appended to `dispatcher.diagnoses.actions_taken` (JSONB array). Operators review post-hoc; git history covers code changes, this column covers everything else.

Schema for each entry:

```jsonb
{"ts": "2026-04-25T18:42:00Z", "type": "git_commit", "sha": "abc123", "message": "fix(deps): align anthropic floor"}
{"ts": "...", "type": "git_push", "branch": "worktree-agent-XYZ", "remote": "origin"}
{"ts": "...", "type": "gh_issue_create", "issue_number": 3401, "title": "..."}
{"ts": "...", "type": "gh_issue_edit", "issue_number": 3297, "labels_added": ["status/blocked"], "labels_removed": ["agent/ready"]}
{"ts": "...", "type": "gh_issue_comment", "issue_number": 3297, "body_preview": "first 200 chars..."}
{"ts": "...", "type": "skill_invoke", "skill": "task-v2-fix-conflict", "exit_code": 0}
{"ts": "...", "type": "bash_run", "cmd": "git rebase origin/main", "exit_code": 0}
```

Action types to log:

- `git_commit` — every commit you (or a sub-skill on your behalf) make.
- `git_push` — every push.
- `gh_issue_create` — `gh issue create`, with the new issue number.
- `gh_issue_edit` — `gh issue edit`, including label add/remove and body changes.
- `gh_issue_comment` — `gh issue comment`.
- `skill_invoke` — every sub-skill call.
- `bash_run` — non-trivial commands (don't log every `cat`/`ls`/`grep`; log every `git rebase` / `gh issue close` / `aws ...` / `pytest` / etc.).

Append-only writer:

```python
# {worktree}/tmp/dispatcher-diagnoser/log_action.py
import json, os, sys
from datetime import datetime, timezone
import psycopg

diagnosis_id = int(sys.argv[1])
action = json.loads(sys.argv[2])
action.setdefault("ts", datetime.now(timezone.utc).isoformat())
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dispatcher.diagnoses "
            "SET actions_taken = COALESCE(actions_taken, '[]'::jsonb) || %s::jsonb "
            "WHERE diagnosis_id = %s",
            (json.dumps([action]), diagnosis_id),
        )
    conn.commit()
```

## `next_directive` — the 3-state daemon signal

Before you exit, the daemon's reaper needs to know whether to respawn the agent or free the slot. **Always write one of three values:**

| Value | Meaning | What the daemon does |
|---|---|---|
| `respawn_at=<phase>` | You advanced the work (committed a fix, etc.). Resume the agent at the named phase. | Daemon launches a fresh agent-runner ECS task on the same `agent_id` with `START_PHASE=<phase>` env. Same branch, same issue. |
| `terminal` | You explicitly say no further action. Whatever needed doing — issue closed, prerequisite filed, comment posted — you already did directly via gh/git. | Daemon frees the slot, logs `daemon.diagnoser_completed_terminal`. |
| (absent / NULL) | You did not write a directive (crashed, OOM, network error, etc.). | Daemon falls back to `escalate` AND logs `daemon.diagnoser_did_not_complete` for operator visibility. |

The explicit `terminal` directive is what makes "I ran and decided no more action" distinguishable from "I crashed before finishing." Always write one when you finished your run.

Valid `respawn_at` phases (mirrored from `daemon.AGENT_RUNNER_VALID_START_PHASES` and the entrypoint's allowlist): `planning`, `setup`, `ralph`, `summary`, `push_and_pr`, `awaiting_ci`, `fix_ci`, `merge`, `awaiting_deploy`, `verify`. The daemon validates against this set; an unknown phase falls back to escalate.

A small directive writer:

```python
# {worktree}/tmp/dispatcher-diagnoser/write_directive.py
import os, sys
import psycopg

diagnosis_id = int(sys.argv[1])
directive = sys.argv[2]  # "respawn_at=ralph" | "terminal"
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dispatcher.diagnoses "
            "SET next_directive = %s WHERE diagnosis_id = %s",
            (directive, diagnosis_id),
        )
    conn.commit()
```

---

## Step 1 — Parse the failure signature FIRST (anchor-bias defense — #3057)

**This is a mandatory first step.** Before you read `prior_diagnoses_this_issue`, `recent_fleet_decisions`, or any other pattern-bearing field, find the **actual failure signature in the raw stderr** and quote it verbatim.

**The rule: stderr is ground truth; prior decisions are priors, not evidence.**

### Procedure

1. From the context bundle, locate the raw stderr text. Depending on the category, it lives in one of:
   - `context.prior_failures[0].details.stderr_tail` (the most recent failure on this issue, when it matches the triggering failure_id)
   - `context.details.stderr_tail` (when the daemon inlined the triggering failure's details at the top level — category-dependent)
   - `context.ralph_done_content` (for ralph-phase failures)
   - The `ci_log_url` / `gh run view --log-failed` output (for `ci_red_after_retries`)

2. Scan the stderr from the **bottom up** and extract the **last** concrete failure line. "Concrete" means one of:
   - A line starting with `FAILED:` (pre-push hook convention)
   - A line containing `[remote rejected]` or `! [rejected]` (git push output)
   - A line starting with `error:` or `fatal:` (git / ruff / pytest conventions)
   - An explicit banner like `Push aborted.`, `check(s) failed.`, `tests failed`, `coverage dropped`, `floor violation`
   - For pytest: the last `FAILED packages/... ::test_name` line from the summary
   - For CI log: the last `##[error]` line or the last non-zero-exit line

3. **Quote that line verbatim** in the `reasoning` field of your final recommendation. Use backticks or double-quotes to set the quoted text apart. **Do not paraphrase, do not summarize, do not compress.** Copy-paste the exact characters.

4. **Only then** consult `prior_diagnoses_this_issue` and `recent_fleet_decisions`. Treat them as priors — useful for detecting fleet-wide spates, dangerous as substitutes for reading the stderr.

### When the stderr has no identifiable failure line

Say so verbatim in the reasoning, then default to `escalate` (with `next_directive=terminal` after you escalate via `gh`).

### Why this step exists — the #3057 anchor-bias regression

On 2026-04-23 the Opus diagnoser hallucinated a PAT-scope cascade push-rejection on a coverage-floor failure. Diagnosis #15 (agent `6d4029f0`, issue #2613) had `recent_fleet_decisions` populated with 9 prior `block_on_existing_task → #3038` decisions — all legitimate PAT cascades on different issues. The actual stderr ended with `"FAILED: coverage floor"` + `"coverage dropped 80.0% -> 68.6%"` and the push never reached the remote. The diagnoser confabulated a `"refusing to allow a Personal Access Token..."` rejection from the fleet pattern. The verbatim-quote requirement closes that failure mode by forcing the LLM to ground its classification in the actual stderr before consulting pattern-bearing context.

---

## Step 2 — Decide the action shape

Walk the decision tree below. For most cases the action shape is clear from the failure category and the verbatim stderr. **Pre-#3366 the diagnoser was constrained to recommendation-only output; post-#3366 you can also do the work directly** — patch the agent's branch, file the issue with `gh issue create`, etc. Use that authority when the action is mechanical and obvious; reserve recommendation-only output (no direct side effects) for cases where a human should still review.

### Action selection — the eight known recommendations

Same decision tree as before #3366; the recommendation field stays as audit context.

1. **External-dep transient** (`subprocess_crash` with 5xx/timeout, no fleet-wide pattern) → `retry`. No comment needed.
2. **Scope ambiguity / missing context** (`subprocess_turn_limit` / `ci_red_after_retries` looping on a fixable thing) → `retry_with_hint`. Write a concrete `hint`.
3. **AC mismatch with reality** (ralph SHIPped, CI caught a drift; the AC uses a renamed field) → `reissue`. Write a `new_scope` body (full rewrite — see below).
4. **Needs human** (`type/decision`, missing secret, vendor billing) → `escalate`.
5. **Issue invalid** (duplicate, already-closed, not reproducible) → `close`.
6. **Operator-action blocker, in-flight** → `block_and_comment`.
7. **Operator-action blocker, new** → `file_prerequisite_task` with focused `title` + `body`.
8. **Tracking issue already exists** → `block_on_existing_task` with `blocker_issue_number`.

**When uncertain, prefer `escalate` over a wrong guess.** A human re-classification is cheap; a wrong `close` or `reissue` can destroy context.

### Per-category guidance — `conflict_unresolvable` (#3225 / #3366)

Routed here on the bypass-fix from #3366. Read `context.details.conflict_files` + `resolution_notes` (and re-fetch the fix_conflict phase_outputs row if needed).

**You can fix it directly.** If the conflict is small (e.g. anthropic SDK floor reconciliation, parallel renames), check out the agent's branch, edit the file, commit, push, and write `next_directive=respawn_at=push_and_pr`. The daemon's reaper will spawn a fresh agent-runner that resumes at `push_and_pr` and runs the rest of the pipeline against your committed resolution.

If the conflict is structural (semantic collision — function rewritten on main, feature reverted), recommend `escalate` or `reissue` with a clarified `new_scope`, and write `next_directive=terminal`.

### Per-category guidance — `agent_runner_route_stub` (#3366)

The agent-runner entrypoint hit a transition shape it didn't know how to route (often `ralph_not_ship` — ralph terminated with a non-SHIP verdict). Read `context.details.route_hint`, the agent's commits (`git log --oneline origin/main..HEAD`), and the latest reviewer feedback (`{worktree}/tmp/ralph/*-feedback.md` if present).

Most cases route to `retry_with_hint` (write a hint, set `next_directive=respawn_at=ralph` if you reset the agent's commits, otherwise `terminal`) or `block_and_comment` / `escalate` for structurally-stuck cases.

### Per-category guidance — AC-infeasibility (#3010)

Same table as before #3366 — `ralph_ac_infeasible` and `summary_ac_infeasible` route to `reissue` (rewrite the AC), `close` (premise broken), or `escalate` (uncertain). The recommendation drives the daemon-side action; `next_directive=terminal` after `escalate` / `close` (no respawn), or `next_directive=respawn_at=planning` if you want a fresh plan→ralph against the rewritten body.

The full AC-infeasibility tables (ralph_ac_infeasible vs summary_ac_infeasible decision matrices, `new_scope` semantics, `ralph_diff` salvage path) are documented in the surrounding code comments + the spec — re-read those when handling these categories. Key constraint: `new_scope` is **always the complete rewritten issue body**; the daemon's `gh issue edit --body-file` does no parsing or splicing. A diff or partial body will truncate the issue.

### Per-category guidance — `merge_conflict_at_push` (#2964)

Daemon-side pre-push rebase hit a conflict; rebase aborted, no PR opened. Read `context.details.conflict_files`. Pre-#3366 the recommendation was the only output; post-#3366 you can attempt to resolve the rebase yourself if the conflict looks routine (parallel imports, lockfile noise) and write `next_directive=respawn_at=push_and_pr`. For structural conflicts, recommend `block_and_comment` or `file_prerequisite_task` and `next_directive=terminal`.

### Per-category guidance — `verify_failed_post_merge` (#3071)

PR is already merged; deploy is live; verify caught a regression. The issue is closed-via-merge — `reissue` / `close` / `retry` are no-ops. **Do not pick `retry` or `retry_with_hint` for this category.** `retry` is a no-op in the post-merge flow (the daemon does not re-run `/task-v2-verify` from the retry-marker path; `phase='done'` is terminal in the post-merge pipeline). Recommend `file_prerequisite_task` (regression issue with the verify evidence_md as reproducer, p1) or `block_and_comment` (needs-human). `next_directive=terminal` always — there's no respawn that re-runs verify on a closed issue.

**Do not pick `reissue` for this category.** The issue is already closed via merge; editing its body with a new scope will not re-open the PR or revert the merge. `reissue` is a pre-merge remedy.

**Do not pick `close` for this category.** The issue is already closed. Re-closing is a no-op that destroys context.

---

## Step 3 — Execute (recommendation OR direct action)

If the recommendation alone is enough (the daemon's deterministic consumer handles `retry`/`reissue`/`escalate`/etc.), just write the recommendation + directive.

If you need to do the work directly — commit a fix, file an issue, post a structured comment that needs LLM-authored prose — DO IT. Log every side effect to `actions_taken` (see §Audit trail). When the work is done, write `next_directive=respawn_at=<phase>` (if the agent should resume the pipeline) or `next_directive=terminal` (if you handled everything).

### Sub-skill invocation

You can call `/task-v2-fix-conflict`, `/tdd`, `/ralph`, etc. via the Agent tool. Each sub-skill runs synchronously, returns its verdict, and you log a `skill_invoke` entry. Sub-skills are uncapped — call as many as judgment requires.

### Bright-line reminders

The hooks block these automatically when `JUDGEMIND_DIAGNOSER_RUN=1` is set. If you trip a hook, default to `escalate`:

- No production deploy.
- No `gh auth switch` / PAT rotation.
- No force-push to main / amending merged commits.
- No recursive `/diagnose-failure` (don't call this skill from inside this skill).

---

## Input contract — the `context` JSONB

Same shape as before #3366 (the daemon serializes this in `_build_diagnoser_context`):

- `agent_id` (str) — UUID. **Required for the `failure_summary` upgrade write (#2900).**
- `failure_id` (int) — `dispatcher.failures.failure_id` for the triggering failure.
- `failure_category` (str) — see `daemon.py` for the full list. New in #3366: `agent_runner_route_stub`.
- `tier` (int) — 2 or 3.
- `issue_number`, `issue_title`, `issue_body`.
- `recent_phase_transitions` — last ~10.
- `prior_failures` — same issue, all agents.
- `prior_diagnoses_this_issue` — same issue, completed only.
- `recent_fleet_decisions` — capped at 3 by default (anchor-bias defense, #3057). Operators can tune via `dispatcher.config.diagnoser_fleet_decisions_cap`.
- `ralph_done_content` — `{worktree}/tmp/ralph/ralph-done.txt` if present.
- `pr_url`, `pr_number`.
- `ci_log_url`.
- `prior_mechanical_fix` — tier 2 only.
- `worktree_path` — absolute path; may be empty in ECS mode (the daemon's host doesn't have the agent's worktree on disk).
- Per-category extras for `ralph_ac_infeasible` / `summary_ac_infeasible` / `conflict_unresolvable` / `merge_conflict_at_push` / `verify_failed_post_merge` (see `daemon._build_diagnoser_context`).

For #3366 the daemon also inlines `details.route_hint` (for `agent_runner_route_stub`) and the fix_conflict phase output fields (`conflict_files`, `resolution_notes`, `budget_exhausted`) for `conflict_unresolvable`.

Read it via:

```python
# {worktree}/tmp/dispatcher-diagnoser/read_context.py
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

---

## Output contract — recommendation writer

Update `recommendation` + `dispatcher.agents.failure_summary` (issue #2900) in a single transaction. The SKILL writes `recommendation` only — `status` stays `'pending'` so the daemon's reaper picks it up. Use a small helper:

```python
# {worktree}/tmp/dispatcher-diagnoser/write_recommendation.py
import json, os, re, sys
import psycopg

diagnosis_id = int(sys.argv[1])
agent_id = sys.argv[2]  # UUID string from context.agent_id
with open(sys.argv[3], "r", encoding="utf-8") as f:
    recommendation = json.load(f)

def _summary_from_reasoning(reasoning: str, cap: int = 240) -> str:
    text = (reasoning or "").strip()
    if not text:
        return ""
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
            "SET recommendation = %s "
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

**Do not add `status = 'completed'` or `completed_at = now()` to this UPDATE.** The daemon's `_consume_diagnosis` / `_mark_diagnosis_completed` owns the `status` transition. Premature `'completed'` causes the reaper (`WHERE status='pending'`) to skip the row and silently drop the directive (issue #3422).

Recommendation shape (unchanged from #3032):

```json
{
  "action": "retry" | "retry_with_hint" | "reissue" | "escalate" | "close" | "block_and_comment" | "file_prerequisite_task" | "block_on_existing_task" | "<novel-action-string>",
  "reasoning": "<one paragraph; first sentence MUST quote the verbatim stderr line — see §Step 1>",
  "hint": "<conditional — retry_with_hint only>",
  "new_scope": "<conditional — reissue only; full rewritten issue body>",
  "title": "<conditional — file_prerequisite_task only>",
  "body": "<conditional — file_prerequisite_task only>",
  "block_labels": ["<conditional — file_prerequisite_task only>"],
  "blocker_issue_number": 42
}
```

Field rules (unchanged from #3032):

- `action` (required) — one of the eight known strings, OR a novel action string.
- `reasoning` (required) — single paragraph (≤500 chars). **First sentence MUST quote the verbatim stderr line** (in backticks or double-quotes), per §Step 1.
- `hint` (conditional) — required when `action='retry_with_hint'`.
- `new_scope` (conditional) — required when `action='reissue'`. **Wholesale body rewrite** — preserve `## Goal`, `## Scope`, `## Acceptance criteria`, `## Priority`, `## References` + any `Parent: #N` / `Blocked by #N` lines.
- `title` + `body` (conditional) — required when `action='file_prerequisite_task'`. Title is conventional-commits style.
- `block_labels` (optional) — for `file_prerequisite_task`.
- `blocker_issue_number` (conditional) — required when `action='block_on_existing_task'`. Positive integer.

Exit 0 when all three writes (recommendation + directive + actions_taken) are persisted. The daemon writes `status='completed'` after consuming the directive — the SKILL does not. Exit non-zero on hard failure — the daemon marks the diagnosis `status='failed'` and falls back to mechanical escalation.

---

## Step-by-step procedure

1. **Set up.** Write `{worktree}/tmp/dispatcher-diagnoser/{read_context,write_recommendation,write_directive,log_action}.py` helpers. Run the reader to pull the JSONB context into memory.

2. **Parse the failure signature FIRST (§Step 1, mandatory).** Quote the verbatim stderr line.

3. **Classify.** Identify `failure_category` and `tier`. Read `prior_mechanical_fix` (tier 2) or `ci_log_url` (tier 3). Now scan `prior_diagnoses_this_issue` + `recent_fleet_decisions` for patterns. If a pattern conflicts with your verbatim quote, trust the quote.

4. **Decide.** Walk the decision tree. Pick the action shape and the directive shape (respawn_at vs terminal).

5. **Act.** Execute side effects (commit, push, gh issue create/edit, etc.) if needed. Log each one via `log_action.py` to `actions_taken`. Or skip side effects entirely and let the daemon's recommendation consumer handle it.

6. **Write recommendation + directive.** Run `write_recommendation.py` and `write_directive.py`. Both update the same row. `status` stays `'pending'` — the daemon's reaper picks it up on the next tick and writes `'completed'` after applying the directive.

7. **Exit 0.** Done. The daemon's next supervisor tick consumes the recommendation; its directive consumer reads `next_directive` and either spawns a new agent-runner with `START_PHASE` or frees the slot.

---

## Examples

### Example 1 — anthropic-floor reconciliation, fix it directly

```text
context.failure_category = "ralph_not_ship" (route_stub terminal)
context.details.route_hint = "ralph_not_ship: anthropic SDK version floor mismatch between sibling packages"

Action: read both pyproject.toml files, pick the higher floor, edit, commit, push.
Recommendation: {"action": "retry", "reasoning": "The stderr ends with `\"AC#1 unmet: ralph cannot satisfy floor reconciliation while siblings disagree\"` ..."}
next_directive: "respawn_at=push_and_pr"
actions_taken: [
  {"type": "bash_run", "cmd": "grep anthropic packages/*/pyproject.toml", ...},
  {"type": "git_commit", "sha": "abc123", "message": "fix(deps): align anthropic floor"},
  {"type": "git_push", "branch": "worktree-agent-XYZ", "remote": "origin"}
]
```

### Example 2 — fleet-wide PAT-scope cascade, file a prerequisite

Training example — `#99001` is a synthetic placeholder. In a real diagnosis, file the issue and use the returned number.

```text
context.failure_category = "push_failed"
verbatim stderr: "remote: refusing to allow a Personal Access Token..."
context.recent_fleet_decisions: 3 prior block_on_existing_task on the same blocker issue.

Recommendation: {"action": "file_prerequisite_task", "title": "...", "body": "...", "reasoning": "..."}
next_directive: "terminal"
actions_taken: [
  {"type": "gh_issue_create", "issue_number": 99001, ...},
  {"type": "gh_issue_edit", "issue_number": 3297, "labels_added": ["status/blocked"]}
]
```

### Example 3 — AC infeasible, recommend reissue, fall back to daemon-driven

```text
context.failure_category = "ralph_ac_infeasible"
context.infeasible_acs: [{"index": 2, "evidence": "..."}]

Action: think only — no direct gh writes (the daemon's `_consume_action_reissue` will write the new body via `gh issue edit --body-file`). Compose `new_scope` carefully.
Recommendation: {"action": "reissue", "new_scope": "<full rewritten body>", "reasoning": "..."}
next_directive: "respawn_at=planning"  (let a fresh plan→ralph run against the new body)
actions_taken: []  (no direct side effects this run)
```

### Example 4 — uncertain stderr, escalate cleanly

```text
context.failure_category = "subprocess_crash"
verbatim stderr: empty (just progress output).

Recommendation: {"action": "escalate", "reasoning": "stderr_tail had no FAILED: line — only progress output."}
next_directive: "terminal"
actions_taken: []  (let the daemon's escalate consumer add labels + post the comment)
```

(Or do the gh writes yourself and log them in `actions_taken` — both paths are valid; just be consistent.)

### Example 5 — conflict_unresolvable, semantic collision, escalate

```text
context.failure_category = "conflict_unresolvable"
context.details.resolution_notes = "function X was rewritten on main; agent's call no longer type-checks"

Recommendation: {"action": "escalate", "reasoning": "..."}
next_directive: "terminal"
actions_taken: []
```

### Example 6 — conflict_unresolvable, routine parallel-edit, fix it directly

```text
context.failure_category = "conflict_unresolvable"
context.details.conflict_files = ["packages/web/lock.json"]
context.details.resolution_notes = "main landed a sibling lockfile bump; agent's diff is otherwise clean"

Action: checkout, regenerate lockfile, commit, push.
Recommendation: {"action": "retry", "reasoning": "..."}
next_directive: "respawn_at=push_and_pr"
actions_taken: [
  {"type": "bash_run", "cmd": "git rebase origin/main", ...},
  {"type": "bash_run", "cmd": "npm install --no-save", ...},
  {"type": "git_commit", "sha": "...", "message": "chore(deps): regenerate lockfile post-rebase"},
  {"type": "git_push", ...}
]
```

---

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules. Write helper scripts to `{worktree}/tmp/dispatcher-diagnoser/` first, then invoke them.
- All temp files go under `{worktree}/tmp/`, never `/tmp/`.
- Bright lines are hook-enforced (`JUDGEMIND_DIAGNOSER_RUN=1` env). Do not work around a block — escalate instead.
- `actions_taken` is mandatory for every side-effect action.
- `next_directive` is mandatory before exit (use `terminal` if no respawn is needed). NULL means "I crashed", which falls back to escalate + a `diagnoser_did_not_complete` log event.
- §Step 1 (verbatim stderr quote in reasoning) is mandatory.
- Exit 0 means "directive + recommendation + audit log written". Exit non-zero means "I could not diagnose" — the daemon falls back to fixed mechanical escalation.
- **Do not set `status='completed'`** in `write_recommendation.py`. That column is owned by the daemon's `_mark_diagnosis_completed` method (issue #3422).
