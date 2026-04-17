# Code Standards & Pre-PR Checks

Agent-facing reference for code style, testing, and the local checks that must pass before pushing. CLAUDE.md contains a short summary; this doc has the detail.

## Python (scrapers, NLP pipeline, API tooling)

- Python 3.12+, using `.venv` in each package directory.
- Run tests: `.venv/bin/pytest tests/ -v`
- Install deps: `.venv/bin/pip install -e ".[dev]"`
- Type hints on all function signatures.
- pytest for testing; ruff for linting and formatting.
- Dependencies managed via `pyproject.toml`.
- Async where appropriate (httpx for HTTP, playwright for browser automation).

### Python scripts (`scripts/*.py`)

Scripts that import non-stdlib modules must declare their venv with a `# venv:` header comment in the first 10 lines:

```python
#!/usr/bin/env python3
"""Script docstring."""
# venv: scraper-framework
from __future__ import annotations
```

Run scripts via `scripts/run-py.sh scripts/<name>.py` — it reads the header and activates the correct venv automatically.

**One-off scripts** (backfills, cleanups, fixups) must also include a `# one-off: true` header. This marks them for automatic detection by the `/audit` skill so they can be archived when no longer needed.

```python
#!/usr/bin/env python3
"""Backfill missing party names for Santa Barbara rulings."""
# venv: scraper-framework
# one-off: true
from __future__ import annotations
```

Eval scripts (`scripts/eval/`) are excluded from this convention.

**ECS oneshot constraint:** Scripts run via `ecs-run-task.sh` are uploaded as single files — they **cannot import other `.py` files from `scripts/`**. Only stdlib and installed packages are available. If you need shared code, either inline it, use a lazy import inside a function (for optional features), or move the shared code into an installed package. CI enforces this via `scripts/check-oneshot-imports.sh`. Scripts that are never run as ECS oneshots can be added to the `LOCAL_ONLY` list in that script.

## TypeScript (API, frontend)

- Strict mode always.
- Node.js 20+ for API; activate with `source ~/.nvm/nvm.sh && nvm install 20 --no-progress`.
- Next.js 14+ for frontend.
- ESLint + Prettier.
- In `packages/web/src/app/`, use `@/` path aliases instead of deep relative imports (`../../` or deeper). The `local/prefer-path-alias` ESLint rule enforces this. Deep relative imports break when route groups are reorganised.
- **Follow `docs/web-patterns.md`** for all page layout, component usage, and consistency decisions. Mandatory for frontend work.
- **Apollo cache keyFields:** Any new GraphQL type without an `id` field needs a `keyFields` entry in `packages/web/src/lib/apollo-client.ts` `typePolicies`. Without this, Apollo may collapse distinct items into a single cache entry. Options: `keyFields: ['fieldName']` for types with a natural unique key, or `keyFields: false` for embedded/non-normalized types (edges, etc.). Run `scripts/check-apollo-keyfields.sh` locally to verify before pushing. See #1779.
- Jest or Vitest for testing.

## General

- All code must have tests. Scrapers must have regression tests against archived pages in `tests/fixtures/`.
- Never hardcode secrets, API keys, credentials, or URLs to live court sites in source code. Use environment variables.
- Never commit large binary files. Use `.gitignore`.
- Write clear docstrings/comments for non-obvious logic.
- **When removing or renaming module-level exports** (functions, classes, constants), grep for all import sites across the entire codebase (`src/` and `tests/`) before committing. Update or remove every import — not just the test file matching the modified module. Broken imports in unrelated test files will not surface until CI runs the full suite, and may not surface at all if the tests are skipped or filtered.

## Performance awareness

Every diff review must check for these common bottlenecks:

- **Sequential I/O over collections.** Use concurrency (`ThreadPoolExecutor`, `asyncio.gather`, `pipeline()`) or batching instead of per-item network calls.
- **O(n^2) pagination.** Use keyset (cursor-based) pagination, never `LIMIT/OFFSET` for large datasets.
- **Unbatched DB writes.** Use `executemany`, `COPY`, or psycopg3 `pipeline()` mode.
- **Missing connection reuse.** Reuse HTTP clients, DB connections, and S3 clients across calls.

