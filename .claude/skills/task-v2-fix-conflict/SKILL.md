---
description: Fix-conflict phase for the per-phase /task-v2 pipeline. Reads a rebase-conflict bundle, resolves conflicts semantically against updated origin/main content, and produces either a resolved-file set OR an unresolvable signal.
argument-hint: "<agent-id>"
maxTurns: 60
model: haiku
---

# /task-v2-fix-conflict skill

Fix-conflict phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6). Invoked by the agent-runner entrypoint when the pre-push rebase (in the `push_and_pr` phase) OR the start-of-ralph baseline rebase hits a conflict against `origin/main`. Reads the pre-rebase ralph patch, the set of conflicted files, the commits that landed on main since the agent's branch base, and the current main-branch content of each conflict file; then emits either a `resolved` verdict with the reconciled file contents OR an `unresolvable` verdict that routes the agent through the diagnoser toward a `conflict_unresolvable` terminal.

**Prerequisites:** The agent-runner entrypoint has already (a) aborted the in-progress rebase (`git rebase --abort`), (b) incremented `dispatcher.agents.merge_conflict_attempts`, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/fix-conflict.json`. The entrypoint owns all git operations after this skill returns — the skill does NOT run `git`, does NOT push, does NOT call `gh`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/fix-conflict.json` with `verdict="resolved"` (conflict reconciled; `resolved_files[]` populated) OR `verdict="unresolvable"` (reasoned explanation in `resolution_notes`).

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation. This subprocess is already a dispatcher-spawned background task.

**IMPORTANT — No side effects on GitHub or the worktree.** This skill does NOT comment on issues, edit labels, close issues, edit PRs, write files directly into the worktree, run `git`, or invoke `gh`. The only write is the output JSON at the path above.

**IMPORTANT — Smallest correct reconciliation.** Apply the smallest change that satisfies BOTH the agent's original intent (preserved in `original_patch`) AND the updated state of `origin/main` (in `main_files_content`). Do not refactor, do not rename, do not add defensive code unrelated to the conflict. The reconciled files accrete on top of main's latest — they are NOT replacements for the agent's ralph work, they are ralph's work re-applied on top of the new base.

**IMPORTANT — Budget bounds.** The entrypoint bounds this skill at `FIX_CONFLICT_MAX_ATTEMPTS` invocations per agent lifetime (default 2). A second attempt is reasonable because main may advance again during the first resolution run. A third attempt is not: if two rounds of claude-resolution couldn't close the loop, the conflict is structurally adversarial and deserves `unresolvable`.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/fix-conflict.json`. Required fields:

- `agent_id` (str) — the failing agent's UUID.
- `issue_number` (int) — the GitHub issue this agent is working on.
- `issue_body` (str) — the original task description (so you can reason about what the agent was trying to do).
- `original_patch` (str) — the pre-rebase unified diff of the agent's ralph output (from `dispatcher.ralph_patches` latest SHIP row, OR `git diff origin/main..HEAD` at the moment the rebase aborted — the entrypoint picks whichever is available). This is what the agent WANTED to ship before main advanced.
- `conflict_files` (list of `{path, conflict_markers_text}`):
  - `path` (str) — repo-relative path of each conflicted file.
  - `conflict_markers_text` (str) — the file's on-disk content at the point `git rebase --abort` ran, INCLUDING the `<<<<<<<` / `=======` / `>>>>>>>` markers. This shows you exactly which hunks disagreed and what both sides looked like at the time of the conflict. Note: after `--abort`, the on-disk content should have NO markers (abort restores pre-rebase state); the entrypoint captures the markers BEFORE aborting and stashes them here.
- `main_commits_since_base` (list of `{sha, author, subject, body, stat}`) — commits on `origin/main` that landed after the agent's branch-base. Usually 1–5 entries; ordered newest-first. `stat` is the `git show --stat` summary for each commit so you can see at a glance which files it touched.
- `main_files_content` (list of `{path, content}`) — the CURRENT (post-rebase-target) content of each conflict-file as it exists on `origin/main`. This is the authoritative reference for the new base. When reconciling, the resolved file should fit on top of THIS content, not on top of the pre-rebase `HEAD`.
- `worktree_path` (str) — absolute path of the agent's worktree (for context; do NOT write files here).
- `repo_root` (str) — same as `worktree_path` for this pipeline.
- `attempt_number` (int) — how many times this skill has already run for this agent (1 on first invocation, 2 on second). Defaults to 1 if absent.

