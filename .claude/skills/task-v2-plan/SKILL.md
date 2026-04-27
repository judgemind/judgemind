---
description: Plan phase for the per-phase /task-v2 pipeline. Reads an issue + comments, produces a plan document, scope-check findings, and a go/no-go signal. Called once per task by the dispatcher daemon before /task-v2-ralph.
argument-hint: "<agent-id>"
maxTurns: 50
model: opus
---

# /task-v2-plan skill

Plan phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Reads an issue + comments handed over by the daemon, performs scope-check, and emits a structured plan for `/task-v2-ralph` to execute.

**Prerequisites:** The dispatcher daemon has already (a) claimed the issue (posted the pickup comment), (b) created the worktree at `{worktree}`, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/plan.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/plan.json` — a structured plan that ralph can execute mechanically. No worktree writes outside of `tmp/`, no git operations, no issue comments. Pure reading + reasoning + output-JSON.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. The `/task-v2-plan` subprocess is already a dispatcher-spawned background task — further backgrounding causes completion notifications to surface in the wrong context and leads to lost results.

**IMPORTANT — Heartbeat lines (issue #3017).** Emit two distinctive heartbeat lines to stdout so CloudWatch Log Insights can answer "what was the last thing the skill did before its stream went silent?" during a hang. Run the Bash tool with:

- `echo PHASE_START plan` immediately after reading this SKILL.md (before Step 1).
- `echo PHASE_DONE <verdict>` right before writing the output JSON (Step N — the last step), where `<verdict>` matches the verdict/go field the output JSON will carry.

These are plain `echo` statements — the dispatcher daemon's stream-forwarder (`scripts/dispatcher/stream_forwarder.py`) picks them up from subprocess stdout and tags them with `agent_id`, `issue_number`, `phase=plan`, `stream=stdout` in CloudWatch + a real-time JSONL mirror at `{worktree}/.dispatcher/plan-<agent_id>.jsonl`. The grep-friendly `PHASE_START` / `PHASE_DONE` tokens make it trivial to filter phase boundaries: `filter @message like /PHASE_START/`.

**IMPORTANT — No side effects.** This phase is read-only against the repo and GitHub. Do not edit code, do not comment on issues, do not spawn subagents. The only write is the output JSON at `{worktree}/tmp/dispatcher-output/plan.json`.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/plan.json`. The daemon guarantees it exists before spawning this subprocess (§6 step 6). Required fields:

- `agent_id` (str) — your UUID for correlation.
- `issue_number` (int) — the issue being worked.
- `issue_title` (str) — title text.
- `issue_body` (str) — raw markdown issue body.
- `issue_comments` (list of `{author, author_association, date, body}` objects) — filtered to non-bot authors by the daemon.
- `issue_labels` (list of str).
- `worktree_path` (str) — absolute path to your worktree root.
- `repo_root` (str) — same as `worktree_path` (retained for symmetry with `/task`).

Optional:

- `blocked_by` (list of int) — any `Blocked by #N` references the daemon parsed. If non-empty and any are still open, you must produce `go=false`.
- `parent_issue` (int or null) — `Parent: #N`.
- `linked_specs` (list of path) — any paths under docs/specs that the daemon extracted from issue text.

If the file is missing or malformed, exit with `go=false, block_reason="input JSON missing or malformed"`. Never read from GitHub to reconstruct — the daemon owns the handoff.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/plan.json` with these fields, then exit 0:

```
{
  "agent_id": "<echo from input>",
  "issue_number": <int>,
  "go": true|false,
  "block_reason": null | "<string>",
  "task_type": "coding" | "operational",
  "plan_text": "<markdown, ≤500 words>",
  "acceptance_criteria": ["<criterion 1>", "<criterion 2>", ...],
  "scope_check": [
    {"search_pattern": "<regex or glob>", "locations_found": ["<path>:<line>", ...], "in_scope": true|false, "note": "<why>"}
  ],
  "relevant_files": ["<path>", ...],
  "relevant_docs": ["docs/specs/<name>.md", ...],
  "change_type": "api" | "scraper" | "ingestion" | "web" | "db_migration" | "dx_tooling" | "backfill_script" | "docs" | "agent_skill" | "no_deployed_component",
  "dependencies_to_install": ["<package-relative-path>", ...],
  "collapsed_comments": [<comment dicts — see Step 5.5>]
}
```

Exit 0 regardless of `go=true` or `go=false`. The daemon reads the verdict from the JSON, not the exit code.

---

## Step 1 — Understand the problem

Read the issue body thoroughly. Identify:

- The concrete problem or feature.
- The acceptance criteria (typically `- [ ]` checkboxes). Each criterion should become one entry in `acceptance_criteria` — copy the criterion text verbatim.
- Related issue / PR references (e.g. `Closes #N`, `Parent: #N`, `Blocked by #N`, `See also #N`).
- Linked specs under `docs/specs/`. Read them if they affect the design.
- Any implementation notes or scope clarifications in issue comments. Non-bot comments may supersede the original body (re-scoping, adding criteria).

