---
description: Summary phase for the per-phase /task-v2 pipeline. Reads the issue body and the git diff, produces a process-summary issue comment (with AC mapping), a conventional commit message, a PR title, and a PR body.
argument-hint: "<agent-id>"
maxTurns: 30
model: haiku
---

# /task-v2-summary skill

Summary phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Maps the ralph-produced diff back to the issue's acceptance criteria and emits three artifacts the daemon needs before opening the PR.

**Prerequisites:** The dispatcher daemon has already (a) run `/task-v2-ralph` with verdict=SHIP, (b) captured the full git diff and changed-file list into the input bundle, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/summary.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/summary.json` with the process-summary comment, commit message, PR title, and PR body. No GitHub writes — the daemon posts the comment and creates the PR after reading this skill's output.

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — Heartbeat lines (issue #3017).** Emit two distinctive heartbeat lines to stdout so CloudWatch Log Insights can answer "what was the last thing the skill did before its stream went silent?" during a hang. Run the Bash tool with:

- `echo PHASE_START summary` immediately after reading this SKILL.md (before Step 1).
- `echo PHASE_DONE <verdict>` right before writing the output JSON (Step N — the last step), where `<verdict>` matches the verdict/go field the output JSON will carry.

These are plain `echo` statements — the dispatcher daemon's stream-forwarder (`scripts/dispatcher/stream_forwarder.py`) picks them up from subprocess stdout and tags them with `agent_id`, `issue_number`, `phase=summary`, `stream=stdout` in CloudWatch + a real-time JSONL mirror at `{worktree}/.dispatcher/summary-<agent_id>.jsonl`. The grep-friendly `PHASE_START` / `PHASE_DONE` tokens make it trivial to filter phase boundaries: `filter @message like /PHASE_START/`.

**IMPORTANT — No side effects.** This phase does not modify code, does not commit, does not push, does not comment on GitHub. The only write is the output JSON at `{worktree}/tmp/dispatcher-output/summary.json`.

---

## When skipping a scope item (anti-hallucination, issue #3126)

Summary inherits ralph's output and classifies each AC into one of: met / deferred / unmet_shape_mismatch / infeasible / not_applicable. Any reason summary gives for marking an AC deferred, unmet, or infeasible — and any justification text it writes into `process_summary_md`, `pr_body_md`, or `ac_mapping[].evidence` — MUST be grounded. The same anti-hallucination rule that binds ralph (see `.claude/skills/task-v2-ralph/SKILL.md` §"When skipping a scope item") binds summary: a hallucinated skip-reason cannot be laundered through an AC classification.

**Every classification entry MUST ground in one of:**

1. **A line actually present in `git_diff` or `changed_files`** — cite file path, function name, test name, or line range. `Met` and `unmet_shape_mismatch` classifications need this.
2. **A pattern match on the issue body's `Verify:` line** — for `deferred` classifications via the marker / heuristic lists in §2a. The exact `Verify:` text goes into `deferred_acs[].verify_instruction` verbatim.
3. **A concrete absent-symbol citation** — for `infeasible` classifications, the `evidence` paragraph MUST name the missing symbol and the grep you ran (or would run) to confirm it is not in the tree. "The AC can't be met because…" with no cite is hallucination.
4. **An explicit out-of-scope note from `scope_check`** — for `not_applicable` classifications, reference the scope_check entry that excluded the item.

**Forbidden phrases** — without an attached log / stderr excerpt from a real command or a line from the actual diff, the following are hallucination and MUST NOT appear in `process_summary_md`, `pr_body_md`, or any `ac_mapping[].evidence` field:

- "N consecutive push rejections"
- "Tried N times"
- "Confirmed via the rejection"
- "Three push rejections confirmed this"
- Any "I observed X error" that isn't pasted from real output.

**"Tracked at #N" freshness check.** If an AC evidence paragraph cites an issue number as a blocker (e.g. "this AC is infeasible because it depends on #XXXX"), verify the referenced issue is open via `gh issue view <N> --repo judgemind/judgemind --json state,closedAt`. A closed issue is historical and cannot block the current AC; if the issue is closed, the AC is not infeasible-by-dependency and you must reclassify.

The intent is to enforce the rule at both layers (ralph AND summary) so neither layer can silently launder a fabricated skip-reason into the final PR. Ralph owns the commit message and in-diff prose; summary owns the `unmet_criteria` list and the process-summary comment. Both must be grounded.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/summary.json`. Required fields:

