# GitHub API access — MCP vs `gh` CLI

> **When to read this:** you are writing or editing a skill, agent doc, or CLAUDE.md section that interacts with GitHub, and need to choose between a `mcp__github__*` MCP tool and the `gh` CLI.
>
> **TL;DR:** prefer MCP for structured reads. Keep `gh` for writes (temporarily — see the write-path note below), for anything in the Gap rows of `docs/agent/gh-to-mcp-migration.md`, and for shell scripts where MCP is not reachable.

## Why this doc exists

Agents historically used `gh` for every GitHub operation, which forced a handful of annoying patterns:

- Multi-line content needed a `--body-file` preamble — write to a tmp file, then invoke `gh`.
- Reads required `--json field1,field2,...` enumeration and often `-q .[0].databaseId` jq juggling.
- The combination of shell quoting and `-q` mixed-quote rules tripped the platform's safety checks (see `docs/agent/unattended-patterns.md` §`gh` jq Quoting).

The `github` MCP server exposes the REST API directly as structured tool calls — body is a native string, the response is parsed JSON, no shell escaping. For reads this is a clear win. For writes it will be a clear win once auth is configured (see below).

## Decision rule

| Situation | Use | Why |
|---|---|---|
| Structured read of a single issue, PR, comment list, file contents | **MCP** (`get_issue`, `get_pull_request`, `get_pull_request_comments`, `get_file_contents`, etc.) | No `--json` enumeration, no `-q` jq. Returns full typed object in one call. |
| Listing issues or PRs with filters (labels, state, assignee) | **MCP** (`list_issues`, `list_pull_requests`) | Native filter params, paginated response. |
| PR status checks / review state | **MCP** (`get_pull_request_status`, `get_pull_request_reviews`, `get_pull_request_files`) | |
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
