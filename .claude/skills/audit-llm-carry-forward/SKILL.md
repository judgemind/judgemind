---
description: Weekly LLM carry-forward audit across CA counties — runs scripts/audit_llm_carry_forward.py, applies per-axis thresholds, files agent/ready follow-ups for trips, and posts a heartbeat comment on the long-lived audit-log issue. Triggered by the daemon scheduler weekly.
argument-hint: agent_id
maxTurns: 60
---

# /audit-llm-carry-forward skill

Run the weekly LLM carry-forward audit across every CA county. This skill is non-interactive — it runs probes, applies thresholds, files issues / posts comments, and returns a verdict without LLM review of the probe output.

**Trigger:** The dispatcher daemon fires this skill weekly (Sunday 06:00 UTC) via the `audit-llm-carry-forward` row in `dispatcher.scheduled_skills` (migration 63, issue #4309).

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Setup

Establish the output directory for this run. The worktree root is the repo root.

Create the output directory:

```
mkdir -p tmp/llm-carry-forward
```

### State persistence (issue #4318)

The probe persists per-county totals in two places, in priority order:

1. **`dispatcher.scheduled_skills.last_run_state`** (primary, JSONB column added in migration 64). Survives ECS task restarts so the second-and-later fires see the prior week's totals and the noisy-axis +25% jump checks (`motion_type_contradiction`, `case_title_text_mismatch`) actually fire.

2. **`tmp/llm-carry-forward/last_totals.json`** (development fallback). Local file path written on every successful run so the local file stays a faithful copy of DB state for debugging. Used as the fallback read source when the DB-backed state is unavailable (psycopg missing, migration 64 not applied, DB error). Not committed.

On the very first run after migration 64 lands the DB row's `last_run_state` is NULL — the probe treats NULL the same as a missing local file (first-run mode, noisy-axis jump checks silent). From the second fire onward, the noisy-axis checks fire whenever a county's count jumps > 25% over the prior fire.

---

## Step 1 — Run the probe

Run the probe wrapper, writing the findings envelope to the output path:

```
packages/scraper-framework/.venv/bin/python scripts/dispatcher/llm_carry_forward_probe.py --output tmp/llm-carry-forward/findings.json --state tmp/llm-carry-forward/last_totals.json
```

The script reads `DATABASE_URL` from the environment (injected by the ECS task definition), invokes `audit_llm_carry_forward.run_audit()` over every CA county, applies thresholds, and writes a JSON envelope:

```json
{
  "summary": { ... full run_audit output ... },
  "totals_by_county": {"County": {"axis": count}},
  "findings": [
    {
      "probe": "outcome_continue",
      "county": "...",
      "title": "[llm-carry-forward outcome_continue ...] ...",
      "body": "...",
      "severity": "warning",
      "should_file_issue": true,
      "details": { ... }
    }
  ],
  "comment_markdown": "## Weekly LLM carry-forward audit\n..."
}
```

The probe also reads `dispatcher.scheduled_skills.last_run_state` for the `audit-llm-carry-forward` skill before the audit (jump-detection baseline) and writes the new totals back to that column on success. The `--state` local file path is written too as a development fallback. Override the skill name only for testing — pass `--skill-name=''` to disable the DB-backed state and use only the local file.

On DB error the script exits non-zero — let the failure propagate so the scheduler records the run as failed.

---

## Step 2 — Read the findings envelope

Read the envelope file:

```
cat tmp/llm-carry-forward/findings.json
```

Parse the JSON envelope. The two pieces you act on next:

* `findings` — list of threshold trips. Each entry has `should_file_issue` (always true today; reserved for future heartbeat-only findings).
* `comment_markdown` — pre-rendered weekly heartbeat comment for the long-lived audit-log issue.

If `findings` is empty, skip to Step 4 — there is no follow-up issue to file, just the heartbeat comment.

---

## Step 3 — File a follow-up issue per finding (with dedup)

For each entry in `findings`:

### Sub-step 3.0 — Check for an existing open issue (MANDATORY dedup gate)

Extract the bracketed prefix from the finding's `title` (e.g. `[llm-carry-forward outcome_continue Riverside]`). Search for an existing open issue with that prefix to avoid filing duplicates each week:

```
gh issue list --repo judgemind/judgemind --label source/audit-llm-carry-forward --state open --search "[FINDING_PREFIX] in:title" --json number,title
```

Replace `FINDING_PREFIX` with the bracketed prefix from the finding's title (e.g. `[llm-carry-forward outcome_continue Riverside]`).

**Branch on result:**

* **One or more open issues match** (dedup hit): pick the lowest-numbered match and append a comment with the finding's `body` instead of filing a new issue. Skip `gh issue create`.

  Write the finding's `body` to a temp file:

  ```
  tmp/llm-carry-forward/comment-N.txt
  ```

  (`N` is the finding's index in the `findings` array, 0-based.)

  Then post the comment via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478):

  ```
  scripts/gh-comment-with-retry.sh ISSUE_NUMBER --body-file tmp/llm-carry-forward/comment-N.txt
  ```

  Record the comment URL.

* **No match** (no existing open issue): proceed with `gh issue create` (sub-step 3.1).

### Sub-step 3.1 — Create a new issue (only when no existing open issue found)

Write the finding's `body` to a temp file:

```
tmp/llm-carry-forward/issue-N.txt
```

Then file the issue:

```
gh issue create --repo judgemind/judgemind --title "FINDING_TITLE" --label "source/audit-llm-carry-forward" --label "area/scraping" --label "area/ingestion" --label "type/bug" --label "priority/p2" --label "agent/ready" --body-file tmp/llm-carry-forward/issue-N.txt
```

Replace `FINDING_TITLE` with the finding's `title` field verbatim.

**Important:** Never use `priority/p0`. The maximum priority for this audit is p2.

Record the issue URL.

Repeat sub-steps 3.0–3.1 for every finding in the `findings` array.

---

## Step 4 — Post the weekly heartbeat comment on the audit-log issue

Find the long-lived audit-log issue. It is the open issue labeled `source/audit-llm-carry-forward-log` (singular — there should be exactly one).

```
gh issue list --repo judgemind/judgemind --label source/audit-llm-carry-forward-log --state open --json number --limit 5
```

**Branch on result:**

* **One open issue exists:** post the heartbeat comment as a reply.

  Write the heartbeat to a file (use the `comment_markdown` field from the envelope):

  ```
  tmp/llm-carry-forward/heartbeat.txt
  ```

  Post via `scripts/gh-comment-with-retry.sh` (the wrapper handles the 504-after-success failure mode #4478):

  ```
  scripts/gh-comment-with-retry.sh LOG_ISSUE_NUMBER --body-file tmp/llm-carry-forward/heartbeat.txt
  ```

* **No matching open issue:** create the long-lived audit-log issue once. Subsequent weeks will append comments to it.

  Write the body to:

  ```
  tmp/llm-carry-forward/log-body.txt
  ```

  Body content:

  ```
  ## Long-lived LLM carry-forward audit log

  This issue is the heartbeat target for the `/audit-llm-carry-forward`
  weekly scheduled skill (issue #4309). Each Sunday the skill posts a
  comment with per-county counts. Threshold trips are filed as separate
  `agent/ready` follow-up issues with title prefix
  `[llm-carry-forward …]`.

  Do not close this issue — it stays open as the rolling log. The first
  weekly heartbeat appears below.
  ```

  Then create:

  ```
  gh issue create --repo judgemind/judgemind --title "audit-log: LLM carry-forward weekly audit" --label "source/audit-llm-carry-forward-log" --label "area/scraping" --label "type/operations" --body-file tmp/llm-carry-forward/log-body.txt
  ```

  After creating the log issue, post the heartbeat as a comment on the new issue (same `gh issue comment` recipe as above, with the new issue number).

* **More than one open issue:** treat the lowest-numbered match as canonical, post the heartbeat there, and emit a `WARN` line in your output noting the duplicates so an operator can close the extras.

---

## Step 5 — Return verdict

Emit a summary so the dispatcher's `handle_scheduled_skill` function records success. Output exactly this envelope with the actual values substituted:

```
FILED ISSUES

Weekly LLM carry-forward audit complete.
Findings: N total threshold trips.
Filed N new follow-up issues, commented on M existing follow-ups.
Heartbeat posted on audit-log issue #LOG_ISSUE.

Filed issues: (list issue URLs, or "none" if 0 filed)
Commented on existing follow-ups: (list comment URLs, or "none" if 0)
Heartbeat URL: (URL of the heartbeat comment)
```

The `FILED ISSUES` header (or any non-empty output containing `FILED ISSUES` / `NO ACTIONABLE FINDINGS` / `SHIPPED` / `PASSED`) signals success to `handle_scheduled_skill` (agent-runner-entrypoint.sh ~line 2085).

If there were zero findings AND a heartbeat comment posted, emit:

```
NO ACTIONABLE FINDINGS

Weekly LLM carry-forward audit clean.
All thresholds within expected bounds.
Heartbeat posted on audit-log issue #LOG_ISSUE.

Heartbeat URL: (URL of the heartbeat comment)
```

---

## Guardrails

- **No `$()` in shell commands.** Retrieve dynamic values (issue numbers, URLs) as separate tool calls and substitute manually.
- **No heredocs, no quoted strings with `&&` or `;`.** Use Write tool for multi-line content.
- **All temp files go in `tmp/llm-carry-forward/`.** Never `/tmp/`.
- **If `llm_carry_forward_probe.py` exits non-zero, do not proceed to Steps 3–5.** Let the scheduler record the failure and retry next week.
- **Never file p0 issues.** The maximum priority is p2 — this audit is preventative, not emergency-grade.
- **Never modify probe output.** `llm_carry_forward_probe.py` (and the underlying `audit_llm_carry_forward.py`) are authoritative; this skill is a delivery wrapper only.
