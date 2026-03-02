# Judgemind — Agent Instructions

Read this file before starting any task. It defines how you work in this codebase.

## Project Context

Judgemind is a free, open-source legal research platform replacing Trellis.law. Read the specs in `docs/specs/` for full context. The key things to know:

- **Tentative rulings are ephemeral.** If a scraper is down, data is permanently lost. Scraper reliability is the highest priority in the system.
- **Two data pipelines.** Model A captures California tentative rulings (ephemeral, high urgency). Model B extracts judge analytics from dockets/documents (all other states, persistent data, NLP-dependent).
- **Self-funded and free.** Every architecture decision must consider cost. Prefer fixed-cost over usage-based. Never assume unlimited budget.
- **API-first.** The web app is a client of the API. Every UI feature has an API endpoint.

## Before Starting Any Task

1. Read the GitHub Issue thoroughly, including linked issues and documents.
2. Check `docs/specs/` for relevant guidance (product spec, architecture spec, investigation reports).
3. Look at existing code for patterns. Be consistent with what's already there.
4. If the task is unclear or you need a decision from the maintainer, comment on the issue explaining what you need, label it `status/blocked`, and move to another task. Do not guess on ambiguous requirements.

## Code Standards

### Python (scrapers, NLP pipeline)
- Python 3.12+
- Type hints on all function signatures
- pytest for testing
- ruff for linting and formatting
- Dependencies managed via pyproject.toml
- Async where appropriate (httpx for HTTP, playwright for browser automation)

### TypeScript (API, frontend)
- Strict mode always
- Node.js 20+ for API
- Next.js 14+ for frontend
- ESLint + Prettier
- Jest or Vitest for testing

### General
- All code must have tests. Scrapers must have regression tests against archived pages in `tests/fixtures/`.
- Never hardcode secrets, API keys, credentials, or URLs to live court sites in source code. Use environment variables.
- Never commit large binary files. Use `.gitignore`.
- Write clear docstrings/comments for non-obvious logic. Court data has many edge cases — document them.

## Git Workflow

- Branch from `main`: `feat/issue-{N}-short-description` or `fix/issue-{N}-short-description`
- Commit messages follow conventional commits: `feat(scraping): implement OC PDF link scraper (#42)`
- One PR per task. Link the issue in the PR description with `Closes #42` or `Relates to #42`.
- PRs must pass CI before requesting review.
- Never merge your own PRs. Human review is required for all merges.
- Never push directly to `main`.

## Creating Sub-Tasks

If a task naturally breaks into 2+ independent pieces of work, create child issues:

- Each child issue must follow the issue template.
- Reference the parent: "Parent: #42" in the issue body.
- Sub-tasks should be self-contained — another agent should be able to pick one up independently.
- Label child issues appropriately (area, priority, type).
- Add `agent/ready` label if the sub-task is fully specified and ready for work.

## Investigation Tasks

Investigation tasks produce documentation, not code:

- Write findings directly in the issue body or as a markdown file in `docs/investigations/`.
- Always end with: recommended next steps, sub-tasks to create, and decisions that need human input.
- Be specific about what you found and what you couldn't determine.

## Scraper Development Rules

- **Never scrape live court websites from your development environment.** Use archived test fixtures in `tests/fixtures/`. Production scraping runs only from deployed infrastructure.
- Every scraper must implement the base `Scraper` class from the framework.
- Every scraper must report health metrics after each run.
- Every captured document gets a SHA-256 content hash for version tracking.
- Raw content is always archived to object storage before any processing.
- Scraper configurations (URLs, selectors, schedules) are separate from scraper logic.

## Infrastructure Code

- Terraform for all AWS resources. No clicking in the console.
- Every resource must be in a module.
- Use variables for anything environment-specific (instance sizes, counts, etc.).
- Tag all resources with `project=judgemind` and `environment={dev|staging|production}`.
- Never commit AWS credentials or state files. Use remote state in S3.

## Things You Must Not Do

- Do not merge PRs.
- Do not deploy to production.
- Do not make architectural decisions that contradict the specs without flagging them as `type/decision` issues.
- Do not scrape live court websites during development.
- Do not store secrets in code, config files, or commit history.
- Do not add dependencies without justification in the PR description.