Optional:

- `max_iterations` (int) — cap on internal claude iterations. Defaults to 1 (one resolve-and-return pass). Not a retry budget — the retry budget is `FIX_CONFLICT_MAX_ATTEMPTS` enforced by the entrypoint.

If the file is missing or malformed, exit 0 with `verdict="unresolvable", resolution_notes="input JSON missing or malformed"`, and an empty `resolved_files` list.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/fix-conflict.json`:

```json
{
  "agent_id": "<echo>",
  "verdict": "resolved" | "unresolvable",
  "resolution_notes": "<short paragraph — what was reconciled or why not>",
  "resolved_files": [
    {"path": "...", "content": "..."}
  ],
  "conflict_files": ["<path>", ...],
  "notes": "<optional prose for the retro phase>"
}
```

- `verdict="resolved"` — the conflict has been semantically reconciled. `resolved_files` contains the **full new content** of every conflict-file (not a diff). The entrypoint will write each file to its path in the worktree, stage it, and create a single new commit with a conventional-commits message. The next pass of `push_and_pr` will re-fetch + rebase + push — and should succeed because the resolved content matches the new base.
- `verdict="unresolvable"` — the conflict cannot be resolved without a human decision. `resolution_notes` MUST explain why (structural collision, semantic mismatch, AC conflict). `resolved_files` should be empty. The entrypoint routes the agent to the `conflict_unresolvable` terminal; the diagnoser picks it up and chooses between `AC_INFEASIBLE` (if `resolution_notes` indicates the original intent no longer applies) and `retry_with_hint` (if a fresh agent with a better mental model could succeed).

Always exit 0. Verdict comes from the JSON, not the exit code.

**`resolved_files` content rules:**

- Each entry's `content` is the complete file body after reconciliation — NOT a diff, NOT a patch, NOT a hunk. The entrypoint writes `content` verbatim to `{worktree}/{path}`.
- UTF-8 encoded. Preserve the file's line-ending style (trailing newline / no trailing newline) from the pre-conflict `main_files_content`. Never force CRLF.
- Every file in `conflict_files` (input) MUST appear in `resolved_files` (output) on `resolved`, unless the reconciliation legitimately drops a file (rare — note in `resolution_notes`). Do NOT include files that were not in `conflict_files`.
- Do NOT include conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) in the content. If you can't reconcile a hunk without leaving markers, emit `unresolvable` for the whole file set — a half-resolved file is worse than an explicit failure.

---

## Step 1 — Read the bundle and understand the conflict

1. Read `{worktree}/tmp/dispatcher-input/fix-conflict.json` with a helper script. Write the helper to `{worktree}/tmp/dispatcher-fix-conflict/read_input.py` first, then invoke it — no inline `python -c`.
2. For each conflict file:
   - Compare `conflict_markers_text` (what the two sides looked like at conflict time) against `main_files_content[].content` (the current main-side content).
   - Extract from `original_patch` the hunks the agent applied to this file. These represent the agent's *intent*.
3. Read the relevant commits in `main_commits_since_base` (subject + body + stat) to understand what changed on main.
4. Form a hypothesis for each hunk: can the agent's intent be re-applied on top of main's new content, and if so, how?

## Step 2 — Classify the conflict

For each file (and its overall set), classify into one of three buckets:

| Classification | Definition | Action |
|---|---|---|
| **Parallel edits** | Main and the agent edited different regions of the same file — the rebase tool flagged them because they were on the same blob, but the hunks don't overlap semantically. | Merge both edit sets on top of main. Straightforward — re-apply the agent's hunks against the new main content at the matching anchor. |
| **Overlapping edits, compatible intent** | Both sides touched the same region but they're trying to achieve compatible things — e.g. main renamed `foo` → `bar`, agent added a call to `foo`. The agent's call must be updated to `bar`, but the intent survives. | Re-express the agent's hunk in the vocabulary of main's new content. Preserve the agent's *behavior*; adapt the *form*. |
| **Semantic collision** | Both sides changed the same behavior incompatibly — e.g. main deleted the function the agent's PR relied on; main rewrote the function's signature in a way that changes its contract; main added a new acceptance criterion that the agent's implementation contradicts. | `unresolvable`. The agent's original intent no longer fits on top of the new base. Explain in `resolution_notes`. |

**Classification heuristics:**

- If main's commit stat shows `-50 lines` in a file the agent added `+5 lines` to, the deletion probably removed the exact region the agent was editing — check carefully.
- If main's commit subject mentions "refactor", "rename", "delete", "revert" or "rewrite" on a file the agent also touched, the collision is more likely semantic than parallel.
- If the file is a test fixture or a schema, parallel-edit resolution is usually safe. If it's a hot code path (a function body, a route handler), overlap-resolution requires more care.

## Step 3 — Produce resolved content (on `resolved`) OR explain (on `unresolvable`)

For `resolved`:

1. For each file in `conflict_files`, build the complete resolved content:
   - Start from `main_files_content[i].content`.
   - Apply the agent's intent from `original_patch`, translated into main's vocabulary per §2.
   - Verify no conflict markers remain.
   - Verify the imports / top-level declarations make sense (e.g. if main dropped an import the agent needed, re-add it; if main renamed an import the agent used, update the usage).
2. Populate `resolved_files` with one entry per conflict file.
3. Set `resolution_notes` to a short paragraph: "Resolved N conflicts across M files. Main had landed commits A, B, C. Key translations: agent's call to `foo()` → `bar()` per commit B. Agent's addition to function `process_x` re-anchored on main's new line 42 signature."
4. Verdict `resolved`.

For `unresolvable`:

1. Set `resolved_files: []`.
2. Set `resolution_notes` to an honest paragraph explaining the collision:
   - WHICH file's conflict couldn't be resolved.
   - WHAT main changed that the agent's intent no longer fits.
   - WHY a human decision is needed (AC changed? feature was removed? design diverged?).
   - Use one of these phrasings to help the diagnoser classify:
     - `"function X was rewritten on main — agent's addition no longer applies"` → diagnoser picks `AC_INFEASIBLE`.
     - `"main landed a sibling feature with overlapping design; agent can re-attempt with updated context"` → diagnoser picks `retry_with_hint`.
     - `"main reverted the feature this PR depended on"` → `AC_INFEASIBLE`.
3. Verdict `unresolvable`.

## Step 4 — Write the output JSON

Write your helper to `{worktree}/tmp/dispatcher-fix-conflict/write_output.py` first (avoid inline `python -c`), then invoke it to serialize your final dict to `{worktree}/tmp/dispatcher-output/fix-conflict.json`.

---

## What this skill does NOT do

- **Does not run `git`.** The entrypoint owns all git operations — rebase, abort, stage, commit, push.
- **Does not call `gh`.** Reading the issue body is done from the input bundle (already fetched by the entrypoint).
- **Does not write files into the worktree.** Only the output JSON is written. On `resolved`, the entrypoint applies `resolved_files[]` by reading the output and writing each file back itself.
- **Does not retry itself.** If you can't resolve, return `unresolvable` — the entrypoint's budget machinery decides whether to retry on the next agent invocation.
- **Does not touch acceptance criteria.** AC collisions are the diagnoser's domain, not the fix-conflict skill's.

## Escalation policy

- **Attempt 1** (`attempt_number=1`) — primary pass. Try hard to classify and reconcile.
- **Attempt 2** (`attempt_number=2`) — only reached when main advanced AGAIN during attempt 1's resolution (rare but valid). Same procedure.
- **Attempt 3+** — unreachable. `FIX_CONFLICT_MAX_ATTEMPTS=2` caps this. The entrypoint will not invoke you past 2.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules. Write helper scripts to `{worktree}/tmp/dispatcher-fix-conflict/` first.
- All temp files go under `{worktree}/tmp/`, never `/tmp/`.
- Prefer `Read` for files, not `cat`. Prefer `Edit` over `Write` when you're editing something that already exists — but note that in this skill the outputs live only in JSON, not in the worktree.
- The `resolution_notes` paragraph surfaces directly in the diagnoser's context bundle. Write it for a reader who has the stderr but not the files.
- When in doubt between `resolved` and `unresolvable`, pick `unresolvable` with a precise note. A wrong `resolved` produces a broken commit that the next push will reject; an explicit `unresolvable` routes through the diagnoser and preserves the agent's ralph work for a human or follow-up agent.
