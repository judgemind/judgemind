---
description: File a GitHub issue in judgemind/judgemind with the repo's house conventions locked in — conventional-commits title, body written to a temp file, Verify lines on every acceptance criterion, proper labels, and correct blocking dependencies. Thin guardrail — the full authoring reference lives in docs/agent/issue-authoring.md.
argument-hint: "[one-line summary of the issue]"
---

# /file-issue skill

A guardrail/checklist for filing an issue in `judgemind/judgemind`. This skill does **not** rewrite the issue-authoring docs — the source of truth is `docs/agent/issue-authoring.md`. Use this skill when you are about to create an issue so you don't skip the house-style requirements.

**Why this exists:** agents routinely file issues with inline bodies, vague acceptance criteria, missing `Verify:` lines, or the wrong priority. The pattern is in the docs, but it's easy to forget under time pressure. This skill is the pre-filing checklist.

> **MCP vs `gh`:** issue creation is a write operation. `mcp__github__create_issue` exists and would let the body be passed as a native string (no tmp file), but the MCP server currently has no auth token and all writes fail with `Requires authentication` (verified from a `/task` subagent). Until that is fixed, this skill keeps `gh issue create --body-file`. See `docs/agent/github-api-access.md` for the decision rule and `docs/agent/gh-to-mcp-migration.md` for the full inventory.

---

## Checklist — run through every item before calling `gh issue create`

### 1. Title — conventional commits style

- Format: `type(area): short description`.
  - `type`: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `perf`, or `ci`.
  - `area`: matches an existing `area/*` label when possible (e.g. `scraping`, `ingestion`, `agent`, `validation`, `web`, `api`).
  - Keep under ~70 characters. Details go in the body, not the title.
- Good: `fix(scraping): OC tentatives drops per-department judge name`
- Bad: `OC scraper is broken for some departments and needs fixing urgently`

### 2. Body — write to a file first, never inline

The `gh` CLI mangles multi-line content and `$()` patterns when passed via `-b` / `--body`. Always:

```
Write tool → {worktree}/tmp/issue_body.txt
gh issue create --body-file {worktree}/tmp/issue_body.txt ...
```

The body should contain at minimum:

- **Summary** — 1–3 sentences, what is broken / what needs to exist and why.
- **Acceptance Criteria** — a markdown checklist; each criterion has a `Verify:` line.
- **Context / References** — links to related issues, investigation docs, PRs, or scraper-lesson entries. Use `Parent: #N` for hierarchy and `Blocked by #N` for dependencies (see step 5).
- **Out of Scope** (optional, but useful when the issue is ambiguous) — what this issue explicitly does not cover.
- **Suspected Effort** (optional) — rough LoC budget or "small / medium / large".

### 3. Every acceptance criterion needs a `Verify:` line

This is the single most-violated rule. A criterion without a `Verify:` line is a guess dressed up as an acceptance test. Examples:

- **SQL**: `Verify: SELECT COUNT(*) FROM derived.rulings WHERE judge_name IS NULL AND county = 'Orange' returns 0`
- **HTTP**: `Verify: curl https://dev.api.judgemind.org/graphql ... returns 200 with the new field populated`
- **Grep**: `Verify: grep -n "Evidence-Based Answers" CLAUDE.md returns the new section heading`
- **URL/screenshot**: `Verify: https://dev.judgemind.org/cases/<id> renders the attorney block`

When a criterion is genuinely subjective (visual design, prose quality), say so explicitly: `Verify: reviewer confirms on read-through.`

### 4. Labels — pick the right ones

- **Type** — exactly one of `type/*` (e.g. `type/bug`, `type/feature`, `type/dx`, `type/docs`, `type/investigation`, `type/decision`, `type/refactor`).
- **Area** — one or more `area/*` (e.g. `area/scraping`, `area/ingestion`, `area/agent`, `area/validation`, `area/web`, `area/api`, `area/infra`).
- **Priority** — exactly one of `priority/p1`, `priority/p2`, `priority/p3`. **Never set `priority/p0`** — p0 is human-only. Default DX to p1, features to p3; see `docs/agent/issue-authoring.md` §Priority Framework for the full table.
- **Status** — add `agent/ready` if the issue is fully specified and an agent can pick it up without further human input. Omit (and add `status/blocked` via `scripts/block-issue.sh` — see step 5) if it needs a decision first.

### 5. Blocking dependencies — use the script, not the label

If this issue depends on another issue, never just slap on `status/blocked`. Use the helper:

```
scripts/block-issue.sh <this-issue> <blocker-issue>
```

The script adds `status/blocked` **and** appends a `Blocked by #<blocker>` line to the issue body, which is required for the `unblock-issues` workflow to auto-unblock on merge. A label-only block never unblocks.

`Parent: #N` is hierarchy — use it to group sub-tasks under a larger epic. It is **not** a dependency; a parent can be open while the children are worked.

### 6. Assignee — `drewthaler` for now

Per memory note, the `judgemind-agent` account is temporarily flagged. Until further notice, assign issues and PRs to `drewthaler`:

```
gh issue create ... --assignee drewthaler
```

Revert to `@me` / the agent account when the note is removed from memory.

### 7. External-integration issues need a feasibility note

If the issue proposes to query a third-party website or API (court case-search endpoints, public records APIs, etc.), include a one-line HTTP feasibility note in the body before labeling `agent/ready`:

```
Feasibility: curl https://example.court.gov/api/search?case=123 returns JSON, no reCAPTCHA/WAF, anonymous access works
```

See `docs/agent/issue-authoring.md` for why — #1979 burned ~a day of agent time on an infeasible premise.

### 8. Data cleanup on `derived.*` defaults to rebuild_db.py

Data cleanup tasks on `derived.*` tables should plan to use `rebuild_db.py --county <name>`, not a surgical delete/patch script. Surgical scripts ship with their own bugs and only patch existing rows. Only write a surgical script if (a) rebuild cost is prohibitive at the affected scale, or (b) the deletion is scoped to a subset rebuild can't express — and put a one-line justification in the body when going surgical.

---

## Creation command

Once the checklist is satisfied:

```
gh issue create --repo judgemind/judgemind \
    --title "type(area): short description" \
    --body-file {worktree}/tmp/issue_body.txt \
    --label type/<kind> \
    --label area/<area> \
    --label priority/p<1|2|3> \
    --label agent/ready \
    --assignee drewthaler
```

Capture the returned URL and reference it in any comment, PR, or Telegram reply that mentions the new issue.

---

## See also

- `docs/agent/github-api-access.md` — MCP vs `gh` decision rule and the write-path note.
- `docs/agent/gh-to-mcp-migration.md` — full tool-by-tool inventory.
- `docs/agent/issue-authoring.md` — full authoring reference, priority framework, sub-task mechanics, investigation-task conventions.
- `docs/agent/task-dependencies.md` — blocking/unblocking mechanics, `Blocked by #N` semantics.
- `scripts/block-issue.sh` — the helper that satisfies the Blocked-by contract.
- `CLAUDE.md` §Collaboration & Judgment — Evidence-Based Answers and Root Cause Over Symptoms rules; relevant when framing acceptance criteria.
