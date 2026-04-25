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

#### Dispatcher daemon venv

`scripts/dispatcher/` has no `pyproject.toml`, so `scripts/install-package-venv.sh` cannot bootstrap its venv. Use the dedicated helper instead:

```
scripts/install-dispatcher-venv.sh
```

This creates `scripts/dispatcher/.venv` and installs `pytest`, `ruff`, `boto3`, and an editable `packages/judgemind-config` — the four dependencies required by the dispatcher test suite. The script is idempotent: re-running it on an existing venv is safe.

**One-off and permanent markers.** **Every** top-level script must carry exactly one of the following markers, as a standalone top-level comment anywhere in the **first 50 lines** of the file (the header comment block — the marker sits adjacent to the `# venv:` header, typically just before or after the module docstring):

- `# one-off: true` — finite-lifetime script (backfills, cleanups, fixups) tied to a specific bug fix or migration. Candidate for archival to `scripts/archive/` once its work is done.
- `# permanent: true` — re-runnable utility (parameterizable, idempotent, intended to be invoked repeatedly). Exempt from one-off staleness checks.

```python
#!/usr/bin/env python3
"""Backfill missing party names for Santa Barbara rulings."""
# venv: scraper-framework
# one-off: true
from __future__ import annotations
```

The 50-line window is enforced by `scripts/check-script-headers.py` (CI job `script-headers-check`, and `.githooks/pre-push`). The window accommodates long module docstrings — e.g. `scripts/backfill_llm_enrichment.py` has a 29-line docstring and carries `# permanent: true` on line 32, which the old "first 10 lines" rule-of-thumb incorrectly flagged (see #2533). If a marker would otherwise land past line 50, shorten the docstring rather than expanding the window.

Historical note: the original (#2533) convention only required a marker on scripts whose filename matched `backfill`, `cleanup`, `fix`, `dedup`, `merge`, `migrate`, or `remediat`. #2547 extended the requirement to ALL top-level scripts so the `/audit` skill (§1.9) can use `permanent_count + 5` as a self-adjusting `scripts/*.py` count threshold — a new permanent utility raises the ceiling automatically; a new one-off consumes a slot of headroom. Run `scripts/check-script-headers.py --count` to see the current marker breakdown. The historical narrow scan is still available via `scripts/check-script-headers.py --narrow` for callers that want the #2533 behaviour.

The `archive/`, `eval/`, `tests/`, and `spotcheck/` subdirectories under `scripts/` are excluded from the scan.

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

## S3 write discipline

Rules that apply to every script or service that writes or copies objects under the `raw/` prefix in S3:

- **All raw S3 writes go through `S3Archiver.archive()`**, which asserts `sha256(bytes) == filename_hash` before the PUT. Never call `s3_client.put_object()` directly for `raw/` objects.
- **Any new script that copies, re-keys, or migrates S3 objects under `raw/`** must call `verify_key_matches_bytes(s3_client, bucket, key)` on both source AND destination after the copy. Reference: lessons from #2638 / #2663.
- **Never derive an S3 key from a DB column** without also re-hashing the bytes you intend to write under that key. The DB value may be stale or wrong; only the bytes are authoritative.

Helpers are in `packages/scraper-framework/src/framework/s3_integrity.py` — use `verify_key_matches_bytes` for a boolean check and `assert_key_matches_bytes` for a hard assertion (raises `S3MislabelError` on any mismatch or missing object).

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
- Pre-push only enforces the floor when `coverage/lcov.info` (or `coverage.xml`) is newer than every source file under `src/` and `tests/`. A scoped `npm test -- --coverage <files>` produces a stale-relative-to-source report; pre-push detects this and emits a warning instead of failing — re-run the full coverage command before pushing.

### TypeScript packages

```
npm run lint         # ESLint
npm run typecheck    # tsc --noEmit
npm test             # Vitest
```

For `packages/web/`, also run `npm run build`. The same diff coverage and floor ratchet gates apply to TypeScript packages (CI reads `lcov.info`).

`packages/web/` also has `npm run check` — covers hardcoded-colors, apollo-keyfields, and graphql-queries, the CI hygiene guards that fail fast. Run it before pushing large frontend changes.

### Terraform (from `infra/terraform/`)

```
terraform fmt -check -recursive
terraform init -backend=false -lockfile=readonly
terraform validate
```

**`.terraform.lock.hcl` churn hazard — always use `-lockfile=readonly` for agent-side validation.** `terraform init` (with or without `-backend=false`) will silently rewrite the environment's `.terraform.lock.hcl` to match the providers the current module set references. If the lock file contains providers that the environment does not currently reference (for example `hashicorp/archive` in `environments/dev/` when no `.tf` in that environment uses the `archive` provider), `init` "helpfully" **prunes** those provider hash blocks from the lock file. The pruned lock file then appears as an unrelated modification in `git status`, and if committed can either (a) pollute the PR with unrelated diff noise or (b) cause a hash mismatch when a different environment that does reference the provider runs `init`. See #2582.

The `-lockfile=readonly` flag tells `terraform init` to fail fast if it would modify the lock file, rather than silently rewriting it. Use it for every pre-PR validation. Only omit the flag when you are intentionally adding or upgrading a provider in the current environment — in that case the lock-file change is the point, and it should be staged and committed together with the corresponding `main.tf` / `terraform.tf` change.

If you run `terraform init` without the flag by mistake and see `.terraform.lock.hcl` in `git status`, revert it with `git checkout -- infra/terraform/environments/<env>/.terraform.lock.hcl` before committing anything else.

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

### Interpreting mergeStateStatus (UNSTABLE-but-green)

GitHub's `mergeStateStatus` field — exposed by `gh pr view --json mergeStateStatus` and `mcp__github__get_pull_request` — is derived from the *entire history* of check runs on the PR's head SHA, not just the latest attempt per workflow. One stale failed run from a previous CI attempt is enough to keep a PR flagged `UNSTABLE` even after a successful rerun on the same SHA flips the latest rollup green.

**Symptom:** `gh pr view <N> --json mergeable,mergeStateStatus` returns `{"mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE"}` yet every currently-displayed check on the PR page is green. Common trigger: the first run on the SHA hit a transient GitHub Actions flake (e.g. `detect-changes` timeout), you reran that workflow on the same SHA, and the rerun succeeded. Both runs' check runs are attached to the SHA; GitHub surfaces the old failed run in the mergeStateStatus calculation forever.

**Correct merge gate** (use this, not `mergeStateStatus == CLEAN`):

1. `mergeable == MERGEABLE` (GitHub can compute a merge commit — no conflicts), AND
2. The **required** status check — `ci-passed` on this repo — has `conclusion: SUCCESS` on its latest run, AND
3. No *latest* non-required check has `conclusion: FAILURE` (a failed required-ish check on the current SHA — distinct from a stale failed run already addressed by a rerun).

`SKIPPED` is fine. `CANCELLED` on a non-required check is usually fine — it frequently comes from a Vercel / smoke-test concurrency guard superseding an older deploy (`Canceling since a higher priority waiting request exists`) and doesn't reflect a real failure. `UNSTABLE` with gates (1)-(3) satisfied is safe to merge.

**One-line recipe:**

```
gh pr view <N> --repo judgemind/judgemind \
  --json mergeable,statusCheckRollup \
  --jq '{mergeable, rollup: [.statusCheckRollup[] | {name, conclusion}]}'
```

Scan the output: `mergeable` should be `MERGEABLE`, `ci-passed` should be `SUCCESS`, and nothing should be `FAILURE`. `CANCELLED` on Vercel/Smoke-Test-style checks can be ignored if the current SHA has no corresponding failure from the same workflow. If so, merge — regardless of `mergeStateStatus`.

**Incident example (#3099):** PR #3095's first CI attempt (run `24847629910`) failed on the `detect-changes` job — a transient GitHub Actions flake. The rerun on the same SHA (run `24847660775`) succeeded with every job green. `mergeable` was `MERGEABLE`, `ci-passed` was `SUCCESS`, no latest rollup conclusion was `FAILURE`, but `mergeStateStatus` stayed `UNSTABLE` because the old failed `detect-changes` check run was still attached to the SHA. The PR was safe to merge under the gate above.

The `/task` skill's §A.7 merge step uses this gate — see `.claude/skills/task/SKILL.md`.

### Database migrations — schema drift

When any file under `packages/api/migrations/` changes, regenerate `packages/api/src/data-access/schema.sql`:

```
scripts/regenerate_schema.sh
```

Then commit the updated `schema.sql` alongside the migration. `schema.sql` is auto-generated — do not edit it directly. If you prefer to verify before regenerating, run `scripts/check_schema_drift.sh` which compares `schema.sql` against a fresh migration-applied schema and exits non-zero on drift.

The same check runs in CI as the `schema-drift-check` job. The pre-push hook runs it whenever a `packages/api/migrations/*.sql` file is in the push (requires Docker + a running daemon; emits a WARNING and skips if Docker is unavailable), so migration-vs-schema drift is caught locally before the ~10 minute CI round trip. See #2702.

### macOS bash 3.2 compatibility

Operator laptops run macOS, which ships with bash 3.2.57 (Apple has frozen the OS bash at 3.2 since 2007 for GPLv3 licensing reasons). Shell scripts in this repo — especially `scripts/check-*.sh` hygiene guards and `scripts/tests/*.sh` unit tests — are run both from CI (ubuntu-latest, bash 5+) and from operator shells. A script that uses a bash 4+ feature passes CI but fails locally with a cryptic message such as `mapfile: command not found` (exit 127) or `bad substitution`.

**`scripts/check-bash-compat.sh` enforces this**, scanning every `scripts/**/*.sh` file for the forbidden constructs below and exiting non-zero on a match. It is wired into the `scripts-tests` CI job (same path filter as the other hygiene checks). Comment lines are exempted, so prose referencing a forbidden token (e.g., "we avoid `mapfile` because...") is fine.

#### `scripts-tests` matrix shards

The `scripts-tests` CI job runs two parallel matrix shards to keep wall-clock time under 600 s (issue #3307):

- **`python` shard** — both `pytest` suites (`scripts/tests/` and `scripts/dispatcher/tests/`) plus all 11 inline shell hygiene guards (`check-aws-bool-flags.sh`, `check-bash-compat.sh`, etc.). The dispatcher daemon suite uses `pytest-xdist` (`-n auto`) because all tests are `monkeypatch`/`tmp_path`-based and contain no `os.chdir` calls. Estimated wall-clock ~200 s.
- **`shell` shard** — `scripts/run-scripts-tests.sh` only (60 shell tests auto-discovered under `scripts/tests/*.sh`). Dominated by `test_agent_runner_entrypoint.sh` (~275 s) and `test_check_dispatcher_image_versions.sh` (~58 s). Estimated wall-clock ~395 s.

Both shards inherit the same `needs: detect-changes` / `scripts == 'true'` path filter. GH Actions aggregates the two matrix expansions under the single `scripts-tests` name, so the `ci-passed` `needs:` list does not need updating.

**Forbidden constructs** (each has a bash 3.2-compatible rewrite):

| Construct | Bash 3.2 behaviour | Use instead |
|-----------|-------------------|-------------|
| `mapfile` / `readarray` (bash 4+ builtin) | `command not found` → exit 127 | `while IFS= read -r line; do arr+=("$line"); done < <(cmd)` |
| `declare -A` / `typeset -A` (bash 4+ associative arrays) | `declare: -A: invalid option` | Parallel indexed arrays, or namespaced variables (`var_foo`, `var_bar`) |
| `declare -g` (bash 4.2+ global from function) | `declare: -g: invalid option` | Plain assignment at global scope, or `VAR=$(fn)` at caller |
| `${var,,}` / `${var,}` / `${var^^}` / `${var^}` (bash 4+ case conversion) | `bad substitution` | `tr '[:upper:]' '[:lower:]'` (or the reverse) |
| `local -n` / `declare -n` / `typeset -n` (bash 4.3+ namerefs) | `local: -n: invalid option` | Pass by value and return via stdout: `r=$(fn "$x")` |
| `;;&` in `case` (bash 4+ fall-through) | `syntax error near unexpected token &` | Separate `if` arms, or repeat the body |
| `\|&` pipe shorthand (bash 4+ pipe-both-streams) | `syntax error near unexpected token &` | `2>&1 \|` (more explicit and bash 3.2+ compatible) |

**Not forbidden** (these work on bash 3.2):
- `echo -e` — bash 3.2's `echo` builtin supports `-e`.
- Process substitution `< <(...)` — bash 3.2 supports it.
- `[[ ... ]]` conditionals, `$(...)` command substitution, indexed arrays — all bash 2+.

**Historical context.** Two earlier check scripts (`scripts/check-admin-dispatcher-brand-accent.sh`, `scripts/check-no-inline-ecs-healthcheck.sh`) and the `scripts/check-terminal-routing-comments.sh` guard each carry inline comments explaining why they avoid `mapfile` — the convention existed as tribal knowledge for months. PR #3081's first draft still used `mapfile -t` and silently exited 127 on the operator's laptop; the author found the precedent only by grepping peer check scripts. Issue #3082 codified the convention into `check-bash-compat.sh` + this docs section.

### Hygiene-check CI steps

When wiring a `scripts/check-no-*.sh`, `scripts/check-forbidden-*.sh`, or `scripts/check-deprecated-*.sh` guard into `.github/workflows/ci.yml`, **do not quote the forbidden string in the step's `name:` field** — the quoted pattern itself will trip the guard on the next CI run (see #2541/#2542 for the specific incident). Name the step after what the check *does* instead of what it *forbids* (e.g., name it after the replacement tool or the category of misuse, not the literal string).

The peer guard tests under `scripts/tests/test_check_*.sh` include a self-match assertion (see `scripts/tests/_guard_self_match_helpers.sh`) that catches this at test time. When adding a new string-forbidding guard, add an `assert_no_self_match_on_ci_step_name` call to its test.

### Subagent responsibilities

Subagents MUST install dependencies, run ALL lint/format/test commands for every package touched, fix failures before committing, and only push after all local checks pass.
