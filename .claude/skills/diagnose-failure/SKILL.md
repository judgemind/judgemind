---
description: Diagnose a dispatcher-v3 task-runner failure. Reads the agent's session transcript from S3 (compact), falling back to raw jsonl or CloudWatch Logs. Takes side effects directly with full peer-agent authority — close issue, file follow-up, comment, edit labels, or re-add `agent/ready` to retry. Writes `agents.outcome_summary` and exits.
argument-hint: "<agent_id>"
model: opus
---

# /diagnose-failure skill — v3 empowered diagnoser (issue #3874)

The diagnoser is the v3 launcher's **failure authority**. When a `task-runner` ECS task exits non-zero (or is killed by silent-hang detection), the launcher spawns a `diagnoser` ECS task that runs `claude -p "/diagnose-failure $AGENT_ID"`. The diagnoser reads the failed agent's full session transcript, decides what to do, **takes side effects directly**, writes a one-line `agents.outcome_summary`, and exits. The launcher polls `dispatcher.agents.status` after the diagnoser exits — it does not consume any structured "recommendation" return value.

**This is the v3 contract.** The v2 diagnoses ledger, the legacy 9-action recommendation enum, the legacy 3-state directive column, and the `_consume_action_*` daemon methods are not part of v3. The diagnoser is an **authority, not an advisor** — there is no orchestrator to advise.

Spec references (the dispatcher-v3 spec lands under `docs/specs/` alongside this SKILL): §4.2 (diagnoser contract), §4.1 (launcher invocation + per-issue claim budget), §5 (state model — five tables), §12 (multi-runner future).

## Cohabitation with v2 (transitional, see #3874)

