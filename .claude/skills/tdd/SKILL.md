---
description: Test-driven implementation for code tasks — write failing tests first, implement until green. Use after /task has claimed an issue. Not for Terraform, DB migrations, docs, or investigation tasks.
argument-hint: ""
---

# /tdd skill

Implement the current task using test-driven development. This skill assumes you are already in a worktree with a claimed issue (use `/task` first to set up).

**When to use this skill:** Python packages (scrapers, API logic, NLP pipeline) and TypeScript packages (API, frontend) — any task where you can write and run tests locally.

**When NOT to use this skill:** Terraform/infrastructure changes, database migrations, CI/CD pipeline work, documentation, investigation tasks, or anything that requires external services (databases, deployed APIs) to test. For those tasks, follow the standard PR workflow in CLAUDE.md directly.

Do not ask for confirmation. Work autonomously through every step.

**This skill covers the implementation phase only** (CLAUDE.md substep 4.3). After completing /tdd, return to the caller (/task or manual workflow) for the commit, push, PR, and CI steps.

---

## Step 1 — Create todo list for TDD steps

Create todos using `TaskCreate` to track the TDD cycle:

1. "Understand requirements" (activeForm: "Reading requirements")
2. "Write failing tests" (activeForm: "Writing tests")
3. "Implement minimum code" (activeForm: "Implementing")
4. "Iterate until green" (activeForm: "Fixing test failures")
5. "Run all pre-PR checks" (activeForm: "Running pre-PR checks")

Mark each todo `in_progress` when starting and `completed` when done.

---

## Step 2 — Understand the Requirements

Read the issue body thoroughly. Check `docs/specs/` for relevant guidance. Examine existing code and test patterns in the packages you will modify.

Identify:
- **Acceptance criteria**: What must be true when this is done?
- **Edge cases**: Court data is messy — document known edge cases.
- **Test boundaries**: What can be tested with unit tests vs. integration tests? If a task requires external services (DB, S3, etc.) for meaningful tests, mock them or use existing test fixtures.

---

## Step 3 — Write Failing Tests First

Before writing any implementation code, create comprehensive tests that define the acceptance criteria:

- **Unit tests** for each logical function or method.
- **Integration tests** for end-to-end behavior (API endpoints, scraper pipelines, etc.). Use mocks for external dependencies (databases, HTTP calls, S3).
- **Edge case tests** for known quirks (empty inputs, malformed data, encoding issues).
- **Regression tests** for scrapers: use archived fixtures from `tests/fixtures/`.

Run the tests to confirm they fail:
```
# Python
.venv/bin/pytest tests/ -v --tb=short

# TypeScript
npm test
```

All new tests MUST fail at this point. If any pass, your tests aren't testing the new behavior — fix them.

---

## Step 4 — Implement Minimum Code to Pass

Write the minimum implementation to make all tests pass. Do not over-engineer — if three lines work, don't write a framework.

Guidelines:
- Follow existing code patterns in the package.
- Type hints on all Python function signatures.
- Strict TypeScript mode.
- No hardcoded secrets or URLs.

---

## Step 5 — Iterate Until Green

Run the full test suite (not just new tests):

```
# Python
.venv/bin/pytest tests/ -v --tb=short

# TypeScript
npm test
```

If any test fails:
1. Analyze the failure.
2. Fix the implementation (not the test, unless the test itself is wrong).
3. Re-run the full suite.

**Max 5 implementation-test cycles.** If you cannot get green after 5 attempts, stop and comment on the issue describing what's failing and why. Add `status/blocked` and request human input.

---

## Step 6 — Run All Pre-PR Checks

Run every applicable check for every package you touched:

**Python:**
```
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/pytest tests/ -v --tb=short
```

**TypeScript:**
```
npm run lint
npm run typecheck
npm test
```

Fix any failures. Auto-fix lint with `.venv/bin/ruff check --fix src/ tests/` then `.venv/bin/ruff format src/ tests/`.

---

## Step 7 — Done — Return to Caller

All checks pass. The implementation phase is complete. **Do not commit, push, or open a PR from this skill.** Return control to the calling workflow (`/task` Path A, or the manual PR workflow in CLAUDE.md) which handles commit, push, PR creation, CI monitoring, and cleanup.

---

## Guardrails

- **Never modify existing tests without flagging it.** If you need to change an existing test, add a comment in the PR body explaining why.
- **Always run the full suite**, not just new tests. Regressions in existing tests are bugs.
- **Max 5 implementation cycles.** Escalate to human after 5 failures.
- **Test coverage**: every new public function/method must have at least one test.

---

## Reminders

- **No `$()` in any Bash command.** Use separate tool calls for dynamic values.
- **No quoted strings with `&&` or `;`.** Split into separate tool calls.
- **All temp files go in `{worktree}/tmp/`**, not `/tmp/`.
- **Always Read before Write** for existing files.