- `agent_id` (str).
- `issue_number` (int).
- `issue_title` (str).
- `issue_body` (str).
- `issue_comments` (list of `{author, date, body}` — non-bots only).
- `ralph_summary` (str) — the 1-3 sentence summary from ralph output.
- `changed_files` (list of path).
- `git_diff` (str) — full unified diff from `git diff origin/main...HEAD`. Post-#2971, ralph's Step 2.5 always commits its work before returning, so the range resolves against a committed HEAD and the diff is non-empty for every non-no-op SHIP. No working-tree-vs-committed-state branching is required on the summary side.
- `worktree_path` (str).
- `repo_root` (str).
- `branch` (str) — worktree branch name.

Optional:

- `plan_acceptance_criteria` (list of str) — from plan output, useful if the issue body AC got edited mid-flight.
- `scope_check` (list) — from plan output; informs the "Scope decisions" section.

If the file is missing or malformed, exit 0 with empty output and `error` field populated; daemon will retry.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/summary.json`:

```
{
  "agent_id": "<echo>",
  "issue_number": <int>,
  "verdict": "OK" | "AC_INFEASIBLE",
  "process_summary_md": "<markdown comment body>",
  "commit_message": "<conventional-commits subject + body>",
  "pr_title": "<subject line, matches commit subject without body>",
  "pr_body_md": "<full PR body markdown with Summary + Test plan>",
  "deferred_acs": [
    {"index": <int>, "reason": "marker" | "heuristic",
     "verify_instruction": "<Verify: line text>"}
  ],
  "infeasible_acs": [
    {"index": <int>, "evidence": "<paragraph>"}
  ],
  "unmet_criteria": ["<criterion text>", ...],
  "ac_mapping": [
    {"index": <int>, "criterion": "<text>",
     "status": "met" | "deferred" | "unmet_shape_mismatch" | "infeasible" | "not_applicable",
     "evidence": "<1-3 lines or reason>"}
  ],
  "pre_pr_check_notes": "<optional notes about lint/tests — prose>"
}
```

`verdict` is `"OK"` on the normal path (summary proceeds to PR) and `"AC_INFEASIBLE"` on the structural-impossibility path (daemon routes to diagnoser — see §"Classifier order" below). When `verdict == "AC_INFEASIBLE"`, `infeasible_acs` MUST be non-empty; on `verdict == "OK"`, `infeasible_acs` is absent or `[]`.

`deferred_acs` lists acceptance criteria that summary recognized as post-deploy-only. The daemon persists this alongside the rest of summary's output (`dispatcher.phase_outputs`), and `/task-v2-verify` reads it after deploy to run those ACs against the live dev environment. The list may be non-empty on both `verdict="OK"` and `verdict="AC_INFEASIBLE"` — deferred classification runs before the unmet-AC classification.

`unmet_criteria` stays on the **shape-mismatch** path only: ralph produced a valid implementation that simply doesn't match the AC's expected artifact (inline tests vs. a fixture file, wrong field name, etc.). Non-empty `unmet_criteria` triggers today's `needs_review` flow (draft PR + operator comment). Structural-impossibility unmet ACs go to `infeasible_acs` instead, with `verdict="AC_INFEASIBLE"`.

`ac_mapping` is the full per-AC classification, one entry per acceptance criterion in the issue body. Every AC lands in exactly one bucket via the classifier order below; the `process_summary_md` comment is rendered from this list.

Exit 0 regardless. Empty `unmet_criteria` + empty `infeasible_acs` + `verdict="OK"` == proceed.

---

## Step 1 — Extract acceptance criteria

Read the issue body and `issue_comments`. Identify all `- [ ]` checkboxes under an "Acceptance criteria" heading (or similar). Also capture any criterion mentioned in a non-bot comment that supersedes the original body (re-scoping, adding criteria).

If `plan_acceptance_criteria` is populated, cross-check against your extracted list. If they diverge, prefer the issue body + comments (the source of truth). Note the divergence in `pre_pr_check_notes` so the retro phase can file a follow-up.

## Step 2 — Classifier order (deferred → validate → classify unmet)

For EACH acceptance criterion, run these checks in order. The first match wins — do not fall through once a bucket is assigned. Issue #3010 codifies this ordering so the deferred check always runs BEFORE the validate-against-diff check, preventing post-deploy ACs from being flagged unmet.

### 2a — Deferred check (runs FIRST, always)

Before validating against the diff, determine whether the criterion can be validated pre-merge at all:

1. **Marker — authoritative.** If the AC's `Verify:` line begins with `(post-deploy)` (exact literal, case-sensitive), mark it deferred with `reason="marker"`. Stop here — do NOT run the validate or unmet steps for this AC.
2. **Heuristic — for pre-convention issues without the marker.** If the `Verify:` line references a dev/prod artifact, mark it deferred with `reason="heuristic"`. Stop here. Non-exhaustive heuristic tokens (match any of these, case-insensitive):
   - `scripts/ecs-run-task.sh`, `scripts/ecs-run.sh`, `ecs-logs.sh`
   - `scripts/dev-db-query.sh`, `dev-db-query.sh`
   - `curl dev.api.judgemind.org`, `curl https://dev.api.`, `curl https://dev.judgemind.org`
   - `dev.judgemind.org/` (any path), `dev.api.judgemind.org/` (any path)
   - `POST /<index>/_count`, `GET /<index>/_search`, `OpenSearch`, `opensearch` (index query)
   - `rebuild_db.py`, `rebuild_db --reset`, `scripts/rebuild_db.sh`
   - `gh run watch` on a deploy workflow (`deploy-api.yml`, `deploy-scraper.yml`, `deploy-production.yml`, `terraform.yml`)
   - `aws logs`, `CloudWatch Insights`, `/ecs/judgemind-` (log group names)
   - `dispatcher.phase_outputs`, `dispatcher.failures`, `dispatcher.diagnoses` (require a running daemon)
   - `kubectl`, `helm` (future — deploy-dependent)
   - `psql $DATABASE_URL`, `psql dev`, anything that runs a SQL query against a deployed database

   The heuristic is deliberately broad to catch the backlog of pre-convention issues. **False-positive heuristic (tagging a code-verifiable AC as deferred) is benign** — verify runs it post-deploy and confirms the pass. **False-negative (missing a post-deploy AC) is today's behavior — no regression.** When uncertain between deferred-heuristic and validate, prefer deferred — the verify phase is the safety net.