Grep existing code for patterns that match the described change. The goal is to be consistent with what's there.

## Step 2 — Scope completeness check

Before recommending `go=true`, search the codebase for all locations affected by the described change. This is the single most valuable step in plan — it catches "fix X in file Y" issues that should have said "fix X in files Y1, Y2, Y3".

For each thing-to-change, use Grep with the specific identifier:

- Renaming a function? Grep for all call sites.
- Fixing a data quality bug in one scraper? Grep for the same pattern across other scrapers.
- Removing a deprecated flag? Grep for every call to the deprecated function.

For each grep, record one entry in `scope_check`:

- `search_pattern` — the pattern used.
- `locations_found` — relevant matches (prune noise; cap at ~20 per pattern).
- `in_scope` — `true` if ralph should touch this location; `false` if it's out of scope.
- `note` — why in/out. Follow-ups go in `plan_text` under "Follow-ups".

If `in_scope=false` locations exist that arguably should have been in scope: mention in `plan_text` under "Follow-ups to file" so `/task-v2-retro` can file them at the end.

## Step 3 — Resolve ambiguity

If the issue requires a maintainer decision before you can proceed, set `go=false` and write a concrete `block_reason` in one sentence naming the exact decision needed. Do not guess on ambiguous requirements — a guessed-wrong PR costs more than a blocked-for-review issue.

If `acceptance_criteria` is empty or too vague to verify mechanically, set `go=false` with `block_reason="acceptance criteria need sharpening"` and quote the worst-offending criterion in `plan_text`.

If `blocked_by` contains open issues, set `go=false` with `block_reason="blocked by open issues: #A, #B"`.

If the issue is clearly a duplicate of an open issue, set `go=false` with `block_reason="duplicate of #N"`.

## Step 4 — Determine change type and dependencies

Set `change_type` from this table (used by `/task-v2-verify` to pick the verification strategy):

| change_type | When |
|---|---|
| `api` | `packages/api/` (GraphQL, REST handlers) |
| `scraper` | `packages/scraper-framework/src/courts/` or framework |
| `ingestion` | ingestion worker, transcription, enrichment |
| `web` | `packages/web/` |
| `db_migration` | `packages/api/migrations/` |
| `backfill_script` | one-off data script under `scripts/` |
| `dx_tooling` | agent workflow, CI scripts, pre-push hooks, guards |
| `docs` | docs-only |
| `agent_skill` | `.claude/skills/` content |
| `no_deployed_component` | CI config, docs, agent config with no deploy step |

Set `dependencies_to_install` to the packages ralph's worker will need. The daemon runs `scripts/install-package-venv.sh <pkg>` for each entry in the `setup` phase before spawning ralph. Examples: `scraper-framework`, `nlp-pipeline`, `api`, `web`. Empty list for docs-only, terraform-only, or `.claude/`-only changes.

## Step 4.5 — Classify task_type

Set `task_type` to one of two values. This controls which pipeline branch the daemon runs after plan:

| task_type | When | Pipeline |
|---|---|---|
| `coding` | The task requires writing or editing code, tests, docs, or config files in the repo (i.e., a PR will be opened). This is the default for any issue that touches source files. | plan → ralph → summary → push+PR → CI → merge → deploy → verify → retro |
| `operational` | The task requires only running a script, executing a DB query, firing a gh action, adding/removing labels, rebuilding derived data, or any other operational action — **no code change, no PR**. | plan → operational skill → done |

**Decision tree:**

1. Does the acceptance criteria require editing any file tracked in git (Python, TypeScript, SQL, YAML, Markdown, Terraform, shell scripts, skill SKILL.md files)? → `coding`
2. Does the issue ask to run `rebuild_db.py`, `scripts/ecs-run-task.sh`, `scripts/dev-db-query.sh`, a one-off ECS task, or a gh label/close/comment action only? → `operational`
3. Does the issue ask to restore a specific county's data, reprocess a batch of documents, backfill derived records, or similar "run a script against the DB" work? → `operational`
4. Unclear? Default to `coding` — the operational path skips all code-review guardrails, so false-positives here bypass the safety net.

**Important:** When `task_type=operational`, set `dependencies_to_install=[]` (the operational skill does not need a ralph venv). `change_type` should still reflect what kind of system is affected (e.g. `dx_tooling`, `backfill_script`, `no_deployed_component`).

## Step 5 — Write the plan

For `go=true`, write `plan_text` as ≤500 words of markdown covering:

1. **What will change, per file** — specific files and the shape of the edit. Name the functions/types that ralph will add or modify.
2. **Tests** — which test files will be added or updated. Per CLAUDE.md, all code has tests. For TypeScript in `packages/web/`, prefer colocated `*.test.tsx`. For Python, `tests/` mirrors `src/`.
3. **Scope boundary** — what is intentionally NOT done. List follow-ups ralph should NOT touch.
4. **Acceptance-criterion verification map** — one line per criterion: "Criterion X verified by <test file>::<test name>" or "Criterion X verified by <behavior at runtime>".
5. **Consistency checks** — if this change follows an existing pattern, name the precedent file that ralph should model the new code after.
6. **Follow-ups to file** — one-line descriptions of any out-of-scope findings from Step 2. `/task-v2-retro` will file them.