v2 keeps invoking this skill via `claude -p "/diagnose-failure <numeric-diagnosis_id>"` from `scripts/dispatcher/daemon.py::_spawn_diagnoser_subprocess`. This SKILL.md is now v3-shaped. **The deliberate decision (per #3874) is to accept v2 diagnoser degradation during cohab** rather than carry a dual-mode SKILL with conflicting contracts.

What this means in practice:

- v2's daemon will spawn this SKILL with a numeric diagnosis-id positional arg + the legacy diagnoser sentinel env var. The SKILL will read the agent context as best it can from the v3-shaped sources (S3 transcript, agent row, issue/PR via `gh`) — the v2 diagnosis-id arg is ignored, and no rows are written to the legacy v2 diagnoses ledger. The v2 daemon's diagnosis-consumer will see a NULL recommendation after the SKILL exits and the 90-min `DIAGNOSER_SUBPROCESS_TIMEOUT_SECONDS` fallback kicks in — the v2 diagnoses status advances to `failed` via the v2 daemon's mark-failed path, which already routes to mechanical escalate (the same fallback path used for any diagnoser crash).
- The v2 daemon's mechanical-escalate fallback is the safety net. Failures still get human attention via Telegram + `status/needs-human`; the loss is fidelity (the LLM-authored escalation prose), not safety.
- This degradation is bounded by the v2→v3 ramp schedule (spec §9). Once v2 reaches cap=0 and is retired, the cohab path disappears.
- An alternative considered: env-var-gated dual-mode SKILL that swaps to legacy v2 logic when the v2 sentinel is set, else runs v3 logic. Rejected because it doubles the contract surface, both branches drift, and the legacy v2 contract is being deliberately deleted (spec §7 — the legacy recommendation-action enum, the directive column, and the `_consume_action_*` daemon methods are all going away).

If the v2 cohab degradation produces operator pain in practice, the path forward is to land #3882 (v3 launcher invocation path) faster, not to re-introduce a dual-mode SKILL.

---

## Inputs — env, args, and DB state

The launcher spawns this skill with one positional argument and one env var:

```
claude -p "/diagnose-failure $AGENT_ID"
```

- **Argument:** `AGENT_ID` — UUID of the failed agent's `dispatcher.agents` row.
- **Env:** `AGENT_ID` (also exported by the v3 task-runner entrypoint), `DATABASE_URL` (RDS connection string for `dispatcher.*`), `SESSIONS_BUCKET` (S3 bucket for archived transcripts), `AWS_REGION`.

### What to read

1. **The agent's row** — current state of the failed agent.
   ```sql
   SELECT agent_id, issue_number, task_arn, status, exit_code,
          exit_reason, pr_number, outcome_summary, current_milestone,
          current_milestone_detail, current_milestone_at, started_at, ended_at,
          parent_run_id
   FROM dispatcher.agents
   WHERE agent_id = $AGENT_ID;
   ```
   Key fields:
   - `status` is typically `failed` at this point (the launcher wrote it before spawning you). `exit_code` and `exit_reason` give the proximate cause (`silent_hang`, ECS `stoppedReason`, or the runner's own non-zero exit).
   - `pr_number` may be set (if the agent got far enough to push) — fetch the PR for current state.
   - `current_milestone` is best-effort progress observation (`planning`, `ralph`, `summary`, `push_and_pr`, `awaiting_ci`, `fix_ci`, `merge`, `awaiting_deploy`, `verify`, `retro`); use it to anchor your transcript scan.

2. **Cross-attempt history on the same issue** — was this a recurring failure or a first attempt?
   ```sql
   SELECT agent_id, status, exit_code, exit_reason, outcome_summary,
          started_at, ended_at, pr_number
   FROM dispatcher.agents
   WHERE issue_number = $CURRENT_ISSUE_NUMBER
     AND agent_id <> $AGENT_ID
   ORDER BY started_at DESC
   LIMIT 10;
   ```
   The per-issue claim budget (default `claim_attempts_max=3`) means the launcher will skip future claims of this issue once `count(agents) >= 3`. If you re-add `agent/ready` and the count is at 3, the launcher's claim step will instead set `status/needs-human` + Telegram-alert (see spec §4.1). Treat that as a hard cap on retry — beyond it, the issue needs human triage.

3. **The session transcript** — what `/task` actually did, every tool call, every reviewer message. **Stderr is ground truth; prior decisions are priors, not evidence** (see §Step 1 below).

   The diagnoser's read cascade matches `dispatcher-v3-spec.md` §4.1's two-capture design:

   - **Primary: compact rendered transcript at `s3://$SESSIONS_BUCKET/<agent_id>.txt`.** The task-runner's EXIT trap runs `judgemind-transcripts/render-transcript.py` on the raw stream-json before uploading both files. The compact form is ~20:1 compressed (e.g. 41.9MB → 2.2MB) and is the diagnoser's default read.
   - **Fallback: raw stream-json at `s3://$SESSIONS_BUCKET/<agent_id>.jsonl`.** Used when the compact transcript wasn't produced (rare — means the EXIT trap hit a render error after the upload preamble).
   - **Last resort: CloudWatch Logs `GetLogEvents`** against the task's log stream. Used when the EXIT trap was bypassed entirely (SIGKILL/OOM/spot-reclaim). CloudWatch's per-event 256 KB and per-batch 1 MB limits can drop large tool-result events under burst, so it is lossy under burst — but it is the only survivor of EXIT-trap bypass. The log stream name follows ECS's `/ecs/judgemind-task-runner-dev/<task-id>` pattern; resolve the task-id from `dispatcher.agents.task_arn`.

   Read the cascade in order. Stop at the first source that returns a usable transcript.

4. **Issue body, comments, and PR** — the session is a snapshot at exit time; fetch current state via `gh` because the issue/PR may have changed since the agent started (operator may have edited the AC, another PR may have closed it, etc.).
   ```
   gh issue view <issue_number> --repo judgemind/judgemind --json number,title,body,labels,assignees,state,comments
   gh pr view <pr_number> --repo judgemind/judgemind --json number,state,mergeable,statusCheckRollup,body --jq '...'
   ```

---

## Step 1 — Parse the failure signature FIRST (anchor-bias defense — #3057)

**This is a mandatory first step.** Before you read prior agent attempts on the same issue, fleet-wide failure patterns, or any other pattern-bearing context, find the **actual failure signature in the session transcript** and quote it verbatim.

**The rule: stderr is ground truth; prior decisions are priors, not evidence.**

### Procedure

1. Scan the session transcript from the **bottom up** and extract the last concrete failure line. "Concrete" means one of:
   - A line starting with `FAILED:` (pre-push hook convention).
   - A line containing `[remote rejected]` or `! [rejected]` (git push output).
   - A line starting with `error:` or `fatal:` (git / ruff / pytest conventions).
   - An explicit banner like `Push aborted.`, `check(s) failed.`, `tests failed`, `coverage dropped`, `floor violation`.
   - For pytest: the last `FAILED packages/... ::test_name` line from the summary.
   - For CI log: the last `##[error]` line or the last non-zero-exit line.
   - For ralph: the last `BLOCKED:` reason from the worker / reviewer transcripts.
   - For silent-hang exits: there is no failure line — the launcher killed the task because the session-log `lastEventTimestamp` stopped advancing for `silent_hang_minutes`. In that case quote the last meaningful tool call or stdout line you can find, and note `exit_reason='silent_hang'`.

2. **Quote that line verbatim** in your `outcome_summary` and in any escalation comment you post. Use backticks or double-quotes to set the quoted text apart. Do not paraphrase, do not summarize, do not compress. Copy-paste the exact characters.

3. **Only then** consult cross-attempt history and fleet patterns. Treat them as priors — useful for detecting fleet-wide spates, dangerous as substitutes for reading the transcript.

### When the transcript has no identifiable failure line

Say so verbatim in your `outcome_summary`, then default to the **mark needs_review** action (§Step 3 — Action 4). The launcher's `status='needs_review'` path triggers the Telegram alert; an operator can take it from there.

### Why this step exists — the #3057 anchor-bias regression

On 2026-04-23 the diagnoser hallucinated a PAT-scope cascade push-rejection on a coverage-floor failure because the recent fleet decisions were dense with PAT-cascade diagnoses on different issues. The actual stderr ended with `"FAILED: coverage floor"` + `"coverage dropped 80.0% -> 68.6%"` and the push never reached the remote. The verbatim-quote requirement closes that failure mode by forcing the LLM to ground its classification in the actual transcript before consulting pattern-bearing context.

The fleet-history cap remains tunable in v3 via `dispatcher.config.diagnoser_fleet_decisions_cap` (default 3). Larger windows raise anchor-bias risk; smaller windows lose useful pattern signal.

---

## Step 2 — Classify and decide

Walk the decision tree. **Default to taking the action, not filing it.** The v3 diagnoser has the same authority surface as a `/task` agent or the operator's interactive dispatcher session. When a failure is **tractable** (the fix is mechanical and bounded — a small patch, a missing log line, a duplicate to close, a stale row to clean up), the diagnoser MUST do the work directly rather than escalating.

### "Default to taking the action, not filing it" rule

Examples of tractable actions the diagnoser SHOULD do inline:

- **Instrumentation patch:** the worker's stdout/stderr was not captured; write the 3-line patch, open a PR, watch CI, merge. Then re-add `agent/ready` so the next attempt has full diagnostic output.
- **Cleanup query:** a stale or duplicate DB row is blocking a pipeline step; write and run the targeted `UPDATE` / `DELETE` via `scripts/dev-db-query.sh`, confirm the row is gone, re-add `agent/ready`.
- **Sibling-fix port:** the same bug was fixed in a sibling scraper last week; port the fix, open a PR, merge, re-add `agent/ready`.
- **Missing diagnostic field:** a failure has ambiguous root cause because a key field isn't logged; add the field to the log statement, open a PR, merge, re-add `agent/ready`. Do NOT wait to see if the next run fails before acting.
- **Duplicate PR consolidation:** when N>1 open PRs all carry `Closes #<issue>` (operator invariant violated), close the duplicates inline. See Example 4 below for the detection + selection pattern.

**Reserved for needs_review (the diagnoser must NOT act unilaterally):**

- PAT / secret rotation (auth tokens, API keys, AWS credentials).
- Product or spec decisions (should this feature exist, should this UI work this way).
- Infrastructure outside agent reach (Fargate task-def changes requiring human approval, DNS changes, CDN config).
- Destructive operations without a safe preview (DROP TABLE, mass DELETE without query-first verification, irreversible S3 mutations).

**The closing rule:** A failure pattern that says "instrument this and the answer becomes obvious" is NEVER `needs_review`.

### "Chains, not blockers" rule

When the work is tractable but larger than ~30 minutes (multi-file refactor, non-trivial new feature, data migration), prefer **filing a prerequisite task** over marking `needs_review`:

- Write the prerequisite as a `type/task` issue with `agent/ready` and enough scope that an agent can pick it up immediately.
- Append `Blocked by #<new>` to the failed agent's issue body via `gh issue edit --body-file`.
- Add `status/blocked` to the failed agent's issue, remove `agent/ready` (the auto-unblock workflow will re-add it when the prerequisite PR merges with `Closes #<new>`).

Never set `status/needs-human` on the failed agent's issue just because the fix requires a multi-step chain. `needs-human` is reserved for the four operator-only domains above — it is not a synonym for "complicated" or "uncertain."

### The four authoritative actions

There are four, not nine. The v3 diagnoser **does not** emit a structured recommendation-action — there is no orchestrator to consume it. Each action is a sequence of side effects you take directly via `gh` / `git` / `scripts/dev-db-query.sh`, followed by an `outcome_summary` write, followed by exit.

#### Action 1 — Retry (re-add `agent/ready`)

When the failure looks transient (network blip, Claude API 5xx/timeout, rate-limit pause, ECS spot-reclaim, single-shot CI flake) **and** the per-issue claim budget has headroom (`count(agents WHERE issue_number = X) < claim_attempts_max`):

```
gh issue edit <N> --repo judgemind/judgemind --add-label agent/ready
```

If `status/blocked` was added by an earlier attempt and the blocker is resolved, also remove `status/blocked`. Use `scripts/unblock-issue.sh <N>` rather than bare `gh issue edit --remove-label status/blocked` — it prunes stale closed-blocker lines and runs the all-blockers gate.

The launcher's next 30s tick scans `agent/ready` and claims the issue. The retry loop is just "re-add the label" — there is no per-phase resume directive, no state surgery. `/task` runs end-to-end from `planning` again.

#### Action 2 — File follow-up issue + mark agent failed

When the failure points to a specific bug or missing prerequisite that needs its own ticket — `/task` exhausted internal retries on something fixable but distinct from the original AC:

1. Write the follow-up body to `{worktree}/tmp/diagnoser/followup_body.md` (Parent: #<original-issue>, concrete repro from the transcript, suggested fix sketch).
2. Run `gh issue create --repo judgemind/judgemind --title "<conventional-commits-title>" --body-file {worktree}/tmp/diagnoser/followup_body.md --label "agent/ready,<area>,<priority>"`. Capture the new issue number.
3. Append `Blocked by #<new>` to the original issue body (fetch with `gh issue view --json body`, edit, push back with `gh issue edit --body-file`).
4. Run `gh issue edit <original> --repo judgemind/judgemind --add-label status/blocked --remove-label agent/ready`.
5. Post a short comment on the original issue explaining the block and naming the prerequisite.
6. The agent stays at `status='failed'` (already set by the launcher) — no DB write needed beyond the `outcome_summary` write at end of run. The auto-unblock workflow will restore `agent/ready` when the prerequisite PR merges with `Closes #<new>`.

#### Action 3 — Reissue the AC + retry

When the failure reveals that the issue's acceptance criteria are infeasible as written (renamed field, dropped feature, AC describes a state that has since shipped, AC and reality have drifted) but the underlying **intent** is still valid:

1. Read the current issue body. Compose a wholesale-replacement body that fixes the AC. The new body is the **complete rewritten issue body** — preserve `## Goal`, `## Scope`, `## Acceptance criteria`, `## Priority`, `## References`, and any `Parent: #N` / `Blocked by #N` lines. Do NOT post a diff or a partial splice — `gh issue edit --body-file` does no parsing.
2. Write the new body to `{worktree}/tmp/diagnoser/reissue_body.md`.
3. Run `gh issue edit <N> --repo judgemind/judgemind --body-file {worktree}/tmp/diagnoser/reissue_body.md`.
4. Post a comment naming the AC change (`AC #2 was infeasible as written — the field was renamed in #3204; the AC has been rewritten to reference the new name.`).
5. Re-add `agent/ready` (subject to claim budget).

The launcher's next tick claims the issue with the rewritten body. `/task` runs end-to-end against the new AC.

`new_scope` (the AC-rewrite term used in v2) was always required to be a **wholesale replace, not a partial splice**. v3 keeps that requirement — `gh issue edit --body-file` doesn't parse content, so any partial body truncates the issue.

#### Action 4 — Mark `needs_review`

When the failure is structurally unclear (the transcript shows no FAILED line, the AC isn't infeasible but the failure mode is novel, the fix requires an operator-only decision per the four categories above), update the agent row directly:

```sql
UPDATE dispatcher.agents
SET status = 'needs_review'
WHERE agent_id = $AGENT_ID;
```

The launcher detects `status='needs_review'` and Telegram-alerts the operator. Also add `status/needs-human` + `priority/p1` to the issue and post the escalation comment with the verbatim stderr quote. Do not re-add `agent/ready` — the issue is paused until the operator triages.

---

## Step 3 — Execute, then write `agents.outcome_summary` and exit

The diagnoser does the work directly via `gh` / `git` / `aws` / `scripts/dev-db-query.sh` — there is no recommendation/directive return. The launcher polls `dispatcher.agents.status` after the diagnoser exits and proceeds based on the row state.

### Final write — `agents.outcome_summary`

Before exit, write a one-line human-readable narrative to `dispatcher.agents.outcome_summary`. The cockpit reads this column to render "what happened to this agent" in the admin UI. **First sentence MUST quote the verbatim stderr line** when one is identifiable (same rule as Step 1).

```python
# {worktree}/tmp/diagnoser/write_outcome.py
import os, sys
import psycopg

agent_id = sys.argv[1]
summary = sys.argv[2]
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE dispatcher.agents "
            "SET outcome_summary = %s WHERE agent_id = %s",
            (summary, agent_id),
        )
    conn.commit()
```

Run it as the last step:

```
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "$OUTCOME"
```

The summary is a single line, ≤500 chars, present tense. Examples:

- `"FAILED: coverage floor" — coverage dropped 80.0% to 68.6% on packages/api after the alerts refactor; re-added agent/ready, the next attempt will see the failing assertion in the prior_attempts.md context.`
- `"remote: refusing to allow a Personal Access Token to create or update workflow" — filed prerequisite #4012 to bump the agent PAT scope; this issue blocked-on #4012.`
- `silent_hang at ralph_iter_3 — task-runner stopped emitting log events for 31 min during reviewer subagent. Marked needs_review; transcript ends mid-Bash call to gh run watch.`

### No recommendation/directive return — the launcher polls `agents.status`

There is **no** structured recommendation JSONB field, no directive TEXT field, no actions-taken audit array, no v2-style diagnoses row to UPDATE. The diagnoser SKILL is contract-side-effects-then-exit. The launcher's next 30s tick reads `dispatcher.agents` and acts on `status` — `failed` (with `agent/ready` re-added → claim again on next tick), `needs_review` (Telegram + skip), `succeeded` (rare from the diagnoser path, possible if you closed the issue as already-shipped), or unchanged `failed` with no `agent/ready` (terminal — the issue is parked at the per-issue claim budget cap or you intentionally left it that way).

The diagnoser's exit code is informational only — exit 0 if you took an action, exit non-zero if you crashed. The launcher's failure-of-the-diagnoser path (spec §4.2 — cap is 1 diagnoser per agent_id) marks the agent `status='needs_review'` and Telegram-alerts; there is no recursion into "diagnose the diagnoser."

### Bright lines — irreducible (hook-enforced when the env var is set)

These are policy, not parsimony — the same lines that bound `/task` agents and the operator-laptop preflight. v2's diagnoser-sentinel env var was the legacy enforcement mechanism; v3's task-runner role policy enforces the same constraints at the IAM layer (the diagnoser task role excludes the prod-account assume-role) plus the PreToolUse hook checks any of these bright lines that fire shell-side:

1. **No production deploy.** `terraform apply` against `environments/production/`, ECS service writes against `*-production` clusters. Human-only per `CLAUDE.md`.
2. **No PAT rotation / `gh auth switch`.** Operator-only.
3. **No force-push to `main`, no amending merged commits.** Destructive across all agents.
4. **No recursive `/diagnose-failure` invocation.** Depth-1 cap. The launcher caps at 1 diagnoser per agent_id; the SKILL must not spawn another diagnoser sub-skill.

If a hook blocks a command, do NOT try to work around it. Default to **mark needs_review** instead.

---

## Capabilities — same as a peer agent

You can use the full toolset of a `/task` agent or the operator's dispatcher session:

- **Bash** — `git`, `gh`, `aws`, `psql` (via `scripts/dev-db-query.sh`), any other shell command. Set `timeout: 1200000` on long-running commands per `CLAUDE.md`.
- **Edit / Write / Read / Glob / Grep** — full filesystem access. Edit files in the failed agent's worktree (when present), write helpers to `{worktree}/tmp/diagnoser/`, read PR diffs, etc. Note: the v3 diagnoser runs in its own ECS task; it does not have the failed agent's worktree on disk by default. Use `git fetch origin pull/<PR>/head:adopt-<PR>` to materialize the agent's branch when you need to inspect or patch it.
- **Agent (sub-skill invocation)** — call `/ralph`, `/audit`, etc. when judgment requires. Sub-skills run with their own normal contracts.
- **MCP servers** — `github`, `awslabs_cloudwatch-mcp-server`, `awslabs_ecs-mcp-server`, `plugin:telegram` (read/notify only — see Telegram Integration in `CLAUDE.md`).
- **gh / git / aws CLI** — full operator-tier authority. You may commit and push to the failed agent's branch, file new issues, edit issue/PR bodies, add/remove labels, post comments (via `scripts/gh-comment-with-retry.sh` for `--body-file` posts so the 504-after-success failure mode #4478 is handled transparently), close issues. AWS reads (CloudWatch logs, ECS describe-tasks) plus same writes the launcher already has on the dev account.

---

## Examples

### Example 1 — coverage-floor regression, retry with context

**Failure context.** Agent on issue #3500 exited non-zero from the pre-push hook. Transcript ends with:

```
FAILED: coverage floor
coverage dropped 80.0% -> 68.6% on packages/api
```

The agent's PR was never opened (push never reached remote). No prior attempts on this issue. No fleet-wide pattern.

**Action.** Re-add `agent/ready`. The next attempt's `prior_attempts.md` will surface the coverage-floor failure as context, and the worker will see "the previous attempt landed under-covered" and write a test for the regression. Do NOT file a follow-up — the issue is the same; the fix is "the next agent reads the prior failure and writes more tests."

```
gh issue edit 3500 --repo judgemind/judgemind --add-label agent/ready
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "\"FAILED: coverage floor\" — coverage dropped 80.0% to 68.6% on packages/api before push. Re-added agent/ready; next attempt's prior_attempts.md will surface the regression."
```

### Example 2 — PAT-scope cascade, file prerequisite + block

**Failure context.** Agent on issue #3297 hit:

```
remote: refusing to allow a Personal Access Token to create or update workflow `.github/workflows/foo.yml`
```

Three prior fleet decisions all blocked-on the same PAT issue (#4012). The current agent's failure is an instance of the same root cause.

**Action.** Append `Blocked by #4012` to issue #3297, add `status/blocked`, post the explanatory comment via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478):

```
gh issue view 3297 --repo judgemind/judgemind --json body --jq .body > {worktree}/tmp/diagnoser/orig_body.md
# Append "Blocked by #4012" to a copy
python3 {worktree}/tmp/diagnoser/append_blocked_by.py 4012 < {worktree}/tmp/diagnoser/orig_body.md > {worktree}/tmp/diagnoser/new_body.md
gh issue edit 3297 --repo judgemind/judgemind --body-file {worktree}/tmp/diagnoser/new_body.md
gh issue edit 3297 --repo judgemind/judgemind --add-label status/blocked --remove-label agent/ready
{worktree}/scripts/gh-comment-with-retry.sh 3297 --body-file {worktree}/tmp/diagnoser/block_comment.md
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "\"remote: refusing to allow a Personal Access Token to create or update workflow\" — same PAT-scope cascade as #4012. Blocked-on #4012; will auto-unblock when that PR merges."
```

### Example 3 — AC infeasible as written, reissue + retry

**Failure context.** Agent on issue #3601 exited with ralph block_reason `AC#1 unmet: field 'transcribed_text' does not exist on the Document type`. The field was renamed to `transcription_html` in #3204 (merged 3 weeks ago). The AC was authored before the rename.

**Action.** Rewrite the issue body — replace `transcribed_text` with `transcription_html` throughout — and re-add `agent/ready`.

```
gh issue view 3601 --repo judgemind/judgemind --json body --jq .body > {worktree}/tmp/diagnoser/orig_body.md
python3 {worktree}/tmp/diagnoser/rewrite_field_name.py {worktree}/tmp/diagnoser/orig_body.md transcribed_text transcription_html > {worktree}/tmp/diagnoser/reissue_body.md
gh issue edit 3601 --repo judgemind/judgemind --body-file {worktree}/tmp/diagnoser/reissue_body.md
gh issue comment 3601 --repo judgemind/judgemind --body "AC #1 referenced 'transcribed_text', which was renamed to 'transcription_html' in #3204. The AC has been rewritten to use the current field name. Re-adding agent/ready."
gh issue edit 3601 --repo judgemind/judgemind --add-label agent/ready
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "\"AC#1 unmet: field 'transcribed_text' does not exist on Document\" — the field was renamed to transcription_html in #3204. Reissued the AC and re-added agent/ready."
```

(Note: the `gh issue comment ... --body "..."` line above passes a short inline body, not `--body-file`, so the 504-after-success wrapper isn't needed here. The wrapper is required for `--body-file` posts; short inline bodies are unaffected by the failure mode.)

### Example 4 — duplicate-PR consolidation (#3725)

**Failure context.** Agent on issue #3601 exited from the duplicate-PR check at `/task` Step 4a — two open PRs (#3628 and #3650) both carry `Closes #3601` in their body. The operator invariant "one PR per issue" is violated. This pattern triggers the duplicate-PR consolidation regardless of the proximate failure category.

**Detection (one gh search):**

```
gh pr list --repo judgemind/judgemind --state open \
  --search "in:body Closes #3601" \
  --json number,headRefName,updatedAt,statusCheckRollup
```

Returns rows for #3628 and #3650 (N=2).

**Selection — pick the canonical PR to keep:**

1. Most recent push: compare `updatedAt` across rows; pick max.
2. Tie-break: fewest CI failures (count `statusCheckRollup` entries with `conclusion=FAILURE` per PR).
3. Tie-break: largest diff: `gh pr diff <N> --patch | wc -l` (higher line count wins).

In this example: chosen = #3650; duplicates = [#3628].

**Action — close duplicates inline (one Bash call per duplicate):**

```
gh pr close 3628 --repo judgemind/judgemind --comment "Closing as duplicate of #3650" --delete-branch
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "Detected N=2 open PRs with Closes #3601 — #3628, #3650. Closed #3628 as duplicate of #3650 (most recent push). Daemon CI-watch proceeds on #3650. action_taken=consolidate_duplicate_prs"
```

Issue label state: leave `status/in-progress` in place. The launcher's CI-watch / merge logic on #3650 takes over from here. No `agent/ready` toggling needed — #3650 is already the active PR for this issue.

### Example 5 — silent-hang with no failure line, mark needs_review

**Failure context.** Agent on issue #3700 exited with `exit_reason='silent_hang'` after 31 minutes of no log growth during `ralph_iter_3`. Transcript ends mid-`Bash` tool call to `gh run watch`. No FAILED line, no error banner, no exception. Two prior attempts on this issue both hit silent-hang at the same milestone.

**Action.** Three silent-hangs at the same milestone is a recurring pattern that needs human triage — it might be a CI rate-limit cascade, a `gh` CLI bug, or a network egress issue. Don't retry blindly; mark `needs_review`.

```sql
UPDATE dispatcher.agents
SET status = 'needs_review'
WHERE agent_id = $AGENT_ID;
```

```
gh issue edit 3700 --repo judgemind/judgemind --add-label status/needs-human,priority/p1
{worktree}/scripts/gh-comment-with-retry.sh 3700 --body-file {worktree}/tmp/diagnoser/escalation.md
python3 {worktree}/tmp/diagnoser/write_outcome.py "$AGENT_ID" "silent_hang at ralph_iter_3 — task-runner stopped emitting log events for 31 min during reviewer subagent. Third silent-hang at the same milestone on this issue. Marked needs_review; transcript ends mid-Bash call to 'gh run watch'."
```

The launcher detects `status='needs_review'` on its next tick and Telegram-alerts.

### Example 6 — empowered diagnoser walks #3694 (#3744)

**Failure context.** Agent on issue #3694 has silently failed three times. Each run exits with `exit_reason='silent_hang'`. The ECS task completes (or is killed by silent-hang detection) but the scraper worker's stdout and stderr were never captured — the launcher only sees the exit code. Without those streams, no diagnoser can determine root cause.

**WRONG path (pre-#3744 thinking):**

1. Diagnoser reads the three failed rows.
2. Diagnoser concludes: "I can't diagnose without the worker output."
3. Diagnoser marks `needs_review` and posts a comment: "Need operator to investigate ECS logging."
4. Issue sits in `needs-human` queue for days.

**RIGHT path (post-#3744 empowered behavior):**

1. Diagnoser reads the three failed rows.
2. Diagnoser identifies the gap: worker stdout/stderr not captured by the task-runner entrypoint.
3. **Diagnoser acts:** opens a worktree, writes a small patch adding `stdout=result.stdout, stderr=result.stderr` capture to the task-runner subprocess launch (or whichever ECS launch wrapper is responsible for subprocess capture).
4. Opens a PR, watches CI (`gh run watch`), merges.
5. Re-adds `agent/ready` on issue #3694 — the next dispatch will have full diagnostic output.
6. Writes `outcome_summary`: `"silent_hang × 3 with no captured worker output — root cause was missing stdout/stderr capture in task-runner entrypoint. Shipped instrumentation patch in PR #4040; re-added agent/ready on #3694 so the next attempt has full diagnostics."`

**Oversize fallback.** If the instrumentation fix is larger than ~30 min (e.g. requires a schema migration to add a `log_text` column), use Action 2 (file follow-up) with a precise task body so the prerequisite can be picked up immediately and the auto-unblock workflow restores `agent/ready` on #3694 when the prerequisite PR merges.

**The invariant.** In neither path does the diagnoser emit `status/needs-human` for issue #3694. That label means "a human must make a judgment call." Deciding where to add a log line is not a judgment call — it is mechanical and bounded.

---

## Reminders

- No `$()`, no heredocs, no inline `python -c`. See `CLAUDE.md` Critical Rules. Write helper scripts to `{worktree}/tmp/diagnoser/` first, then invoke them.
- All temp files go under `{worktree}/tmp/`, never `/tmp/`.
- Bright lines (no prod deploy, no `gh auth switch`, no force-push to `main`, no recursive `/diagnose-failure`) are policy. Default to **mark needs_review** if a hook blocks a command.
- **§Step 1 (verbatim transcript-line quote in `outcome_summary`) is mandatory** when a failure line is identifiable. This is the anchor-bias defense from #3057.
- The diagnoser is **side-effects-then-exit**. No structured recommendation, no directive, no writes to the legacy v2 diagnoses ledger — the launcher polls `agents.status` and acts on it. (Cohabitation note: v2's daemon will see a NULL recommendation after this SKILL exits and fall back to mechanical escalate via its 90-min timeout. Accepted degradation per #3874; see §Cohabitation with v2.)
- The per-issue claim budget (default 3, configurable via `dispatcher.config.claim_attempts_max`) caps retries. If you re-add `agent/ready` and the count is at 3, the launcher's next claim flips the issue to `status/needs-human` instead of claiming.
- Exit 0 means "I took an action and wrote `outcome_summary`." Exit non-zero means "I crashed before finishing" — the launcher marks the agent `status='needs_review'` and Telegram-alerts.