3. **No match.** Proceed to 2b.

Add deferred entries to `deferred_acs` with `{"index": <N>, "reason": "marker" | "heuristic", "verify_instruction": "<exact Verify: line text>"}`. Do NOT count deferred ACs as unmet or infeasible.

### 2b — Validate against diff

For each AC NOT marked deferred, determine from `git_diff` and `changed_files` whether the diff satisfies it. If satisfied: record as **met** in `ac_mapping` with a citation (file path, function name, test name, line range as appropriate). Done for this AC.

### 2c — Classify unmet ACs (shape-mismatch vs. structural-impossibility)

When an AC is neither deferred (2a) nor satisfied by the diff (2b), classify it as one of:

- **Shape mismatch** — ralph produced something valid but not the exact artifact the AC describes. Typical causes: AC demands a fixture file but ralph wrote inline tests; AC expects a specific field name but ralph used a similar one; the diff covers the behavior but not the exact Verify line's pattern. → Add the raw criterion text to `unmet_criteria` AND record in `ac_mapping` with `status="unmet_shape_mismatch"` and a one-line reason. The daemon opens a DRAFT PR with the unmet list in the body and terminates the agent as `status='needs_review'` for operator triage. This is today's flow.

- **Structural impossibility** — the AC references a symbol that doesn't exist in the codebase OR the PR diff, self-contradicts another AC, or demands work outside this issue's scope. → Add an entry to `infeasible_acs` with `{"index": <N>, "evidence": "<paragraph citing the grep / file / conflicting AC>"}` AND record in `ac_mapping` with `status="infeasible"`. Set `verdict="AC_INFEASIBLE"` on the overall output. The daemon writes a `dispatcher.failures(category='summary_ac_infeasible', details={infeasible_acs, deferred_acs, ralph_diff, summary_ac_mapping, agent_id, issue_number})` row and routes to the diagnoser; ralph's shipped diff is discarded on this branch.

