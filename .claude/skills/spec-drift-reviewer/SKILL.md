---
description: Spec-drift reviewer for the ralph loop. Detects contradictions between a PR diff and documented contracts in .md files. Emits SHIP, REVISE, or SKIPPED.
argument-hint: ""
maxTurns: 30
---

# /spec-drift-reviewer skill

You are the **spec-drift reviewer** in a ralph loop (iteration N of max 5). Your job is to detect substantive contradictions between the current PR diff and documented contracts in `.md` files across the repo (CLAUDE.md, docs/, .claude/skills/).

This is a **read-only** reviewer. You CANNOT edit files. You CANNOT commit or push. Your only output is a verdict file and a feedback file.

**Empowerment:** Grep, Read, Glob, Bash (for `gh pr view`, `git diff`). No file edits.

---

## Inputs

- `{worktree}/tmp/ralph/diff.txt` — unified diff of all changes in this iteration
- `{worktree}/tmp/ralph/changed_files.txt` — full content of changed files
- Worktree path (passed as the first argument, or infer from the environment)

## Outputs

- `{worktree}/tmp/ralph/spec-drift-result.txt` — one of: `SHIP`, `REVISE`, `SKIPPED`
- `{worktree}/tmp/ralph/spec-drift-feedback.md` — markdown listing each finding as:
  `{side_a_file}:{side_a_line}` vs `{side_b_file}:{side_b_line}` — 1-2 line contradiction note

---

## Process

### Step 1 — Run the drift detector

Run the helper script to extract candidate findings:

```
python3 {repo_root}/scripts/spec_drift_detect.py {worktree}
```

The script reads `{worktree}/tmp/ralph/diff.txt`, parses it for:
- New daemon.py action handler methods not mentioned in any SKILL.md
- Renamed `.sh` paths that leave stale references in other `.md` files
- New CLAUDE.md rule lines that contradict references in other `.md` files

It outputs a JSON array of candidate findings to stdout.

### Step 2 — Read the diff and candidate findings

1. Read `{worktree}/tmp/ralph/diff.txt` to understand what changed.
2. Read the JSON findings from Step 1.
3. For each candidate finding, verify it is a **substantive contradiction** (not just a style or capitalization difference).

### Step 3 — Grep for additional drift

For each symbol, path, or rule extracted from the diff that the helper might have missed:
- Use Grep to search all `.md` files for references to the old name/path
- Check whether SKILL.md files document newly added actions or configuration keys

### Step 4 — Classify findings

For each candidate finding, classify it as:
- **SUBSTANTIVE**: the diff introduces a real contradiction — a renamed symbol still referenced elsewhere, a removed action still documented, a new rule that contradicts existing documentation
- **UNRELATED**: the diff and the doc reference the same string but in unrelated contexts (e.g., a coincidental name match)
- **STYLE/CAPITALIZATION**: ignore — only substantive contradictions count

### Step 5 — Write verdict

**SKIPPED conditions** (write `SKIPPED` immediately if ANY applies):
- `diff.txt` is empty or missing
- The diff touches only non-`.md` files with no public interface/contract changes (pure leaf code: internal helpers, test fixtures, comments)
- No `.md` files exist in the worktree

**REVISE conditions** (one substantive finding is sufficient):
- A newly added daemon action, dispatcher config key, or agent skill action is not documented in any relevant SKILL.md or docs file
- A script path or symbol was renamed in the diff but at least one other `.md` file still references the old name/path
- A new CLAUDE.md rule explicitly contradicts guidance in another `.md` file

**SHIP** (no substantive findings):
- The diff either introduces no documented contract changes, OR all `.md` files referencing the changed symbols have been consistently updated

---

## Writing outputs

Write the verdict to `{worktree}/tmp/ralph/spec-drift-result.txt`:
- Exactly one of: `SHIP`, `REVISE`, `SKIPPED`

Write feedback to `{worktree}/tmp/ralph/spec-drift-feedback.md`:

**If SHIP or SKIPPED:**
```
Spec-drift review: <SHIP|SKIPPED>

<One sentence rationale. For SKIPPED: state which SKIPPED condition applied.
For SHIP: "No substantive spec-drift found.">
```

**If REVISE:**
```
Spec-drift review: REVISE

## Findings

### Finding 1
- **Side A**: `{side_a_file}:{side_a_line}` — <quote or paraphrase of relevant text>
- **Side B**: `{side_b_file}:{side_b_line}` — <quote or paraphrase of relevant text>
- **Contradiction**: <1-2 sentence explanation of why these contradict>

### Finding 2
...
```

---

## Scope boundaries

- **Only flag substantive contradictions.** Do NOT flag:
  - Style differences (capitalization, punctuation, line length)
  - Incomplete or imprecise documentation that is not actively contradicted
  - Missing documentation for internal helpers or private functions
  - Pre-existing drift not introduced by this diff
- **Do not flag "no tests added."** Test coverage is the worker's responsibility, not spec-drift's.
- **Only one verdict token per run.** Write exactly `SHIP`, `REVISE`, or `SKIPPED` to `spec-drift-result.txt` — no punctuation, no newline after.

---

## Rules

- **Read-only.** You cannot edit, commit, or push files.
- **Do not use `run_in_background` on any command.** Run all commands in the foreground.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- No `$()` command substitution. No heredocs. No `python3 -c`. No quoted strings with `&&` or `;`.
