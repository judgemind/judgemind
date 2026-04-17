---
description: Periodic codebase health audit — reviews recent PRs, checks for dead code, test gaps, performance issues, security concerns, dependency health, and CI pipeline health. Files issues for findings. Triggered by the dispatcher every 20 merged PRs.
argument-hint: ""
maxTurns: 200
---

# /audit skill

Perform a comprehensive codebase health audit, filing GitHub issues for every actionable finding. This skill is read-only — it analyzes and reports but never modifies source code.

**Trigger:** The dispatcher spawns `/audit` every 20 merged PRs (tracked via `prs_since_last_audit` in `tmp/dispatcher_state.json`). It can also be invoked manually.

**Prerequisites:** Must be in a worktree. No special dependencies required — the audit uses Read, Grep, Glob, and `gh` CLI only.

Do not ask for confirmation. Work autonomously through every step.

---

## Step 0 — Setup

Create a working directory for audit state:

```
{worktree}/tmp/audit/
```

Fetch the list of recently merged PRs (the last 20):

```
gh pr list --repo judgemind/judgemind --state merged --limit 20 \
    --json number,title,headRefName,mergedAt,files,body
```

Also fetch the current list of open issues to avoid filing duplicates:

```
gh issue list --repo judgemind/judgemind --state open \
    --json number,title,body,labels --limit 200
```

Store both results in `{worktree}/tmp/audit/recent_prs.json` and `{worktree}/tmp/audit/open_issues.json` for reference throughout the audit.

---

## Step 1 — Audit Categories

Work through each category in order. For each finding, record it in `{worktree}/tmp/audit/findings.md` with this format:

```markdown
### [Category] Finding title

- **Severity:** critical | high | medium | low
- **Files:** path/to/file.py:42, path/to/other.py:100
- **Description:** What the problem is and why it matters.
- **Suggested fix:** Concrete next steps to resolve it.
- **Issue filed:** #N (or "skipped — duplicate of #N")
```

### 1.1 — Adversarial code review (recent changes)

Review the last 20 merged PRs for real bugs that slipped through review:

1. For each PR, read the diff using `gh pr diff <N> --repo judgemind/judgemind`.
2. Look for:
   - **Logic errors** — off-by-one, wrong comparison operators, inverted conditions, missing null checks.
   - **Race conditions** — shared mutable state without synchronization, TOCTOU patterns.
   - **Missing error handling** — bare `except:`, swallowed exceptions, missing retries for network calls, unhandled edge cases.
   - **Cross-PR interactions** — did PR #X change an interface that PR #Y depends on? Are there broken assumptions?
   - **Regressions** — did a refactor remove functionality or change behavior?
3. Only flag real bugs or significant risks. Do not flag style preferences, minor naming concerns, or theoretical issues that have no practical impact.

### 1.2 — CLAUDE.md hygiene

Read `CLAUDE.md` and cross-reference with the actual codebase:

1. **Contradictions** — rules that conflict with each other or with code in `docs/`.
2. **Outdated instructions** — references to files, paths, tools, or patterns that no longer exist.
3. **Commonly violated rules** — check recent ralph feedback (`tmp/ralph/feedback.md` patterns in recent worktrees, or review-log.jsonl) for repeated issues that suggest a rule is unclear or missing.
4. **Bloat** — sections that could move to `docs/` files loaded on-demand, reducing context window usage.
5. **Missing rules** — patterns that keep causing agent errors but have no corresponding rule.

### 1.3 — Architecture drift

Scan for code and infrastructure that has drifted from its intended design:

1. **Dead code** — functions with zero callers (use Grep to verify), unreachable branches, orphaned files not imported anywhere.
2. **Unused dependencies** — packages listed in `pyproject.toml` or `package.json` that are never imported in source code.
3. **Orphaned infrastructure** — Terraform resources that are no longer referenced by application code or other modules.
4. **Spec drift** — compare specs in `docs/specs/` with actual implementation. Flag significant divergences.
5. **Schema drift** — tables, columns, or indexes in migration files or `schema.sql` that are not used in application code (or vice versa).