- **Not applicable** — the AC was made obsolete by an earlier PR or the approach shifted. → Record in `ac_mapping` with `status="not_applicable"` and an explanation. Do NOT add to `unmet_criteria` or `infeasible_acs` — the criterion simply doesn't apply.

**Err toward shape-mismatch when uncertain.** `AC_INFEASIBLE` requires citable evidence (grep result, file path, conflicting AC index), not a hunch. A draft PR + operator review is reversible; a diagnoser-driven `reissue` rewrites the issue body and discards ralph's diff — higher cost on a false positive. When in doubt, pick `unmet_shape_mismatch` and let the operator decide.

### 2d — Output assembly

After every AC is classified, assemble the output JSON:

- `verdict` = `"AC_INFEASIBLE"` if `infeasible_acs` is non-empty, else `"OK"`.
- `deferred_acs`, `infeasible_acs`, `unmet_criteria`, and `ac_mapping` populate from the per-AC classifications above.
- Render `process_summary_md` from `ac_mapping` (see Step 3).

**Daemon behavior by verdict:**

| verdict | unmet_criteria | deferred_acs | Daemon next step |
|---|---|---|---|
| `OK` | empty | any | push_and_pr (normal flow); verify reads `deferred_acs` post-deploy |
| `OK` | non-empty | any | `_push_and_open_pr` opens a DRAFT PR with `unmet_criteria` in body, terminates agent as `needs_review` |
| `AC_INFEASIBLE` | any | any | `dispatcher.failures(category='summary_ac_infeasible')` + route to diagnoser; ralph diff discarded |


## Step 3 — Write the process-summary comment

Use this markdown structure in `process_summary_md`. The daemon posts this comment on the issue **before** creating the PR (captures the acceptance-criteria reasoning in the issue thread for maintainer review).

```
## Process Summary

### What was implemented

<2-4 sentences drawn from ralph_summary — what changed, where, and at a high level>

### Acceptance criteria mapping

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | <criterion text, truncate at 80 chars with …> | Met | `path/to/file.py::test_or_function` |
| 2 | <criterion text> | Deferred (marker) | Will be verified post-deploy by /task-v2-verify |
| 3 | <criterion text> | Deferred (heuristic) | Matches post-deploy artifact pattern; /task-v2-verify will run it |
| 4 | <criterion text> | Not met — shape mismatch | needs_review: ralph shipped inline tests but AC asks for fixture file |
| 5 | <criterion text> | Infeasible | Cites `--court` flag not present in `scripts/rebuild_db.py` |

### Scope decisions

<Any intentional exclusions — what was NOT done and why. Pull from plan.scope_check if present.>

### Follow-ups filed

<Issues the retro phase will file based on scope_check out-of-scope findings. Leave empty if none.>
```

Use the `Status` column values: `Met`, `Deferred (marker)`, `Deferred (heuristic)`, `Not met — shape mismatch`, `Infeasible`, `Not applicable`. These map 1:1 to the `ac_mapping[].status` enum. Keep the comment under ~4000 characters. The issue thread gets noisy if summary comments are long. On `verdict="AC_INFEASIBLE"`, the daemon does NOT post this comment on the issue — the diagnoser will post its own comment/reissue body instead; summary's `process_summary_md` is retained for the `dispatcher.phase_outputs` audit trail.

## Step 4 — Write the commit message and PR title

Conventional-commits format: `<type>(<area>): <short description> (#<N>)`.

