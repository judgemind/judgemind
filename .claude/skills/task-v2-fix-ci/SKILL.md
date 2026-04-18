---
description: (WIP — dispatcher v2 spike 0.3 stub) Fix-CI phase for the per-phase /task-v2 pipeline. Reads CI failure logs + PR diff, produces a patch commit OR an explicit blocker signal.
argument-hint: ""
maxTurns: 50
model: opus
---

# /task-v2-fix-ci skill (WIP stub)

**Status:** WIP — extracted from `.claude/skills/task/SKILL.md` A.5/A.7 (ci-fix loop) for dispatcher v2 spike 0.3.

**Goal:** Read failing CI logs from a PR, diagnose the failure, apply a fix in the worktree, and return. The daemon handles committing and pushing; this skill just produces the patch.

**Input:** `{worktree}/tmp/dispatcher-input/fix-ci.json`:
- `pr_number` (int)
- `branch` (str)
- `failing_jobs` (list of `{name, conclusion, log_tail}` objects — log_tail is last ~200 lines of each failing job)
- `git_diff_base_to_head` (str) — full diff for context
- `worktree_path` (str)

**Output:** `{worktree}/tmp/dispatcher-output/fix-ci.json`:
- `verdict` (str) — `PATCHED`, `BLOCKED`, or `FLAKY`
- `changed_files` (list of paths) — if verdict=PATCHED
- `commit_message` (str) — if verdict=PATCHED
- `block_reason` (str or null) — if verdict=BLOCKED, the specific failure the agent can't fix (e.g., "missing secret", "infrastructure error outside repo control")
- `flaky_evidence` (str or null) — if verdict=FLAKY, reasoning (e.g., "intermittent timeout in network call, no code change needed — rerun")

---

## Step 1 — Categorize the failure

For each failing job, classify the root cause:
- **Lint / format** — pattern: `ruff check` errors, `prettier --check` errors. Fix by running the formatter locally (`ruff check --fix`, `ruff format`, `npm run format`).
- **Type error** — pattern: mypy/pyright/tsc errors. Fix by adjusting types or imports.
- **Test failure** — pattern: `FAILED tests/...`, specific assertion traces. Fix by correcting code or updating test expectations (prefer correcting code).
- **Coverage floor** — pattern: `Coverage X% < floor Y%`. Fix by adding tests for uncovered lines.
- **Infra / external** — pattern: timeouts in network calls, AWS errors, GitHub API 5xx. Classify as FLAKY unless the code under test is demonstrably wrong.
- **Missing secret / config** — pattern: `Error: KEY is not set`. Classify as BLOCKED (daemon or human must add the secret).

## Step 2 — Apply the fix

For fixable categories:
1. Read the specific file(s) referenced in the failure.
2. Apply the smallest patch that resolves the issue.
3. Run the same check locally if feasible (`ruff check`, `pytest path/to/test.py::TestClass::test_method`).
4. Verify the fix resolves the root cause, not just the symptom.

Do NOT disable tests or mask errors (e.g. `# noqa`, `# type: ignore`, `pytest.skip`) unless the test itself is wrong AND you can explain why. Prefer fixing the underlying code.

## Step 3 — Write output JSON

For verdict=PATCHED:
- `changed_files`: the files you edited.
- `commit_message`: conventional-commits format, `fix(<area>): <what was fixed> — CI (#<PR-N>)`.

For verdict=BLOCKED:
- Write a concrete `block_reason` that a human can act on in one step.

For verdict=FLAKY:
- Write `flaky_evidence` describing why this is transient. The daemon will rerun the job once; if it fails again, escalate to human review.

## Reminders

- Do not edit CI config (`.github/workflows/`) unless the failure is genuinely in CI config itself. Prefer fixing the underlying code that CI caught.
- Do not push, commit, or call `gh` — the daemon handles all git and GitHub operations.
- No `$()`, no heredocs. Output is the JSON file only.