Populate `relevant_files` with every path ralph should Read during implementation. Populate `relevant_docs` with any docs/specs or docs/agent files ralph must honor.

## Step 5.5 — Collapse long comment threads

After writing `plan_text`, collapse the raw `issue_comments` for downstream phases:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("scripts").resolve()))
from dispatcher.collapse_comments import collapse_comments
collapsed = collapse_comments(issue_comments)
```

Where `issue_comments` is the list read from `plan.json` input. Set `collapsed_comments` in the output JSON to the result:

- When total token estimate is **under 2000**, `collapsed_comments` is identical to `issue_comments` (same objects).
- When total token estimate is **2000 or above**, `collapsed_comments` contains the first comment verbatim, collapsed middle entries (body truncated to 120 chars, `_collapsed: true`), and the last two verbatim.

If any `_collapsed=true` entry's body is still unclear in context, you MAY inline a one-line LLM summary into its `body` field in place of the raw truncation — but this is optional enrichment. The deterministic structural collapse (what `collapse_comments` does) is the minimum requirement.

## Step 6 — Write the output JSON

Use the Write tool to produce `{worktree}/tmp/dispatcher-output/plan.json` containing all fields above. Exit 0.

---

## What this skill does NOT do

- **Does not install dependencies.** The daemon's `setup` phase runs `scripts/install-package-venv.sh` based on `dependencies_to_install`.
- **Does not modify code.** Ralph owns implementation.
- **Does not commit, push, or open a PR.** Daemon owns git + GitHub operations after ralph.
- **Does not post issue comments.** `/task-v2-summary` owns the pre-PR process-summary comment; the daemon posts verification-evidence via `/task-v2-verify`.
- **Does not run tests.** Ralph's worker runs tests as part of the iteration loop.

## Worked example — what a good plan output looks like

For an issue "fix(scraping): Orange County department parsing drops leading zero":

- `acceptance_criteria`: `["Department 03 renders as '03' not '3' on case detail pages.", "Regression test asserts department parsing preserves leading zeros."]`
- `scope_check`: one entry for `re.search(r"dept=(\d+)"` grep, `locations_found: ["packages/scraper-framework/src/courts/ca/orange.py:142", "packages/scraper-framework/src/courts/ca/riverside.py:98"]`, `in_scope: false, note: "Riverside uses same pattern — file follow-up to audit all counties"`.
- `relevant_files`: `["packages/scraper-framework/src/courts/ca/orange.py", "packages/scraper-framework/tests/courts/ca/test_orange.py"]`.
- `change_type`: `scraper`.
- `task_type`: `coding`.
- `dependencies_to_install`: `["scraper-framework"]`.
- `plan_text`: covers the one-line regex fix, the new regression test against a fixture, the out-of-scope note for Riverside, and the AC verification map.
- `collapsed_comments`: result of `collapse_comments(issue_comments)` — empty list when no issue comments, or the collapsed/verbatim list otherwise.
- `go`: true.

For an issue "docs(agent): add retro phase to task skill" where the task skill doesn't exist anymore:

- `go`: false.
- `block_reason`: `"issue references .claude/skills/task/SKILL.md §5 but that section was removed in #2710; acceptance criteria need sharpening against current skill structure"`.

For an issue "ops: restore Santa Clara county data after SC outage (#2419)":

- `acceptance_criteria`: `["Santa Clara county rulings are queryable in the DB after rebuild.", "Row count matches pre-outage snapshot."]`
- `scope_check`: one entry confirming `rebuild_db.py --county "Santa Clara"` is the right script.
- `relevant_files`: `["scripts/rebuild_db.py", "docs/agent/infrastructure-reference.md"]`.
- `change_type`: `backfill_script`.
- `task_type`: `operational`.
- `dependencies_to_install`: `[]`.
- `plan_text`: "Run `rebuild_db.py --county 'Santa Clara'` via ECS oneshot. Query derived.rulings to confirm count. Post evidence comment and close issue."
- `go`: true.

## Reminders

- No `$()`, no heredocs, no `python -c`. See the repo root `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Prefer MCP for GitHub reads (`mcp__github__get_issue`, `mcp__github__get_file_contents`). Keep `gh` for writes — but in this phase, you should make no writes to GitHub at all.
- Use Grep, Glob, and Read tools — never `find`, `cat`, `head`, `tail` from Bash.
- If context exceeds ~20k tokens from long issue bodies or comment threads: summarize comments >2k tokens in `plan_text` rather than quoting verbatim. The downstream phases only see your `plan.json`, not the raw issue.
- The dispatcher-daemon owns the agent status in `dispatcher.phase_transitions`. Do NOT write to any file under `{repo_root}/tmp/agent-status/` — that convention belongs to the laptop-dispatcher `/task` skill, not the Fargate daemon.