Keep the subject under 72 characters.

Derive `type` from the nature of the change:
- `feat` — new user-visible feature.
- `fix` — bug fix.
- `docs` — documentation only.
- `refactor` — no behavior change.
- `perf` — performance improvement.
- `test` — test-only change.
- `chore` — tooling, build, config.
- `cleanup` — removing dead code, deprecated features.
- `spike` — investigation.

Derive `area` from the primary package changed:

| Path | Area |
|---|---|
| `packages/scraper-framework/` | `scraping` |
| `packages/api/` | `api` |
| `packages/web/` | `web` |
| `packages/nlp-pipeline/` | `nlp` |
| `docs/` | `docs` |
| `.claude/` | `agent` |
| `infra/terraform/` | `infra` |
| `scripts/` (agent/dev tooling) | `dx` |
| `.github/workflows/` | `ci` |
| cross-package | most-affected, or omit `(area)` |

If the diff spans multiple areas, pick the dominant one by line count. Example subjects:

- `feat(scraping): capture Orange County PDF metadata (#1234)`
- `fix(ingestion): handle multi-page rulings correctly (#1235)`
- `docs(agent): update task dependencies reference (#1236)`

Set `pr_title` equal to the subject line only (no body — GitHub uses the title separately from the body).

Set `commit_message` to the subject plus an optional body (separated by a blank line). Include `Closes #<N>` in the body if applicable.

**How the commit message reaches main.** The daemon's `push_and_pr` phase runs `git commit --amend -F <message-file>` to rewrite ralph's placeholder commit (`"WIP: ralph output"`) with the `commit_message` from this skill's output. When the PR is squash-merged, GitHub uses the **PR title** for the merged-main commit subject (not the constituent commit messages). So `pr_title` is the on-main authoritative subject; the amended `commit_message` is what maintainers see on the PR's commits tab and in `git log` on the feature branch. Keeping the two in sync (pr_title == subject line of commit_message) is intentional.

## Step 5 — Write the PR body

Use this template for `pr_body_md`:

```
## Summary

<1-3 sentences describing the change and motivation — pull from ralph_summary, sharpen for PR reviewers who have not read the issue>

Closes #<N>

## Test plan

### Automated checks

- [ ] Lint passes (`ruff check` / `npm run lint`)
- [ ] Format check passes (`ruff format --check` / prettier)
- [ ] Tests pass (`pytest` / `npm test`)
- [ ] CI green

### Post-deploy verification

<Fill in with the verification steps specific to change_type — see /task-v2-verify for the matrix>
- [ ] <verification step 1>
- [ ] Verification evidence posted on #<N> (see /task-v2-verify output)
```

If the change has no deployed component (docs, CI, agent config, infra-only with no service restart):

```
### Post-deploy verification

- [ ] N/A — no deployed component (<specify: docs / CI / agent config / …>)
```

If there is a breaking change or schema migration, add a `## Breaking changes` section naming what callers must do.

If new dependencies were introduced, add a `## Dependencies` section listing them with a one-line justification each.

## Step 6 — Write the output JSON

Emit `{worktree}/tmp/dispatcher-output/summary.json` with all fields above. Exit 0.

---

## What this skill does NOT do

- **Does not open the PR.** Daemon does that after consuming this output.
- **Does not post comments.** Daemon posts `process_summary_md` on the issue.
- **Does not commit or push.** Daemon handles git operations. (The daemon's `push_and_pr` runs `git commit --amend -F` to rewrite ralph's placeholder commit with this skill's `commit_message` output — see #2971.)
- **Does not read GitHub directly.** All issue + comment + diff data comes through the input JSON.
- **Does not run tests.** Any pre-PR check reruns happen in ralph's final iteration or the daemon's pre-push hook.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- This skill is Haiku-tier per spec §18 — keep reasoning tight. The input already contains everything needed; no exploratory Reads or greps beyond the input JSON should be necessary in the common case.
- If the `git_diff` in the input is truncated (>50k chars), the daemon will have flagged it; in that case note "diff truncated" in `pre_pr_check_notes` and base the mapping on the truncated view.
