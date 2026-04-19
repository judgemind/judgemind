---
description: Fix-CI phase for the per-phase /task-v2 pipeline. Reads CI failure logs and the PR diff, produces a patch commit OR an explicit blocker signal.
argument-hint: "<agent-id>"
maxTurns: 100
model: sonnet
---

# /task-v2-fix-ci skill

Fix-CI phase for the dispatcher v2 per-phase task pipeline (`docs/specs/dispatcher-v2-spec.md` §6a). Invoked by the daemon when `gh run watch` comes back red on a PR opened by the summary phase. Reads the failure logs, diagnoses the root cause, applies a targeted patch, and returns — OR returns an explicit BLOCKED/FLAKY signal so the daemon can route accordingly.

**Prerequisites:** The dispatcher daemon has already (a) detected CI failure on the PR, (b) extracted the log tails from each failing job, (c) written the input bundle to `{worktree}/tmp/dispatcher-input/fix-ci.json`.

**Goal:** Produce `{worktree}/tmp/dispatcher-output/fix-ci.json` with verdict=`PATCHED` (fix applied to working tree), `BLOCKED` (agent cannot fix — human needed), or `FLAKY` (rerun without code change).

**IMPORTANT — No backgrounding.** Do not use `run_in_background` on any Bash command, Agent tool call, or any other operation.

**IMPORTANT — Smallest patch principle.** Apply the smallest patch that resolves the root cause. Do not refactor, do not rename, do not add defensive code unrelated to the failure. CI-fix commits are separately mergeable and must be easy to review — they accrete on top of the original summary commit.

---

## Input contract

Read `{worktree}/tmp/dispatcher-input/fix-ci.json`. Required fields:

- `agent_id` (str).
- `issue_number` (int).
- `pr_number` (int).
- `branch` (str).
- `failing_jobs` (list of `{name, conclusion, log_tail}`) — `log_tail` is the last ~200 lines of each failing job. The daemon uses `gh run view <id> --log-failed --job <job-id>` or the MCP equivalent.
- `git_diff_base_to_head` (str) — full unified diff of the PR (so fix-ci can see what ralph changed).
- `worktree_path` (str).
- `repo_root` (str).
- `previous_fix_attempts` (int) — how many times `/task-v2-fix-ci` has already run on this PR. 0 for first attempt.

Optional:

- `change_type` (str) — from plan output; helps categorize expected failure modes.

If the file is missing or malformed, exit 0 with verdict=`BLOCKED, block_reason="input JSON missing or malformed"`.

---

## Output contract

Write `{worktree}/tmp/dispatcher-output/fix-ci.json`:

```
{
  "agent_id": "<echo>",
  "pr_number": <int>,
  "verdict": "PATCHED" | "BLOCKED" | "FLAKY",
  "failure_category": "lint" | "format" | "type_error" | "test_failure" | "coverage_floor" |
                       "infra_external" | "missing_secret_or_config" | "build_failure" |
                       "markdown_links" | "hygiene_guard" | "ci_config" | "other",
  "changed_files": ["<path>", ...],
  "commit_message": "fix(<area>): <what was fixed> — CI (#<PR-N>)",
  "block_reason": null | "<string>",
  "flaky_evidence": null | "<string>",
  "notes": "<optional prose for the retro phase>"
}
```

- `PATCHED` — the worktree working tree contains the fix. `changed_files` and `commit_message` are populated. Daemon stages, commits, and pushes.
- `BLOCKED` — the agent cannot fix this failure (missing secret, repo permissions, external infra beyond repo control, or fix-attempts exhausted). `block_reason` is populated with a concrete one-step human action. Daemon escalates to `status/needs-human` + `priority/p1`.
- `FLAKY` — transient failure (intermittent timeout, AWS 5xx, etc.) with no code change needed. `flaky_evidence` explains. Daemon reruns the job once; second flaky → escalate.

Always exit 0. Verdict comes from the JSON, not the exit code.

---

## Step 1 — Categorize the failure

For each `failing_jobs` entry, classify the root cause from the `log_tail`:

