---
description: Retrospective phase for the per-phase /task-v2 pipeline. Reads the full agent history (phase_transitions, failures, PR URL), produces zero-to-many retro issue bodies for the daemon to file.
argument-hint: "<agent-id>"
maxTurns: 30
model: haiku
---

# /task-v2-retro skill

Retrospective phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). After a task succeeds end-to-end (verify=VERIFIED or SKIPPED), this phase reviews the run for workflow-efficiency and preventative-measures findings. Produces zero or more retro issue bodies; the daemon files them as GitHub issues.

**Prerequisites:** The dispatcher daemon has already (a) posted the verification-evidence comment from `/task-v2-verify`, (b) pulled the full `phase_transitions` and `failures` history for this agent from `dispatcher.*`, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/retro.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/retro.json` with a list of retro issue bodies (possibly empty) the daemon will file.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation.

**IMPORTANT — Heartbeat lines (issue #3017).** Emit two distinctive heartbeat lines to stdout so CloudWatch Log Insights can answer "what was the last thing the skill did before its stream went silent?" during a hang. Run the Bash tool with:

- `echo PHASE_START retro` immediately after reading this SKILL.md (before Step 1).
- `echo PHASE_DONE <verdict>` right before writing the output JSON (Step N — the last step), where `<verdict>` matches the verdict/go field the output JSON will carry.

These are plain `echo` statements — the dispatcher daemon's stream-forwarder (`scripts/dispatcher/stream_forwarder.py`) picks them up from subprocess stdout and tags them with `agent_id`, `issue_number`, `phase=retro`, `stream=stdout` in CloudWatch + a real-time JSONL mirror at `{worktree}/.dispatcher/retro-<agent_id>.jsonl`. The grep-friendly `PHASE_START` / `PHASE_DONE` tokens make it trivial to filter phase boundaries: `filter @message like /PHASE_START/`.

**IMPORTANT — Do not fabricate findings.** A clean run (ralph=1 iteration, ci_attempts=1, no failures) is supposed to be boring. Setting `no_findings=true` is the correct answer for those runs. Retro issues should be high-signal and actionable — not "process theater".

**IMPORTANT — No GitHub writes.** The daemon files the issues after reading this output. Do not call `gh issue create`.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/retro.json`. Required fields:

- `agent_id` (str).
- `issue_number` (int) — the original task issue.
- `pr_number` (int).
- `phase_transitions` (list of `{phase, started_at, ended_at, duration_s, outcome}`) — full per-phase timing log from `dispatcher.phase_transitions`.
- `failures` (list of `{category, details, first_seen, last_seen, count}`) — `dispatcher.failures` rows for this agent grouped by category.
- `ralph_iterations` (int) — how many worker/reviewer cycles ralph needed.
- `ci_attempts` (int) — how many times CI ran (>1 means we hit failures and retried via fix-ci).
- `fix_ci_attempts` (int) — how many times `/task-v2-fix-ci` ran.
- `total_duration_s` (int) — wall-clock from claim to verify.
- `diff_stats` (object with `files_changed`, `insertions`, `deletions`).
- `worktree_path` (str).
- `repo_root` (str).

Optional:

- `scope_check_followups` (list of str) — out-of-scope findings from plan's `scope_check` that should become follow-up issues.
- `plan_follow_ups` (list of str) — explicit "Follow-ups to file" lines from the plan_text (see `/task-v2-plan` Step 5).

If the file is missing or malformed, exit 0 with `no_findings=true` and an error note — the retro phase missing is not fatal.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/retro.json`:

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "pr_number": <int>,
  "no_findings": true|false,
  "retro_issues": [
    {
      "title": "<conventional-commits title for the fix>",
      "body": "<full markdown issue body with Acceptance criteria + Verify lines>",
      "labels": ["type/dx", "area/<X>", "agent/ready", "priority/p2"],
      "priority": "priority/p1" | "priority/p2" | "priority/p3",
      "blocked_by": [<int>, ...]
    }
  ],
  "notes": "<optional prose summarizing the run for the weekly report>"
}
```

`blocked_by` is used by the daemon to call `scripts/block-issue.sh` if any of the new follow-ups should not go to `agent/ready` until a prerequisite lands.

Exit 0 regardless. Empty `retro_issues` + `no_findings=true` is a valid happy-path result.

---

## Step 1 — Clean-run short-circuit

If ALL of these are true, skip the review entirely and set `no_findings=true`:

- `ralph_iterations == 1`
- `ci_attempts == 1`
- `fix_ci_attempts == 0`
- `failures == []` or `failures == [{category: 'benign_hook', ...}]`
- `scope_check_followups == []`
- `plan_follow_ups == []`

Write the output JSON with empty `retro_issues` and exit. Skipping this short-circuit on a clean run is fabrication.

## Step 2 — Workflow-efficiency review

Look at `phase_transitions`, `failures`, `ralph_iterations`, `fix_ci_attempts`, and `total_duration_s`. Identify actionable friction:

