---
description: Periodic codebase health audit — reviews recent PRs, checks for dead code, test gaps, performance issues, security concerns, and dependency health. Files issues for findings. Triggered by the dispatcher every 20 merged PRs.
argument-hint: ""
maxTurns: 200
---

# /audit skill

Perform a comprehensive codebase health audit, filing GitHub issues for every actionable finding. This skill is read-only — it analyzes and reports but never modifies source code.

**Trigger:** The dispatcher spawns `/audit` every 20 merged PRs (tracked via `prs_since_last_audit` in `tmp/dispatcher_status.json`). It can also be invoked manually.

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

---

## Step 2 — Deduplicate findings

Before filing issues, compare each finding against the open issues list fetched in Step 0:

1. For each finding, search `open_issues.json` for issues with similar titles or descriptions.
2. Also search for existing TODO comments in the codebase that reference the same problem (with issue numbers).
3. If a duplicate exists, note it in `findings.md` as "skipped — duplicate of #N" and do not file a new issue.
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

## Issues Filed
- #N — title (severity, category)
- ...
```

---

## Step 5 — Send Telegram notification

Send a summary notification via Telegram:

```
scripts/tg-notify.py notify "Audit complete: N findings, M issues filed. [critical: X, high: Y, medium: Z, low: W]"
```

---

## Step 6 — Clean up

Remove the worktree:

```
scripts/end-worker.sh {worktree}
```

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