| Category | Pattern / signal | Fix path |
|---|---|---|
| `lint` | `ruff check` errors (e.g. `E501`, `F401`), ESLint errors | Run `ruff check --fix <files>` + `ruff format <files>`; for TS run `npm --prefix packages/web run lint -- --fix` |
| `format` | `ruff format --check` reports diffs, Prettier complains | Run the formatter; commit the result |
| `type_error` | mypy/pyright/tsc errors | Fix the types or imports; never add `# type: ignore` unless the type system is wrong AND you can justify |
| `test_failure` | `FAILED tests/...`, assertion traces | Read the failing test, read the code under test, apply the smallest fix |
| `coverage_floor` | `Coverage X% < floor Y%` or "diff coverage below 90%" | Add targeted tests for uncovered lines in the PR's diff |
| `infra_external` | timeouts to `api.anthropic.com`, AWS 5xx, GitHub API 5xx, DNS errors | Classify as `FLAKY` |
| `missing_secret_or_config` | `Error: <KEY> is not set`, `Secret "<name>" not found` | Classify as `BLOCKED` — the daemon or human must provision the secret |
| `build_failure` | TypeScript build errors, missing module, webpack/next build fail | Read the error, fix the import/path/type |
| `markdown_links` | `scripts/check-markdown-links.sh` failures | Fix the broken link paths |
| `hygiene_guard` | `scripts/check-no-*.sh` / `check-forbidden-*.sh` / `check-deprecated-*.sh` guards, or `scripts/check-ci-job-skipped.sh` (the #2410/#2505 footgun) | Follow the guard's error message; per CLAUDE.md, self-matching names in ci.yml are the usual cause |
| `ci_config` | `.github/workflows/*.yml` parse error, job definition malformed | Edit the workflow — but only if the failure is genuinely in the workflow, not in code that the workflow runs |
| `other` | Not in the above | If you can't identify, classify as `BLOCKED` with a clear `block_reason` |

**Unsafe shortcuts — do not use:**

- `pytest.skip`, `@pytest.mark.skip`, or `xfail` on a failing test unless the test itself is wrong AND you can explain why in the commit message.
- `# noqa`, `# type: ignore`, `@ts-ignore`, `eslint-disable` unless the linter/type-checker is wrong AND you can explain why.
- Disabling a CI check by deleting its step or adding a conditional that skips it. Never.
- `--force` on pre-push hooks or safety scripts.

If a test is actually flaky (intermittent pass/fail with no code change), document that by marking FLAKY in the output — do NOT pre-emptively add retries or skip markers without filing a follow-up issue.

## Step 2 — Reproduce locally (when feasible)

For `test_failure` and `coverage_floor`, reproduce the failure in the worktree:

- `pytest tests/path/to/test.py::TestClass::test_method -x` — run the single failing test.
- `npm --prefix packages/web test -- --testPathPattern=<regex>` — narrow TS test run.
- `ruff check packages/<pkg>/src/path/to/file.py` — reproduce a single lint error.

Local reproduction confirms the fix before pushing. If the failure is environment-specific (only fails in CI), note that in `notes` and apply the fix based on the log analysis.

For `infra_external`, do NOT try to reproduce — the failure is transient by definition.

## Step 3 — Apply the fix

For fixable categories:

1. Read the specific file(s) referenced in the failure. Use the Read tool, not `cat`.
2. Apply the smallest patch that resolves the root cause. Prefer `Edit` over `Write` for existing files.
3. Run the same check locally if feasible. Confirm it now passes.
4. Verify the fix resolves the root cause, not just the symptom. Example: a test that asserts a field is non-null should be fixed by making the field non-null at its source, not by weakening the assertion.
5. Re-check related files for the same pattern. If the fix applies to sibling files (e.g., the same typo in three similar test fixtures), fix all of them in the same commit.

For `coverage_floor`:

- Read the diff of the PR to identify which lines are not covered.
- Add tests that exercise those lines. Prefer tests colocated with existing test files for the same module.
- Rerun coverage locally to confirm the floor passes.

For `hygiene_guard` failures in `.github/workflows/ci.yml` — the #2410/#2505 footgun is the most common cause. Follow the guard's own error message. If the guard is complaining about a job being SKIPPED on its own CI run, either modify a file that already matches the paths filter, or add `.github/workflows/ci.yml` to the filter.

## Step 4 — Verify the fix locally

Before writing the output JSON:

- Rerun the exact check that failed. Confirm it now passes.
- If multiple checks failed and you fixed a subset, note the unfixed ones in `notes` and keep verdict=`PATCHED` only if the remaining failures are FLAKY or subsequent to your fix.
- If your fix introduces new failures in other checks (rare, but possible), iterate until everything passes or escalate to BLOCKED with specifics.

## Step 5 — Write the output JSON

For verdict=`PATCHED`:

- `changed_files` — every file you edited.
- `commit_message` — conventional commits: `fix(<area>): <what was fixed> — CI (#<PR-N>)`. Keep the subject under 72 chars. Example: `fix(scraping): correct ruff E501 in orange_county.py — CI (#2733)`.

For verdict=`BLOCKED`:

- `block_reason` — a concrete one-step action a human can take. Examples:
  - `"Secret OPENAI_API_KEY not set in the ci environment — ops must provision via scripts/with-secret.sh or Terraform"`
  - `"GitHub Actions cache quota exhausted; rerun after cache eviction"`
  - `"Test hit a permission boundary that cannot be resolved from the agent — owner review needed"`

For verdict=`FLAKY`:

- `flaky_evidence` — explain why this is transient. Example: `"httpx.TimeoutError in packages/api/tests/test_claude_client.py — intermittent Anthropic API hiccup; retry recommended, no code change"`.

---

## What this skill does NOT do

- **Does not commit, push, or call `gh`.** Daemon handles git + GitHub operations after reading this output.
- **Does not edit CI workflow files unless the failure is genuinely in CI config.** Prefer fixing the code that CI caught.
- **Does not add retries, timeouts, or flaky-test decorators** without human approval — that hides real bugs.
- **Does not upgrade dependencies** as a "fix" for a test failure unless the failure is demonstrably caused by a known dependency bug with a filed upstream issue.
- **Does not touch .env, secrets, or credentials.** BLOCKED is the correct response to missing-secret failures.

## Escalation policy

- **Attempt 1** — try to fix. Verdict=`PATCHED` if successful.
- **Attempt 2** — if the first fix itself landed but didn't close all failures, try again. Same worktree.
- **Attempt 3+** — if `previous_fix_attempts >= 2` and the same failure recurs, the pattern suggests a misdiagnosis. Return verdict=`BLOCKED` with `block_reason="CI failure persisted after <N> fix attempts; needs human diagnosis"` and let the daemon escalate.

## Reminders

- No `$()`, no heredocs, no `python -c`. See `CLAUDE.md` Critical Rules.
- All temp files go in `{worktree}/tmp/`, never `/tmp/`.
- Prefer `Edit` over `Write` for existing files.
- Read the log_tail carefully — the real error is often a few lines above where pytest/CI prints the traceback summary.
- If the root cause is unclear from the log_tail alone, read the related source files before guessing.