### 1.4 — Test quality

Evaluate the test suite beyond what coverage metrics catch:

1. **Untested modules** — source files with no corresponding test file or zero test coverage.
2. **Weak assertions** — tests that assert only truthiness (`assert result`), never check specific values, or mock so aggressively that they test nothing.
3. **Missing regression tests** — recent bug-fix PRs that lack a test reproducing the original bug.
4. **Missing edge cases** — error paths, empty inputs, boundary conditions that are untested.

Focus on modules touched by recent PRs first, then broaden if time permits.

### 1.5 — Performance

Look for common performance anti-patterns:

1. **Sequential I/O** — loops making individual network calls (DB queries, HTTP requests, S3 operations) where batching or concurrency would be appropriate.
2. **N+1 queries** — GraphQL resolvers or API endpoints that issue per-item DB queries instead of batch loading.
3. **LIMIT/OFFSET pagination** — should use keyset/cursor-based pagination for large datasets.
4. **Missing connection reuse** — creating new HTTP clients, DB connections, or S3 clients per request instead of reusing.
5. **Missing indexes** — queries in application code that filter/sort on columns without corresponding DB indexes.

### 1.6 — Security

Check for security issues in the codebase:

1. **Hardcoded secrets** — API keys, passwords, tokens, or connection strings in source code (not env vars).
2. **Missing input validation** — API endpoints that accept user input without validation or sanitization.
3. **SQL injection** — string interpolation in SQL queries instead of parameterized queries.
4. **Overly broad IAM policies** — Terraform IAM policies with `"*"` resources or overly permissive actions.
5. **Dependency vulnerabilities** — check for known CVEs in pinned dependencies.

### 1.7 — Dependency health

Review dependency freshness and hygiene:

1. **Outdated dependencies** — major version bumps available for key dependencies.
2. **Known CVEs** — dependencies with published security advisories.
3. **Unused dev dependencies** — dev-only packages that are never imported in test files.
4. **Version conflicts** — different packages pinning incompatible versions of the same dependency.

### 1.8 — CI health

Monitor CI pipeline performance to detect slow jobs before they bottleneck agent throughput. Every `/task` agent blocks on `gh run watch` during the PR cycle, so slow CI directly impacts overall velocity.

**Use the helper script — do not compute trend means by hand.** `scripts/audit_ci_health.py` implements the correct methodology, avoiding the common pitfalls described below. The script exits 0 with no findings or 1 with findings; use it both during the audit and to verify any hand-filed CI perf issue before escalating.

```
scripts/audit_ci_health.py            # sample last 10 CI runs, print summary + findings
scripts/audit_ci_health.py --limit 20 # larger sample window
scripts/audit_ci_health.py --json     # machine-readable output for issue bodies
```

#### Data collection

1. Fetch the last 10 successful CI runs on `main`:

```
gh run list --repo judgemind/judgemind --branch main \
    --status success --workflow ci.yml --limit 10 \
    --json databaseId,createdAt,updatedAt
```

2. For each run, fetch job-level timing:

```
gh run view <id> --repo judgemind/judgemind --json jobs
```

3. For each job, compute:
   - **Job duration** — `completedAt - startedAt` for each job.
   - **Total wall clock** — earliest non-skipped `startedAt` → latest non-skipped `completedAt`.

#### Threshold checks

Flag a finding if any of the following are true:

- **Single job exceeds 10 minutes.** Any individual non-skipped job taking longer than 10 minutes is a threshold violation.
- **Total wall clock exceeds 15 minutes.** The end-to-end CI time from first job start to final completion exceeds 15 minutes.
- **Upward trend detected.** Split the ran-samples into halves (oldest vs. newest) and compute mean duration. Flag if **both** criteria are met:
  - mean increased by at least **20%** between halves, AND
  - the absolute increase is at least **15 seconds** (prevents false positives on trivially short jobs going 3s → 4s).

