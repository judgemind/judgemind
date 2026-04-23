# `gh` to GitHub MCP migration — audit table

> **Purpose:** concrete mapping of every `gh` subcommand currently used in agent-facing skills and docs to the corresponding `mcp__github__*` tool (or a noted gap). Companion to `docs/agent/github-api-access.md`, which explains the decision rules; this document is the exhaustive inventory.

## Scope of the audit

- **In scope:** every `gh` invocation inside agent-facing markdown — the SKILL.md files under `.claude/skills/` (task, dispatcher, ralph, audit, spotcheck, file-issue), the root `CLAUDE.md`, and the per-topic references under `docs/agent/`.
- **Out of scope:** shell scripts under `scripts/`, GitHub Actions workflows under `.github/workflows/`, `.githooks/`, and `gh run watch` / `gh auth` / `gh config` (no MCP equivalents). See `docs/agent/github-api-access.md` §Decision rule for the per-situation split.

## Tool-by-tool mapping

| `gh` subcommand | MCP equivalent | Status | Notes |
|---|---|---|---|
| `gh issue view <N> --json ...` | `mcp__github__get_issue` | **Available** | MCP returns the full issue object without `--json` enumeration. To get comments too, call `mcp__github__get_issue` then paginate via `mcp__github__list_issues` or GitHub REST if needed (MCP's `get_issue` does not embed comments). |
| `gh issue list --label agent/ready --state open` | `mcp__github__list_issues` | **Available** | Supports `labels`, `state`, `sort`, `direction`, `since`. No `--assignee` filter — filter client-side on the `assignees` field. |
| `gh issue create --body-file ...` | `mcp__github__create_issue` | **Available (write — currently blocked, see below)** | Body passed as native `body` string; no tmp-file needed. |
| `gh issue comment <N> --body-file ...` | `mcp__github__add_issue_comment` | **Available (write — currently blocked)** | |
| `gh issue edit <N> --add-label X` / `--remove-label X` | `mcp__github__update_issue` with `labels: [...]` | **Partial (write — currently blocked)** | MCP only supports full label replacement, not incremental add/remove. Agents must read current labels via `get_issue`, compute the new list, then pass the full set to `update_issue`. Keep `gh issue edit --add-label` when a single-call incremental add is simpler. |
| `gh issue edit <N> --add-assignee @me` | `mcp__github__update_issue` with `assignees: [...]` | **Partial (write — currently blocked)** | Same pattern as labels: full replacement only. `@me` resolution is client-side — pass the literal login (e.g. `drewthaler`). |
| `gh issue close <N> --reason completed` | `mcp__github__update_issue` with `state: "closed"` | **Partial (write — currently blocked)** | MCP does **not** expose `state_reason` — cannot set `completed` vs `not_planned` vs `duplicate`. Keep `gh issue close --reason` when the distinction matters (it does for investigation tasks: see `/task` B.2). |
| `gh pr list --state open --json ...` | `mcp__github__list_pull_requests` | **Available** | |
| `gh pr view <N> --json ...` | `mcp__github__get_pull_request` | **Available** | |
| `gh pr view <N> --json statusCheckRollup` / `gh pr checks <N>` | `mcp__github__get_pull_request_status` | **Available** | Returns combined status. |
| `gh pr view <N> --json files` | `mcp__github__get_pull_request_files` | **Available** | |
| `gh pr view <N> --json reviewDecision,reviews` | `mcp__github__get_pull_request_reviews` | **Available** | |
| `gh pr create --body-file ... --base main` | `mcp__github__create_pull_request` | **Available (write — currently blocked)** | Body as native string. |
| `gh pr edit <N> --body-file ...` | *(no direct MCP equivalent for PR body edit)* | **Gap** | MCP has no `update_pull_request`. Keep `gh pr edit`. |
| `gh pr merge <N> --squash --delete-branch` | `mcp__github__merge_pull_request` | **Partial (write — currently blocked)** | MCP supports `merge_method` but has **no flag for `--delete-branch`**. Keep `gh pr merge --squash --delete-branch` for the typical merge-and-cleanup flow. |
| `gh pr diff <N>` | *(no direct MCP equivalent)* | **Gap** | Use `get_pull_request_files` to list changes; for raw unified diff, keep `gh pr diff`. |
| `gh pr review <N> --approve` / `--request-changes` / `--comment` | `mcp__github__create_pull_request_review` | **Available (write — currently blocked)** | |
| `gh run list --workflow X.yml --branch main --limit 1` | *(no MCP equivalent)* | **Gap — stays on `gh`** | MCP has no workflow-runs API exposure. |
| `gh run watch <run-id> --interval 60 --exit-status` | *(no MCP equivalent)* | **Gap — stays on `gh`** | MCP has no long-poll watcher. Explicitly documented as out of scope. |
| `gh run view <run-id> --json jobs` | *(no MCP equivalent)* | **Gap — stays on `gh`** | |
| `gh api rate_limit` | *(no MCP equivalent)* | **Gap — stays on `gh`** | |
| `gh auth status` / `gh auth token` | *(no MCP equivalent)* | **Gap — stays on `gh`** | |
| `gh repo view` / `gh label list` / `gh secret ...` | *(no MCP equivalent)* | **Gap — stays on `gh`** | |

## Write-path status — read this before migrating

As of this PR, the `github` MCP server is configured at `local` scope (`claude mcp add github -s local -- npx -y @modelcontextprotocol/server-github`) but **with no `GITHUB_PERSONAL_ACCESS_TOKEN` environment variable**. Consequences, verified live from a `/task` subagent on 2026-04-18:

- **Reads work** against public repos (judgemind/judgemind is public). `mcp__github__get_issue`, `list_issues`, `get_pull_request`, `get_pull_request_status`, etc. all return full payloads.
- **All writes fail** with `MCP error -32603: Authentication Failed: Requires authentication`. Confirmed against `add_issue_comment`, `update_issue`, and `create_issue`.

Until the MCP server is given a token, agents can only use MCP for reads. Writes must stay on `gh`. **Do not mass-migrate write calls in this PR** — they will break every agent.

Follow-up issue is filed (referenced in this PR's description) to add the token and restart the CLI. Once that lands, write calls can be migrated incrementally in a later PR.

## Migration plan (two phases)

### Phase 1 (this PR) — MCP-first for reads, docs establish the direction

1. Add `docs/agent/github-api-access.md` with the decision rule: **prefer MCP for reads, keep `gh` for writes (temporary until token lands) and for all operations in the "Gap" rows above.**
2. Update skills so that `gh issue view --json` and `gh pr view --json` reads are replaced with `mcp__github__get_issue` / `get_pull_request` references. `gh issue list` and `gh pr list` become `mcp__github__list_issues` / `list_pull_requests`.
3. Leave write calls (comment, edit, create, close, merge) on `gh` with a pointer to the follow-up issue that will unblock MCP writes. Mark them explicitly "MCP write path blocked on <follow-up #>".
4. Update `CLAUDE.md` to point at the new doc rather than duplicating the decision rule.

### Phase 2 (follow-up PR, after MCP token lands)

1. Replace `gh issue comment --body-file` with `mcp__github__add_issue_comment` — this is the biggest ergonomic win (kills the "write body to tmp file first" preamble for all comment posting).
2. Replace `gh issue create --body-file` with `mcp__github__create_issue`.
3. Replace `gh pr create --body-file` with `mcp__github__create_pull_request`.
4. Replace `gh issue edit --add-label X` with the read-current-labels-then-`update_issue` pattern, but only where it is simpler than the current call. Some incremental edits stay on `gh`.
5. Leave `gh pr merge --squash --delete-branch`, `gh pr edit --body-file`, `gh issue close --reason`, and everything in the Gap rows on `gh`.

## Verification

After this PR:

```
grep -rnE 'gh (issue|pr) (view|list)' .claude/skills/ CLAUDE.md docs/agent/
```

Should show no results except in `docs/agent/gh-to-mcp-migration.md` (this file) and `docs/agent/github-api-access.md` — those intentionally contain the old patterns as examples.

Write operations (`gh issue create`, `gh issue comment`, `gh pr create`, `gh pr merge`, `gh issue close`, `gh pr edit`, `gh issue edit`) still appear in skills — that is the intended Phase 1 state.