- **Agent work a script could do cheaper.** Mechanical transformations, boilerplate setup, repeated fix-retry cycles that follow a pattern. → file `type/dx`.
- **CI caught something the pre-push hook should have caught.** (A hygiene check that exists in CI but not in `.githooks/pre-push`.) → file `type/dx` to add the check to pre-push. Common symptom: `fix_ci_attempts >= 1` with category `markdown_links`, `lint`, `format`, `hygiene_guard`, `ci_config`.
- **Ralph took more iterations than expected.** `ralph_iterations >= 3` → note what reviewers kept flagging. If a single reviewer flag pattern recurred, the plan probably missed it — file a `type/dx` to sharpen the plan template or the ralph reviewer prompt.
- **Agent hit permission prompts.** Look in `failures` for categories like `permission_denied`, `tool_blocked`. → file `type/dx` to add the pattern to `.claude/settings.json` allow-list (or to `docs/agent/unattended-patterns.md` if it's a known pattern).
- **Phase took significantly longer than the per-phase budget in §6a.** Plan >15 min, verify >20 min, fix-ci >45 min, retro >15 min. → note in `notes` for the weekly report; file an issue only if the slowdown has a structural cause.
- **Agent hit the 529 rate-limit backoff** (`category='rate_limit_529'`). → file an issue to tighten batching or add retry budgeting, only if it happened more than once in this run.

## Step 3 — Preventative-measures review

Look at the merged diff and ask:

- **What would have caught this earlier?** A lint rule, type check, test, CI check, or runtime assertion that would have flagged the bug before it reached human review. → file an area-labeled issue with `type/dx` (if tooling) or `type/bug` (if the test/assertion is missing).
- **Is this a pattern that could recur?** If the fix pattern applies to other scrapers/endpoints/modules, file a single audit-and-fix issue covering all of them. Do not file N identical issues — file one "audit + fix N places" issue. Per `CLAUDE.md` §Collaboration, root cause > symptom.
- **Were there misleading docs/specs?** If the bug was partially caused by a stale doc, out-of-date acceptance criterion template, or missing cross-reference, file a `type/docs` issue.
- **Did this reveal a gap in the agent's workflow rules?** Missing preflight check, unclear CLAUDE.md rule, permission prompt pattern — file a `type/dx` issue and consider whether CLAUDE.md itself should be updated (that goes in the PR body of the resulting fix, not a separate issue).

## Step 4 — Convert scope-check follow-ups into issues

For each entry in `scope_check_followups` and `plan_follow_ups`:

- Read the description.
- Synthesize a concrete acceptance criterion with a `Verify:` line.
- If the follow-up is "audit + fix N sibling locations the original issue didn't cover", file one issue that lists all N sibling locations (do not file N).

## Step 5 — Write retro issue bodies

For each finding, produce a `{title, body, labels, priority, blocked_by}` entry.

### Title format

Conventional-commits format for the fix that would close the issue: `feat(area): add …`, `fix(area): repair …`, `docs(area): clarify …`, `chore(dx): automate …`. Match the repo's existing title style.

### Body template

```
## Problem

<Concrete context pulled from this retro — what happened in the run that surfaced this>

Example: In PR #<PR-N> (issue #<N>), ralph took <X> iterations because the reviewer kept flagging <pattern>. Root cause: <one-sentence>.

## Proposed fix

<Concrete next step an agent can pick up in a single session.>

## Acceptance criteria

- [ ] <criterion>
  Verify: <SQL query / curl response / URL / test file path — whatever can be checked mechanically>

- [ ] <criterion>
  Verify: <…>

## References

- Surfaced by retro of #<N> (PR #<PR-N>)
- Related phase transitions: <list the relevant phases from phase_transitions>
- <any additional links>
```

### Labels

Always include:

- `agent/ready` if the issue is fully specified and pickup-able.
- `status/triage` if the issue needs human review first (use when the finding is exploratory or needs a decision).

One of: `type/dx`, `type/bug`, `type/docs`, `type/feature`, `type/decision`.

One `area/*` label matching the primary affected package (e.g. `area/scraping`, `area/api`, `area/devops`).

Exactly one `priority/*`.

### Priority

Per `CLAUDE.md` §Task Dependencies, §Priority Framework:

- `priority/p1` — prevents future production bugs, eliminates a repeated-failure pattern that costs significant agent time, or catches a correctness issue.
- `priority/p2` — most agent-friction improvements, documentation fixes, nice-to-have consistency.
- `priority/p3` — large slow work (new framework, redesign).
- **Never `priority/p0`** — human-only per `CLAUDE.md`.

If unsure between p1 and p2: p1 if skipping this causes measurable agent-hours of waste over the next month; p2 otherwise.

### Blocking

If a finding depends on another in-flight or newly-filed retro issue, populate `blocked_by` with that issue's number (or `null` placeholder for "to be filed"). The daemon handles `scripts/block-issue.sh` calls — do not attempt that from this skill. For placeholder forward-references, use descriptive text in the body under "References" and leave `blocked_by` empty; the maintainer will link them manually.

## Step 6 — Write the output JSON

Emit `{worktree}/tmp/dispatcher-output/retro.json` with all fields above. Exit 0.

---

## Scope limits

- One finding per issue. Don't bundle.
- Scoped tightly — each issue should be pickup-able in a single `/task` session.
- Do not fabricate findings to seem productive. A clean run → `no_findings=true`.
- Do not file issues about this retro itself (do not file a "retro was slow" issue).
- Do not file issues for intentional prompts (git push, PR create, merge, deploy) per `CLAUDE.md` §Permission prompt workarounds.

## What this skill does NOT do

- **Does not file issues.** Daemon does that after reading this output.
- **Does not comment on the original issue.** The verification-evidence comment from `/task-v2-verify` is the last comment on the original issue.
- **Does not close the original issue.** PR merge auto-closed it.
- **Does not open subsequent PRs or tasks.** Only produces issue bodies; the dispatcher's queue scan eventually picks them up.
- **Does not read historical failures from other agents.** Only this agent's own `failures` rows, provided by the daemon in the input bundle.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Match the body template shape — the daemon's `gh issue create --body-file` call passes the body verbatim, so malformed markdown lands malformed in GitHub.
- Keep each issue body under 3000 characters where possible. Reference this retro (PR + agent_id) rather than duplicating long context.
