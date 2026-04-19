# GitHub API access — MCP vs `gh` CLI

> **When to read this:** you are writing or editing a skill, agent doc, or CLAUDE.md section that interacts with GitHub, and need to choose between a `mcp__github__*` MCP tool and the `gh` CLI.
>
> **TL;DR:** MCP for **single-object reads** (one issue, one PR, one file) — typed output, no jq, no shell quoting. For **lists** (`list_issues`, `list_pull_requests`) and **wide rollups** (status + check runs), prefer `gh --json <tight field list>` — it's 3–5× smaller than the MCP payload and sidesteps the MCP token ceiling, which a busy queue can exceed. Keep `gh` for writes (temporarily — see the write-path note below), for anything in the Gap rows of `docs/agent/gh-to-mcp-migration.md`, and for shell scripts where MCP is not reachable.

## Why this doc exists

Agents historically used `gh` for every GitHub operation, which forced a handful of annoying patterns:

- Multi-line content needed a `--body-file` preamble — write to a tmp file, then invoke `gh`.
- Reads required `--json field1,field2,...` enumeration and often `-q .[0].databaseId` jq juggling.
- The combination of shell quoting and `-q` mixed-quote rules tripped the platform's safety checks (see `docs/agent/unattended-patterns.md` §`gh` jq Quoting).

The `github` MCP server exposes the REST API directly as structured tool calls — body is a native string, the response is parsed JSON, no shell escaping. **But MCP returns the full server-defined payload.** For narrow single-object reads that's a win (the extra fields are rarely wasted). For lists and wide rollups, a narrow `gh --json <tight field list>` is often 3–5× smaller and strictly better on token budget.

## Decision rule

| Situation | Use | Why |
|---|---|---|
| Structured read of a single issue, PR, comment list, file contents | **MCP** (`get_issue`, `get_pull_request`, `get_pull_request_comments`, `get_file_contents`, etc.) | No `--json` enumeration, no `-q` jq. Typed object, reasonable payload size. |
| Listing issues or PRs with filters (labels, state, assignee) | **`gh` with narrow `--json`** (`gh issue list --json number,title,labels,assignees,createdAt --label agent/ready --limit 50`) | MCP `list_issues` returns the full issue payload (body, reactions, etc.) per item — a busy `agent/ready` queue can exceed 150K chars / the MCP token ceiling. The narrow `gh --json` shape is ~50K for the same queue. Use MCP `list_issues` only for small, filtered queries (single assignee, small label set). |
| PR merge-readiness (`mergeable`, `mergeStateStatus`) | **MCP** (`get_pull_request`) | Single-object read. |
| PR **full CI rollup** (Actions check runs, not just legacy commit statuses) | **`gh pr view <N> --json statusCheckRollup,mergeable,mergeStateStatus`** | MCP `get_pull_request_status` only covers legacy commit statuses (Vercel deploy etc.), not GitHub Actions check runs. Do not rely on it to gate a merge. |
| PR file list or review history | **MCP** (`get_pull_request_files`, `get_pull_request_reviews`) | Single-object scoped reads. |
| Search across the repo (issues, code, commits) | **MCP** (`search_issues`, `search_code`, `list_commits`) | |
| Posting an issue comment or creating an issue/PR with a multi-line body | **`gh` (today) / MCP (once auth lands)** | See the write-path note below. Once the MCP server has a token, `add_issue_comment` and friends eliminate the tmp-file preamble. |
| Adding/removing a single label | **`gh issue edit --add-label` / `--remove-label`** | MCP only supports full label replacement (`update_issue` with the full `labels` array). For an incremental add, `gh` is one call, MCP is two (read, then replace). |
| Closing an issue with `state_reason` (completed / not_planned / duplicate) | **`gh issue close --reason`** | MCP does not expose `state_reason`. |
| Merging a PR with `--delete-branch` | **`gh pr merge --squash --delete-branch`** | MCP has `merge_pull_request` but no branch-delete flag. |
| Editing an existing PR's body after CI or pre-merge checklist updates | **`gh pr edit --body-file`** | MCP has no `update_pull_request` body edit. |
| Watching a workflow run / CI / deploy | **`gh run watch --interval 60 --exit-status`** | MCP has no long-poll watcher. |
| Listing workflow runs or viewing job detail | **`gh run list` / `gh run view`** | MCP has no `actions` API exposure. |
| Checking auth state or rate-limit budget | **`gh auth status` / `gh api rate_limit`** | MCP does not cover auth state or the rate-limit endpoint. |
| Shell script (anything under `scripts/`), GitHub Action, or Git hook | **`gh`** | MCP runs inside Claude Code and is not reachable from shell scripts that execute outside the agent context. |

## The write-path note (read this before migrating writes)

As of this repo's current machine configuration, the `github` MCP server is connected but **no `GITHUB_PERSONAL_ACCESS_TOKEN` is set** in its environment. Live smoke test from a `/task` subagent (2026-04-18):

- `mcp__github__get_issue` — works (unauthenticated reads against this public repo).
- `mcp__github__add_issue_comment` — fails with `MCP error -32603: Authentication Failed: Requires authentication`.
- `mcp__github__update_issue`, `mcp__github__create_issue`, `mcp__github__create_pull_request` — same failure.

Until that is fixed:

- **Reads:** prefer MCP.
- **Writes:** stay on `gh`. Every skill that currently uses `gh issue comment --body-file`, `gh issue edit`, `gh issue create`, `gh pr create`, `gh pr merge`, `gh issue close` etc. keeps those calls unchanged.

Once the MCP server has a token (see follow-up issue referenced in `docs/agent/gh-to-mcp-migration.md`), a Phase 2 PR will migrate the high-value writes (`add_issue_comment`, `create_issue`, `create_pull_request`) to MCP. Writes without an MCP equivalent or with material feature gaps (`gh pr merge --delete-branch`, `gh issue close --reason`, `gh pr edit`) stay on `gh` permanently.

## How to load a deferred MCP tool

MCP tools are **deferred** — they appear as names in the subagent's tool registry but the JSON schema is not loaded until you ask for it. Before the first use in a session, call `ToolSearch`:

```
ToolSearch query="select:mcp__github__get_issue" max_results=1
```

You can load several at once by comma-separating them in the `select:` query. After the schema is loaded, the tool is callable like any other function for the rest of the session.

## Quoting / escaping

MCP call arguments are JSON-structured — no shell involved. A comment body with backticks, single quotes, double quotes, dollar signs, and newlines is just a `body: "..."` JSON string. This is the main ergonomic difference from `gh --body-file`.

## Tool-by-tool mapping

For the full inventory of every `gh` subcommand used in skills/docs and its MCP counterpart (including the gaps), see `docs/agent/gh-to-mcp-migration.md`.