If unsure whether a perf pattern matters at current scale, add a `# TODO(perf):` comment.

## Pre-PR Checks (MANDATORY — No Exceptions)

**Every agent MUST run ALL applicable checks locally BEFORE pushing.** The `.githooks/pre-push` hook also runs them automatically.

### Python packages (from the package directory)

```
.venv/bin/ruff check src/ tests/           # Lint (rules: E, F, I, N, UP, ANN, DTZ)
.venv/bin/ruff format --check src/ tests/  # Format check
.venv/bin/pytest tests/ -v --tb=short      # Tests with coverage
```

If lint fails: `.venv/bin/ruff check --fix src/ tests/` then `.venv/bin/ruff format src/ tests/`.

Common ruff pitfalls: **I001** (unsorted imports, `--fix` resolves), **F401** (unused imports, remove them), **UP017** (use `datetime.now(datetime.UTC)`). Format and lint are **separate commands** — run BOTH.

### Test assertion hygiene (enforced in CI)

Blind exception swallows inside test files hide bugs: if the call under test raises, the test silently passes on a never-executed code path (see #2443). CI enforces this via `scripts/check-test-except-pass.sh`, which forbids the following patterns inside any file under a `tests/` directory:

- `try: ... except: pass`
- `try: ... except Exception: pass`
- `try: ... except BaseException: pass`
- `try: ... except (Exception, ...): pass`

**Escape hatch.** If a swallow is truly necessary (e.g. best-effort teardown cleanup where the exception is irrelevant to the test), append `# noqa: BLE001` to the `except` line:

```python
try:
    shutil.rmtree(tmp)
except Exception:  # noqa: BLE001
    pass
```

Prefer narrowing the handler (`except FileNotFoundError:`) or logging the exception over the escape hatch — the goal is to keep blind swallows out of assertion paths.

### Coverage gates (enforced in CI)

- **Diff coverage:** new/changed lines must have >= 90% test coverage. CI runs `diff-cover` against `coverage.xml` (Python) or `lcov.info` (TypeScript).
- **Coverage floor ratchet:** overall package coverage must not decrease below the baseline in `coverage-baselines.json`. The floor only goes up — when coverage increases, update the baselines with `scripts/update-coverage-baselines.py`.

### TypeScript packages

```
npm run lint         # ESLint
npm run typecheck    # tsc --noEmit
npm test             # Vitest
```

For `packages/web/`, also run `npm run build`. The same diff coverage and floor ratchet gates apply to TypeScript packages (CI reads `lcov.info`).

### Terraform (from `infra/terraform/`)

```
terraform fmt -check -recursive
terraform init -backend=false
terraform validate
```

### Docs / Markdown

When any `.md` file changes (CLAUDE.md, docs, skill SKILL.md, package READMEs, etc.), run:

```
scripts/check-markdown-links.sh
```

This validates internal markdown pointers — both Markdown-style links and backtick references to repo-relative paths — resolve to files that exist. The same check runs in CI as the `markdown-links-check` job; the pre-push hook runs it on any `.md` in the push so failures are caught before the 2-3 minute CI round trip. If a backtick token that looks like a repo path is illustrative (not a real file), remove the backticks or rephrase so the checker does not flag it.

### CI workflow edits

When `.github/workflows/ci.yml` changes, run:

```
scripts/check-ci-job-skipped.sh
```

This detects the #2410 / #2505 footgun: a PR modifies a job body whose `if: needs.detect-changes.outputs.X == 'true'` gate's paths-filter does not match anything in the diff, so the job will be SKIPPED on that PR's own CI run and the modification is never actually exercised. The same check runs in CI as the `ci-job-skipped-check` job; the pre-push hook runs it whenever `ci.yml` is in the push. If it fails, either add `.github/workflows/ci.yml` to the offending filter (so the job always runs when ci.yml itself changes) or modify a file that already matches the filter.

### Subagent responsibilities

Subagents MUST install dependencies, run ALL lint/format/test commands for every package touched, fix failures before committing, and only push after all local checks pass.
