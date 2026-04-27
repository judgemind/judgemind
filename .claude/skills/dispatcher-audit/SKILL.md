---
description: Periodic dispatcher operational-health probes — runs convergence-regression, bad-outcome-streak, and site-health checks and files GitHub issues for any findings. Triggered by the daemon scheduler every 6 hours.
argument-hint: agent_id
maxTurns: 60
---

# /dispatcher-audit skill

Run the three operational-health probes, collect findings, and file GitHub issues for anything actionable. This skill is non-interactive — it runs probes and files issues without LLM review of probe output.

**Trigger:** The dispatcher daemon fires this skill every 6 hours via the `dispatcher-audit` row in `dispatcher.scheduled_skills`.

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Setup

Establish the output directory for this run. The worktree root is the repo root.

Confirm the tmp directory exists:

```
ls tmp/
```

Create the output directory for this audit run:

```
mkdir -p tmp/dispatcher-audit
```

---

## Step 1 — Run probes

Run the audit_probes script and write findings to the output path:

```
python scripts/dispatcher/audit_probes.py --output tmp/dispatcher-audit/findings.json
```

The script reads `DATABASE_URL` from the environment (injected by the ECS task definition) and runs three probes:

1. **convergence_regression** — compares p95 ralph-iteration counts between the recent 6-hour window and the 6h–24h baseline. Flags when recent p95 > 2x baseline p95.
2. **bad_outcome_streak** — counts consecutive non-shipped rows in `dispatcher.terminal_outcomes` ordered newest-first. Flags when streak >= 3 (below the circuit-breaker default of 5).
3. **site_health** — checks HTTP 200 from `https://dev.judgemind.org/` and `https://dev.api.judgemind.org/graphql`, and verifies `derived.documents` row count is non-zero.

On success the script writes a JSON array of findings to `--output` and exits 0. On DB error it exits non-zero — let the failure propagate.

---

## Step 2 — Read findings

Read the findings file:

```
cat tmp/dispatcher-audit/findings.json
```

Parse the JSON array. Each entry has the shape:

```json
{
  "probe": "convergence_regression",
  "title": "...",
  "body": "...",
  "severity": "warning",
  "should_file_issue": true,
  "details": {}
}
```

If the array is empty (no findings), skip to Step 4.

---

## Step 3 — File issues for actionable findings

For each finding where `should_file_issue` is `true`:

Write the issue body to a temp file. Use the Write tool with content derived from the finding's `body` field. File path: `tmp/dispatcher-audit/issue-body-PROBE.txt` (replace `PROBE` with the finding's `probe` value, e.g. `convergence_regression`).

### Sub-step 3.0 — Check for existing open issue (MANDATORY dedup gate)

Before filing a new issue, extract the stable probe-identifier prefix from `finding.title` (e.g. `[bad-outcome-streak]`, `[convergence-regression]`, `[site-health-frontend]`, `[site-health-graphql]`, `[site-health-doc-count]`). Search for an existing open issue with that prefix:

```
gh issue list \
    --repo judgemind/judgemind \
    --label source/dispatcher-audit \
    --state open \
    --search "[PROBE_PREFIX] in:title" \
    --json number,title
```

Replace `[PROBE_PREFIX]` with the bracketed prefix from the finding's title (e.g. `[bad-outcome-streak]`).

**Branch on result:**

- **If one or more open issues match** (dedup hit): pick the lowest-numbered match and add a comment instead of filing a new issue. Skip `gh issue create`.

  ```
  gh issue comment ISSUE_NUMBER \
      --repo judgemind/judgemind \
      --body-file tmp/dispatcher-audit/issue-body-PROBE.txt
  ```

  Record the comment URL. Do **not** run `gh issue create` for this finding.

- **If no match** (no existing open issue): proceed with `gh issue create` as normal (sub-step 3.1 below).

### Sub-step 3.1 — Create new issue (only when no existing open issue found)

```
gh issue create \
    --repo judgemind/judgemind \
    --title "FINDING_TITLE" \
    --label "source/dispatcher-audit" \
    --label "area/devops" \
    --label "type/bug" \
    --label "priority/p2" \
    --body-file tmp/dispatcher-audit/issue-body-PROBE.txt
```

Replace `FINDING_TITLE` with the finding's `title` field.

**Important:** Never use priority/p0. The maximum priority for dispatcher-audit findings is p2.

Repeat sub-steps 3.0–3.1 for each finding with `should_file_issue=true`. Record the issue URL or comment URL from each action.

---

## Step 4 — Return verdict

Emit a summary so the dispatcher's `handle_scheduled_skill` function records success. Output exactly this envelope with the actual values substituted:

```
FILED ISSUES

Dispatcher audit complete. Probes run: convergence_regression, bad_outcome_streak, site_health.
Findings: N total, M filed as GitHub issues.
Filed N new, commented on M existing.

Filed issues: (list issue URLs, or "none" if 0 filed)
Commented on existing issues: (list comment URLs, or "none" if 0 existing)
```

The `FILED ISSUES` header (or any non-empty output) signals success to `handle_scheduled_skill` (agent-runner-entrypoint.sh line ~2082).

---

## Guardrails

- **No `$()` in shell commands.** Retrieve dynamic values as separate tool calls and substitute manually.
- **No heredocs, no quoted strings with `&&` or `;`.** Use Write tool for multi-line content.
- **All temp files go in `{worktree}/tmp/dispatcher-audit/`.** Never `/tmp/`.
- **If audit_probes.py exits non-zero, do not proceed to Steps 3-4.** Let the scheduler record the failure and retry.
- **Never file p0 issues.** The audit probe is a health-check, not an emergency responder.
- **Never modify audit_probes.py output.** The probe functions are authoritative; this skill is a delivery wrapper only.