#### Pitfalls — do not make the following mistakes

- **Do NOT treat skipped jobs as duration = 0.** GitHub path filters (`dorny/paths-filter`) skip conditional jobs such as `ingestion-tests`, `scraper-framework-tests`, and `scraper-registry-check` when the PR did not modify matching paths. If skipped runs are averaged in as zeros, the sample mean for a window that happens to contain many skipped runs will be artificially low — producing massive false-positive "trend regressions" the next time a window contains mostly ran samples. This was the root cause of issue #2401, where the audit reported +145% regressions for jobs whose actual duration had been flat for weeks. **Exclude skipped runs entirely from per-job trend computation.**
- **Do NOT report trend regressions with fewer than 3 ran-samples in each half.** Tiny samples produce noisy means; the `audit_ci_health.py` script enforces a minimum of 3 samples per half before emitting a trend finding.
- **Do NOT rely on percentage alone for small-duration jobs.** A 3-second check going to 4 seconds is +33% but not an actionable regression. The script requires BOTH a ≥20% delta AND a ≥15-second absolute delta.

#### Filing issues

For each threshold violation or trend regression, file a `priority/p1` `type/dx` issue with:

- Which job(s) are slow and their current duration (mean and max over the sampled runs, **counting only runs where the job executed**).
- Historical comparison — what the duration was in the older half vs. the recent half, with sample sizes.
- The specific run IDs and timestamps so the issue is traceable. Paste the output of `scripts/audit_ci_health.py --json` into the issue body.
- Suggested investigation steps: check for new heavy test files, fixture bloat, missing parallelism, runner size, or unnecessary sequential steps.

**Deduplication:** Before filing, check for existing open issues related to CI performance (e.g., #1243 tracks CI runner size / test splitting). If an existing issue covers the same job or concern, note it as "skipped — duplicate of #N" rather than filing a new one. Only file a new issue if the finding is distinct from existing tracked work.

### 1.9 — Scripts directory hygiene

Monitor the `scripts/` directory for one-off script accumulation. After #2095 archived 55 scripts, the directory was cleaned to ~29 `.py` files. This check prevents re-accumulation.

#### Checks

1. **Script count threshold — self-adjusting based on `# permanent: true` markers.** The threshold is computed from the marker counts, not a fixed literal, so adding a new permanent utility automatically raises the ceiling while adding a new one-off consumes a slot of headroom. Get the current marker counts:

```
scripts/check-script-headers.py --count
```

This emits JSON with four keys: `total` (candidate scripts, excluding exempt/archive), `permanent` (scripts carrying `# permanent: true`), `one_off` (scripts carrying `# one-off: true`), and `unmarked` (no marker). The enforcement check (§1.9 check 2 below) guarantees `unmarked == 0` on the repo baseline, so in normal operation `total == permanent + one_off`.

Compute the threshold as:

```
threshold = permanent + HEADROOM   # HEADROOM = 5
```

`HEADROOM = 5` absorbs legitimately-blocked one-off scripts (e.g., cleanup scripts waiting on upstream issues, patch scripts awaiting a library fix) without triggering a false-positive audit finding. When blockers clear and the one-offs archive, the count drifts back toward `permanent` and headroom is restored.

**Flag a finding when `total > threshold`.** Include the current `permanent`, `one_off`, `total`, and computed threshold in the issue body, plus the list of unarchived `# one-off: true` scripts (these are the archival candidates — permanent utilities are intentionally at baseline).

The formula is self-adjusting: a new permanent utility landing bumps `permanent`, which bumps `threshold`, so the next audit does not raise a false-positive ratchet issue (see #2547 for the rationale). Conversely, a new one-off script consumes a headroom slot — if too many one-offs accumulate without being archived, the check correctly flags them.

2. **Missing `# one-off: true` or `# permanent: true` headers.** Use the machine-verifiable check — do not eyeball line numbers:

```
scripts/check-script-headers.sh   # exit 0 = all good, exit 1 = missing markers
```

The script scans **every** top-level `scripts/*.py` (excluding the check script itself and the `archive`/`eval`/`tests`/`spotcheck` subdirectories) and requires EITHER `# one-off: true` OR `# permanent: true` anywhere in the first 50 lines (the script's header comment block — the marker sits adjacent to the `# venv:` header, typically just before or after the module docstring). The 50-line window replaces the old "first 10 lines" rule-of-thumb that routinely flagged correctly-marked scripts whose docstrings pushed the marker to line 15, 20, or 32 (see #2533 for the historical context).

Historical note: the original (#2533) convention only required a marker on scripts whose filename matched a set of name fragments (`backfill`, `cleanup`, `fix`, `dedup`, `merge`, `migrate`, `remediat`). #2547 extended the requirement to ALL top-level scripts so check 1 above can use `permanent_count + HEADROOM` as a self-adjusting threshold — a new permanent utility raises the threshold automatically, while a new one-off consumes a slot of headroom. The narrow (#2533) behaviour is still available via `scripts/check-script-headers.py --narrow` for callers that want the historical scan.

A script should carry exactly one marker:
   - `# one-off: true` — finite-lifetime script (tied to a specific bug fix or migration). Candidate for archival once its work is done.
   - `# permanent: true` — re-runnable utility (parameterizable, idempotent, intended to be invoked repeatedly). Exempt from one-off nagging and staleness checks.

Scripts previously confirmed as permanent in issue comments should carry the canonical `# permanent: true` marker so the check is machine-readable and does not re-flag them each audit cycle (see #2530). The audit treats either marker as sufficient — only unmarked scripts are flagged.

The same check runs in CI as the `script-headers-check` job and in `.githooks/pre-push`, so the repo baseline should always be green; audit findings here should be rare and typically represent newly-added unmarked scripts.

3. **Stale one-off scripts.** For scripts that DO have `# one-off: true`, check their git log to see when they were last modified. If a one-off script has not been modified in more than 30 days, flag it as a candidate for archiving to `scripts/archive/`. Scripts with `# permanent: true` are exempt from this staleness check. When reading the marker, use the same 50-line window as check 2 — a simple `grep -n "^# one-off: true" scripts/*.py` suffices.

#### Filing issues

For script count threshold violations, file a `priority/p2` `type/chore` issue with:
- Current `total`, `permanent`, `one_off` counts and the computed threshold (`permanent + 5`).
- List of scripts carrying `# one-off: true` — these are the archival candidates. Permanent utilities are at baseline by design; they should NOT be listed as candidates.
- Suggested action: archive completed one-off scripts to `scripts/archive/`.

For missing headers, file a single `priority/p3` `type/dx` issue listing the scripts that should be reviewed. Include the verbatim output of `scripts/check-script-headers.sh` in the body so the fix is mechanical. Each listed script should get either `# one-off: true` (if finite-lifetime) or `# permanent: true` (if re-runnable utility).

---

## Step 2 — Deduplicate findings

**Do not rely on the Step 0 snapshot.** The open issues list fetched at the start of the audit may be stale — other agents may have closed or modified issues during Step 1, which can take 30+ minutes. Re-fetch the current state before cross-referencing.

### 2.1 — Re-fetch open issues

Re-run the same query from Step 0 to get a fresh list of open issues:

```
gh issue list --repo judgemind/judgemind --state open \
    --json number,title,body,labels --limit 200
```

Overwrite `{worktree}/tmp/audit/open_issues.json` with the fresh data.

### 2.2 — Verify individual issue state before classifying as duplicate

Before marking any finding as "skipped — duplicate of #N" based on a matching issue #N, verify that #N is still open:

```
gh issue view <N> --repo judgemind/judgemind --json state -q '.state'
```

If the issue has been closed since the list was fetched, treat the finding as new instead. Do not reference closed issues as duplicates — the fix may have already shipped, or the issue may have been closed as stale.

### 2.3 — Compare findings against refreshed issue list

1. For each finding, search the refreshed `open_issues.json` for issues with similar titles or descriptions.
2. Also search for existing TODO comments in the codebase that reference the same problem (with issue numbers).
3. If a duplicate exists (and is verified as still open per 2.2), note it in `findings.md` as "skipped — duplicate of #N" and do not file a new issue.
4. If a finding is related to but not a duplicate of an existing issue, note the related issue in the new issue body.

---

## Step 3 — File issues

For each non-duplicate finding:

1. Determine the appropriate labels:
   - **Type:** `type/bug` (logic errors, regressions), `type/dx` (CLAUDE.md hygiene, workflow), `type/security` (security findings), `type/perf` (performance), `type/chore` (dependency updates, dead code removal), `type/test` (test quality)
   - **Area:** `area/scraping`, `area/api`, `area/web`, `area/infra`, etc. based on affected files
   - **Priority:** `priority/p1` for critical/high severity, `priority/p2` for medium/low severity. **Never set `priority/p0`.**
2. Add `agent/ready` if the issue is fully specified and actionable without human input.
3. Write the issue body to `{worktree}/tmp/audit/issue_N.txt`, then create it:

```
gh issue create --repo judgemind/judgemind \
    --title "..." \
    --label "..." \
    --body-file {worktree}/tmp/audit/issue_N.txt
```

Each issue body should include:
- **Found by:** `/audit` skill (periodic codebase health check)
- **Category:** which audit category found it
- **Description:** clear explanation of the problem
- **Affected files:** specific file paths and line numbers
- **Suggested fix:** concrete steps to resolve
- **Related PRs:** if found during adversarial review, link the PR that introduced the issue

---

## Step 4 — Write summary report

Write a comprehensive summary to `{worktree}/tmp/audit/report.md`:

```markdown
# Audit Report — YYYY-MM-DD

## Summary
- PRs reviewed: N
- Findings: N total (N critical, N high, N medium, N low)
- Issues filed: N
- Duplicates skipped: N

## Findings by Category

### 1. Adversarial Code Review
[List findings or "No issues found"]

### 2. CLAUDE.md Hygiene
[List findings or "No issues found"]

### 3. Architecture Drift
[List findings or "No issues found"]

### 4. Test Quality
[List findings or "No issues found"]

### 5. Performance
[List findings or "No issues found"]

### 6. Security
[List findings or "No issues found"]

### 7. Dependency Health
[List findings or "No issues found"]

### 8. CI Health
[List findings or "No issues found"]

### 9. Scripts Directory Hygiene
[List findings or "No issues found"]
```

---

## Step 5 — Notify completion

The dispatcher will send a Telegram notification when the audit agent completes. No explicit notification step is needed — the report in `{worktree}/tmp/audit/report.md` and the filed issues are the deliverables.

---

## Step 6 — Clean up

Worktree cleanup is handled automatically by Claude Code when the agent exits.

---

## What NOT to do

- **Do not fix anything directly.** The audit skill is read-only. Only file issues.
- **Do not flag style nits or subjective preferences.** Only file issues for concrete, actionable problems.
- **Do not re-file issues that already exist.** Always check the open issues list and TODO comments first.
- **Do not file issues for things tracked by existing TODO comments with issue numbers.**
- **Do not set `priority/p0`.** That priority is reserved for humans.
- **Do not modify any source files, configs, or infrastructure.** Read and report only.

---

## Guardrails

- **Time budget:** The audit should complete within a single agent session. If a category is taking too long, summarize what you found so far and move on.
- **Signal over noise:** Fewer high-quality findings are better than many low-quality ones. Only file issues that would save time or prevent bugs.
- **Err toward filing:** If you are unsure whether something is a real issue, file it with medium severity. The maintainer can close it if it is not actionable.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
