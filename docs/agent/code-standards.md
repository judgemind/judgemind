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

This creates `scripts/dispatcher/.venv` and installs `pytest`, `pytest-xdist`, `ruff`, `boto3`, and an editable `packages/judgemind-config` — the five dependencies required by the dispatcher test suite. The script is idempotent: re-running it on an existing venv is safe.

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

#### Logger configuration

Top-level `scripts/*.py` scripts that emit logger calls with `extra=` kwargs MUST configure logging via `configure_structlog`, not `logging.basicConfig`. The basicConfig idiom — `logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s")` — silently drops every `extra=` field from the LogRecord because the format string only references `%(asctime)s`, `%(levelname)s`, and `%(message)s`. Issue #4368 documented the production incident: a backfill script's `extra=` fields disappeared from CloudWatch Logs Insights, and the post-deploy verification that depended on those fields silently passed.

The canonical pattern is:

```python
#!/usr/bin/env python3
# venv: scraper-framework
# permanent: true
"""Module docstring."""
from __future__ import annotations

import logging

from framework.logging import configure_structlog  # noqa: E402

configure_structlog(json=True, stdlib_bridge=True)
logger = logging.getLogger(__name__)
```

`configure_structlog(json=True, stdlib_bridge=True)` routes stdlib `logging.getLogger(__name__)` calls through structlog's ProcessorFormatter + ExtraAdder, JSON-encoding the LogRecord plus its extras as one event per line. The `stdlib_bridge=True` flag is the load-bearing piece — without it, `extra=` fields still drop because structlog and stdlib `logging` remain unwired.

`scripts/drain_splitter_carry_forward_clusters.py` (PR #4368) is the reference implementation. PR #4373 migrated the other 13 affected `scripts/*.py` files. The `no-basicconfig-with-extra-check` CI job (`scripts/check-no-basicconfig-with-extra.sh`, #4376) enforces the contract: it AST-walks every top-level `scripts/*.py` and fails CI when a file calls `logging.basicConfig(...)` AND passes `extra=` to a logger method AND does NOT also call `configure_structlog(...)`. Files that call both `basicConfig` and `configure_structlog` are accepted — the contributor has been deliberate about routing.

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

## Per-county pre-LLM splitters — use the scaffolder

When adding a multi-case ruling splitter for a new county (the pattern shipped in #2447 / #2450 / #3534 / #3649 / #4304 / #4303), use the scaffolder instead of copy-pasting from a prior PR:

```
scripts/scaffold_pre_llm_splitter.py --county "<County Name>" --format pdf
```

The scaffolder generates:

1. `packages/scraper-framework/src/courts/ca/<slug>_tentatives.py` — the `SplitRuling` dataclass + `_split_rulings` skeleton with placeholder regex.
2. `packages/scraper-framework/src/ingestion/worker.py` — the `_try_<slug>_<format>_split` function and dispatch wiring in `_llm_split_document`.
3. `packages/scraper-framework/tests/courts/test_<slug>_tentatives.py` — placeholder unit tests.
4. `packages/scraper-framework/tests/test_ingestion_worker.py` — `_make_<slug>_event` + `_make_fake_<slug>_rulings` fixtures and the dispatch contract test class (seven canonical cases — gate negatives, fall-through, dispatch contract, exhaustion handling).
5. `scripts/check_split_ruling_fields_propagated.py` — the `_DATACLASS_SCOPE` entry so the propagation guard accepts the new dataclass.

The contributor still has to fill in the county-specific regex (entry boundary + case_number shape) and replace the `xfail` placeholder test with a real fixture-driven test once a representative document is captured. The structural code (worker function, gate, dispatch contract, fake_rulings helper, scope entry, seven canonical dispatch tests) is generated correctly by construction.

Use `--dry-run` to preview the diff without writing. The scaffolder is idempotent — re-running on a county that already has a registered splitter is a no-op.

If the county module already exists (because the scraper for that county already lives in `<slug>_tentatives.py`), the scaffolder appends the `SplitRuling` + `_split_rulings` block instead of overwriting. Same shape for the per-county test file.

After scaffolding, run the standard pre-PR checks from `packages/scraper-framework/`:

```
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest tests/ -k '<slug>'
```

And from the repo root: `scripts/check-split-ruling-fields-propagated.sh`. Tracking issue: #4316.

## S3 write discipline

Rules that apply to every script or service that writes or copies objects under the `raw/` prefix in S3:

- **All raw S3 writes go through `S3Archiver.archive()`**, which asserts `sha256(bytes) == filename_hash` before the PUT. Never call `s3_client.put_object()` directly for `raw/` objects.
- **Any new script that copies, re-keys, or migrates S3 objects under `raw/`** must call `verify_key_matches_bytes(s3_client, bucket, key)` on both source AND destination after the copy. Reference: lessons from #2638 / #2663.
- **Never derive an S3 key from a DB column** without also re-hashing the bytes you intend to write under that key. The DB value may be stale or wrong; only the bytes are authoritative.

Helpers are in `packages/scraper-framework/src/framework/s3_integrity.py` — use `verify_key_matches_bytes` for a boolean check and `assert_key_matches_bytes` for a hard assertion (raises `S3MislabelError` on any mismatch or missing object).

## Telemetry artifacts

S3 paths for agent observability data, all under `s3://judgemind-document-archive-dev/`:

- **`ralph-reviews/<YYYY-MM-DD>/<agent-id>-<issue>.jsonl`** — complete `review-log.jsonl` for one ralph run. One object per agent run. Written by `scripts/ralph_review_log.py::log_summary` (on SHIP) and `scripts/cleanup_worktree.sh` (best-effort fallback on teardown). Schema: JSONL with `type` field — `"review"` (one per iteration per reviewer), `"summary"` (one at loop end), `"dissent_override"` (optional).

Upload helper: `scripts/telemetry_upload.py` — lazy-imports boto3, swallows all errors (network, credentials, missing boto3), returns `True`/`False`. Never raises. Import as a library (`from telemetry_upload import mirror_to_s3`) or invoke directly (`python3 scripts/telemetry_upload.py <local-path> <s3-key>`).

IAM grant: `s3:PutObject` on `<document_archive_bucket>/ralph-reviews/*`, wired into both the `dispatcher-daemon` and `dispatcher-agent-runner` modules via `task_ralph_review_telemetry` policies.

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

### CI guard umbrella (`scripts/run-ci-guards.sh`)

The pre-push hook also runs `scripts/run-ci-guards.sh`, an auto-discovery umbrella that mirrors the dozen+ `scripts/check-*` jobs in `.github/workflows/ci.yml`. Self-maintaining: dropping a new `scripts/check-foo.sh` into the tree makes it pick up automatically — no umbrella-script edit required.

**Run manually before pushing:**
```
scripts/run-ci-guards.sh         # run every applicable guard
scripts/run-ci-guards.sh --list  # show the discovered guard list without running
```

**Bypass for the rare case of intentionally pushing partial work:**
```
SKIP_CI_GUARDS=1 git push        # CI still runs every guard
```

The bypass emits a noisy stderr WARNING so it's hard to miss. Use only when one or more guards are known to fail and you are pushing partial work intentionally — otherwise fix the failure first.

**Skip list.** Several guards are not amenable to running blind from the local tree (need an issue/PR-number argument, need network access for `gh pr list`, depend on `npm ci` of `packages/web/`, depend on Docker, or run in scheduled-cron contexts against the dev DB). The umbrella's built-in `SKIP_LIST` excludes them; see the comment at the top of `scripts/run-ci-guards.sh` for the full list and rationale. Per-file opt-out is also available via a `# ci-guards: skip` marker in the first 20 lines of the guard file.

See #4332 for the motivating retro (PR #4325 hit a CI-only `sql-column-check` failure that would have surfaced locally with this umbrella).

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

### HTTP-status assertion hygiene (enforced in CI for `packages/api/`)

A TypeScript API test whose title names a specific HTTP status — `it('returns 400 for invalid UUID', ...)`, `it('rejects with HTTP 401', ...)`, `it('responds with statusCode 404', ...)` — must include a corresponding `statusCode` assertion in the test body (e.g. `expect(res.statusCode).toBe(400)`). Without this, the title is decorative and the test can pass even when the handler returns a different status, which is exactly how #4129 slipped through (a test titled "rejects … with HTTP 400" only checked the JSON-body shape; PR #4218 added the missing assertion).

CI enforces this via `scripts/check-test-statuscode-assertions.sh`, which scans `packages/api/tests/**/*.test.ts` and `packages/api/src/**/*.test.ts` for `it()`/`test()`/`it.each()` titles that match `HTTP <NNN>` / `returns <NNN>` / `responds with <NNN>` / `status(Code)? <NNN>`, then verifies the same `it()` block contains a `statusCode` reference paired with the matching status number.

**Escape hatch.** If a test legitimately doesn't go through an HTTP boundary (e.g. it asserts on a thrown error rather than on an inject-response object), append `// status-assertion-noqa` to the same line as the `it()` / `test()` opening call:

```typescript
it('returns 400 via direct call', async () => { // status-assertion-noqa
  await expect(callDirectly()).rejects.toThrow(/bad input/);
});
```

The check job is `test-statuscode-assertions-check` (gated on `detect-changes.outputs.api`). Run it locally:

```
scripts/check-test-statuscode-assertions.sh             # default scan
scripts/check-test-statuscode-assertions.sh --selftest  # embedded fixture self-test
```

Ref: #4220 (the issue that added the guard), #4129 (the regression that motivated it), #4218 (the original fix that added the missing assertion).

### Test marker convention (`scripts/tests/test_agent_runner_entrypoint.sh`)

Each test block in `scripts/tests/test_agent_runner_entrypoint.sh` carries a unique marker tied to the GitHub issue that introduced the test. This avoids the sequential-numbering collision that occurred when two parallel agents (PRs #3661 and #3666) both claimed marker T65, causing a merge conflict.

**Naming scheme:**

- **Header comment:** `# Test T_issue<N>: #<N> — short description`
- **Per-test env namespace:** `T<N>_*` — drop `_issue` for compactness (e.g. `T3656_TIMEOUT_TARGET`, `T3656A_STATE_DIR`)
- **Per-test runner function:** `run_t<N>_test()` (e.g. `run_t3656_test()`)
- **Sub-test labels / state dirs:** `t<N>a`, `t<N>b`, ... and `t<N>_state_dir`, `t<N>_stub_bin`, etc.

**Same-issue disambiguation:** when a single issue requires multiple distinct test blocks, append a letter to the issue number — `T_issue3507a` / `T_issue3507b` — with corresponding `T3507a_*` / `T3507b_*` env namespaces.

**Grandfathered tests:** T44–T59 predate this convention and are left as-is until they are naturally edited. New tests always use `T_issue<N>`.

**Why issue numbers?** GitHub issue numbers are globally unique within the repository. Two agents working on different issues N1 ≠ N2 always produce non-overlapping `T_issue<N1>` and `T_issue<N2>` markers, so parallel PRs that both add tests can never collide on a marker name.

#### Silent-drop guard

Even with unique issue-number markers, a 3-way git merge can silently discard one side's appended test block if both branches add lines at the same end-of-file region and the merge heuristics pick only one side. The **silent-drop guard** (`scripts/check-rebase-no-silent-drop.sh`) detects this at pre-push and in CI before a PR is merged.

**How it works:** the script calls `git merge-tree --write-tree origin/main HEAD` to compute an in-memory 3-way merge without touching the working tree. For each watched file, it collects all `MARKER_PATTERN` lines present in HEAD or `origin/main` and verifies that each one survives into the merged tree. If any marker is absent from the merged result **and** the merged file contains no `<<<<<<<` conflict markers (which would make the conflict user-visible), the script exits 1 with a diagnostic naming the dropped marker(s).

**Configuration:**

- `WATCH_FILES` — hardcoded list of 'appended-list' files to check. Initial entry: `scripts/tests/test_agent_runner_entrypoint.sh`. Add new files here when a new appended-list file is identified.
- `MARKER_PATTERN` — ERE passed to `grep -E`. Default: `^# Test T(_issue)?[0-9]+`. Override via environment variable if a different file uses a different marker scheme.

**Wiring:** the guard runs in the pre-push hook whenever `scripts/tests/test_agent_runner_entrypoint.sh` is in the pushed diff. It also runs as an explicit CI step (`Check rebase does not silently drop appended-list markers`) in the `scripts-tests` job (python shard). The CI checkout uses `fetch-depth: 0` so `origin/main` is always reachable. On shallow clones (or repos with no `origin/main`), the guard exits 0 gracefully.

**Fix when the guard fires:** rebase interactively (`git rebase -i origin/main`) and ensure both sides' marker blocks appear in the final result. If the merge heuristics keep collapsing them, add a unique sentinel comment line (e.g. `# --- T_issue<N> end ---`) after each block to give git distinct context to anchor the merge hunks.

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

The graphql-queries guard is implemented as `scripts/check-graphql-queries.sh` (bash wrapper) → `packages/web/scripts/validate-graphql-queries.mjs` (Node ESM validator). The validator lives under `packages/web/scripts/` rather than the top-level `scripts/` dir on purpose: `import 'graphql'` is resolved by Node's ESM loader by walking up from the importer's URL, so locating the validator inside `packages/web/` lets the loader find `packages/web/node_modules/graphql` after a vanilla `npm install` in `packages/web/` — no NODE_PATH tricks or repo-root install required. CI's `graphql-query-check` job continues to install the package via `npm install --no-save graphql@^16.8` at repo root, which the resolver also finds by walking further up. See #4093.

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

### Nullable schema migrations

When a migration drops `NOT NULL` on a column, every Python call site that reads that column without an `IS NOT NULL` guard becomes a latent `NoneType` bug — the column silently returns `None` in rows that were written after the migration, crashing code that assumed a non-null value.

**Audit requirement:** any migration that contains `ALTER COLUMN <col> DROP NOT NULL` must have all affected read sites either guarded or explicitly acknowledged before merging. The `nullable-column-reads-check` CI job enforces this automatically on the migrations shard (see #3394, #3396).

To run the check locally:

```
scripts/check-nullable-column-reads.sh --base origin/main
# or, against a specific migration file:
scripts/check-nullable-column-reads.sh --migration packages/api/migrations/49_foo.sql
```

**Two escape hatches** are accepted:

1. **SQL-level `IS NOT NULL` filter** — add a `WHERE <col> IS NOT NULL` clause to every SELECT that reads the column, or guard the Python read site with an explicit null check:

   ```python
   if row["hearing_date"] is not None:
       ...
   ```

2. **Per-column acknowledgment** — if null values are handled via logic the linter cannot trace (e.g. a downstream consumer filters them), add a comment at the top of the file with the column name:

   ```python
   # nullable-ok: hearing_date: filtered upstream by the ingest pipeline
   ```

   The column name is required; the annotation suppresses violations only for that column in the file. Use sparingly — prefer explicit guards at the read site.

**GraphQL nullability drift:** when the nullable column also backs a GraphQL field, the field declaration must be flipped from `Type!` to `Type` in the **same PR** as the migration. Leaving the field non-null causes the GraphQL serializer to throw at runtime the first time the column returns NULL, crashing the entire query. The `graphql-nullability-drift-check` CI job enforces this automatically for the columns listed in `KNOWN_MAPPINGS` inside `scripts/check-graphql-nullability-drift.py` (see #3441).

To run the check locally:

```
scripts/check-graphql-nullability-drift.sh --base origin/main
# or, against a specific migration file:
scripts/check-graphql-nullability-drift.sh --migration packages/api/migrations/49_foo.sql
```

If you add a new column→GraphQL-field mapping not yet covered, extend `KNOWN_MAPPINGS` in `scripts/check-graphql-nullability-drift.py` in the same PR.

### Bash patterns

#### Exit-code masking via `|| printf`

**Antipattern — do not use when the exit code matters:**

```
_var=$(cmd || printf '')
_rc=$?
```

`$?` always reflects the last command in the substitution, which is `printf` (exit 0), not `cmd`. The caller cannot distinguish "cmd succeeded with empty output" from "cmd failed and we fell back to empty". This pattern silently swallows failures.

Concrete incident: the initial `close_issue_post_merge` implementation in `scripts/dispatcher/agent-runner-entrypoint.sh` (#3411 review) used this shape to probe a GitHub issue's state. When the `gh` stub was misconfigured the probe returned `exit_code=0` with an empty string, so the caller treated the failure as a successful empty-state result rather than a probe error.

**Recommended fix — redirect stdout to a file, capture `_rc=$?` directly:**

```
set +e
cmd --flag arg \
    > "$WORKSPACE/cmd.stdout.log" \
    2> "$WORKSPACE/cmd.stderr.log"
_rc=$?
set -e
_var=""
if [[ -s "$WORKSPACE/cmd.stdout.log" ]]; then
    _var=$(tr -d '\n\r' < "$WORKSPACE/cmd.stdout.log")
fi
```

`_rc` now reflects `cmd`'s own exit code. See `close_issue_post_merge` (lines 3795–3807) for the precedent shape.

**When `|| printf` is fine:** use it only when the caller needs a default value and does NOT make any exit-code-bearing decision afterwards — e.g. `_label=$(git tag --points-at HEAD || printf 'none')` where the caller unconditionally uses `_label` as a display string. If the branch on `_rc` or a `[[ -z "$_var" ]]` guard is anywhere in the same function, prefer the file-redirect shape instead.

#### Post-rebase already-applied check (dispatcher entrypoint)

The canonical "is there anything to ship after a rebase ended?" check in
`scripts/dispatcher/agent-runner-entrypoint.sh` is the helper
`_post_rebase_no_diff_to_main`. Any new rebase-end site in that file
(`handle_push_and_pr`, `handle_ralph_baseline_rebase`, or future siblings)
MUST use this helper rather than an inline `rev-list --count` or bare
`git diff --quiet` block.

Why `git diff --quiet` and not `rev-list --count origin/main..HEAD`? `rev-list`
counts commit *objects*, not semantic diff. After `rebase --abort` HEAD equals
ORIG_HEAD, which still contains the agent's commits as distinct git objects
even when those commits became semantically redundant with main during the
rebase. `rev-list --count` returns N > 0 in that case and the pre-#3675 code
fell through to a terminal-fail envelope on what was actually a benign success.
The full progression is documented in issues #3614 / #3651 / #3662 / #3675.
See the helper's docstring in the entrypoint for a worked example.

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

**Historical context.** Two earlier check scripts (`scripts/check-bare-shadcn-accent.sh` — formerly the narrow `check-admin-dispatcher-brand-accent.sh` retired in #2832, `scripts/check-no-inline-ecs-healthcheck.sh`) and the `scripts/check-terminal-routing-comments.sh` guard each carry inline comments explaining why they avoid `mapfile` — the convention existed as tribal knowledge for months. PR #3081's first draft still used `mapfile -t` and silently exited 127 on the operator's laptop; the author found the precedent only by grepping peer check scripts. Issue #3082 codified the convention into `check-bash-compat.sh` + this docs section.

### Hygiene-check CI steps

When wiring a `scripts/check-no-*.sh`, `scripts/check-forbidden-*.sh`, or `scripts/check-deprecated-*.sh` guard into `.github/workflows/ci.yml`, **do not quote the forbidden string in the step's `name:` field** — the quoted pattern itself will trip the guard on the next CI run (see #2541/#2542 for the specific incident). Name the step after what the check *does* instead of what it *forbids* (e.g., name it after the replacement tool or the category of misuse, not the literal string).

The peer guard tests under `scripts/tests/test_check_*.sh` include a self-match assertion (see `scripts/tests/_guard_self_match_helpers.sh`) that catches this at test time. When adding a new string-forbidding guard, add an `assert_no_self_match_on_ci_step_name` call to its test.

**`scripts/check-no-heredoc-pipe-shadow.sh`** — flags the silent-miscompile pattern `... | python3 << TAG` whose body reads stdin via `json.load(sys.stdin)` / `sys.stdin.read()`. Bash gives the heredoc precedence as Python's stdin, so the piped data is silently discarded and JSON parsing raises `Expecting value: line 1 column 1` at runtime. See #4267 (the guard) and #4252 (the `scripts/ecs-wait-task.sh` PR that surfaced the footgun).

**Canonical repo-walk exclusion list (#4308).** Any new `scripts/check-*.sh` that runs `grep -rEn` over the repo must consume `REPO_WALK_EXCLUSIONS` from `scripts/preflight.sh` rather than hand-rolling its own `--exclude-dir=` list. The canonical baseline (`.git`, `.venv`, `node_modules`, `__pycache__`, `.next`, `.claude`, `.vite`, `tmp`, `dist`, `build`) is repo-wide policy — duplicating it across 10+ scripts is what caused #4300 (a missed `.claude` exclusion took main CI red for ~30 minutes). Per-check augmentations stay local to the script via `EXTRA_EXCLUDE_DIRS=(...)` appended to the canonical iteration. The hygiene gate `scripts/check-repo-walk-exclusions-canonical.sh` enforces the contract — see the `REPO_WALK_EXCLUSIONS` docstring in `scripts/preflight.sh` for the consumer pattern.

#### Hygiene-check guards: Fix-block contract

When you add a new `scripts/check-*.{sh,py}` hygiene guard whose error path lists violations (i.e. it is not a wrapper, decision flow, or operational health probe — see the verdict glossary in `docs/dx/check-script-fix-block-coverage.md`), the guard MUST emit a copy-pasteable `Fix:` block alongside the violation list. The contract has four parts:

1. **Default to a labelled `Fix:` / `Fix options:` / `Remediation:` block in the error path.** The block names the canonical fix in concrete terms — file paths, variable names, or commands the operator can run — not just a pointer to docs. When the canonical fix has a deterministic shape (e.g. "add this entry to a registry," "rename the file using this pattern," "swap this call for that wrapper"), emit a literal copy-pasteable patch with the actual symbols filled in, not a placeholder template.
2. **Show a literal copy-pasteable patch when the canonical fix has a deterministic shape.** Reference upgrade: `scripts/check_split_ruling_fields_propagated.py::_suggest_scope_entry()` (the canonical example landed in PR #4345, generalised by #4346) builds the exact `_DATACLASS_SCOPE` dict-literal the operator must paste — naming the real class key, the real worker function name, and the real reingest flag based on the violating dataclass. Two more reference upgrades from #4346: `check-hyphen-underscore-collision.sh` emits a `git mv` rename suggestion picking the canonical winner per file extension; `check_tf_empty_resource.py` emits a `count = length(local.compacted_<name>) > 0 ? 1 : 0` patch + a `locals { compacted_<name> = compact([...]) }` template naming the actual variables.
3. **The regression test must synthesize a violating fixture and grep for Fix-block content.** In `scripts/tests/test_check_<name>.sh`, build a tempfile that triggers the violation, run the guard, and assert the Fix block appears in stderr (e.g. `grep -q '^Fix:' "$err"` and `grep -q "<expected literal>" "$err"`). A test that only asserts non-zero exit on the violating fixture lets the Fix block silently regress — assert on its content.
4. **Add a row to `docs/dx/check-script-fix-block-coverage.md`** under the appropriate verdict (typically "self-diagnosing (Fix block)") with one-line Notes describing what the Fix block contains. The audit doc is the maintained per-guard health check; missing rows make it impossible to spot which guards have drifted.

The full per-guard verdict survey lives in `docs/dx/check-script-fix-block-coverage.md`. When you add or upgrade a guard, append/update the row there in the same PR.

### Test profiling — find the long pole in a shell test

When a `scripts/tests/test_*.sh` shell test gets slow and the cost is unclear ("which `# Test N:` section is dominant?"), run `scripts/profile-shell-test.sh` against it:

```
scripts/profile-shell-test.sh scripts/tests/test_agent_runner_entrypoint.sh
```

The wrapper:

1. Detects section boundaries (default regex: `^#\s+Tests?\s+\d+:`) and injects `_section_start` / `_section_end` markers around each.
2. Runs the instrumented copy in place (preserves `${BASH_SOURCE[0]}`-based path discovery).
3. Writes one `<elapsed_seconds>\t<section_label>` row per section to a TSV.
4. Prints the top-20 longest sections to stdout in sorted order.

The wrapped test's exit code and PASS/FAIL count are preserved verbatim — the wrapper is a pure observer. Use `--section-pattern` for files that use a different convention (e.g. `# T57a:` sub-test markers), `--top N` to control the summary length, and `--tsv PATH` to write the TSV somewhere predictable instead of the default tempfile.

The check exists because issue #4139's wall-clock optimization wasted iterations on a wrong hypothesis ("parse cost dominates") that this profiler would have falsified in one tool invocation — the actual cost was unconfigured `CI_POLL_INTERVAL=60` / `DEPLOY_GRACE_SECONDS=90` defaults sleeping 60-120s on `awaiting_ci`/`awaiting_deploy` sections. See #4176 for the full rationale.

### Timing-sensitive shell-test fixtures (sleep-gap rule)

Shell-test fixtures that use `sleep` to produce a deterministic ordering (e.g. asserting "sorted by elapsed desc") must use **at least a 4× gap between successive sleeps**. CI scheduling jitter routinely adds 10s-100s of milliseconds to a `sleep` call under load — gaps narrower than 4× can be reordered, flipping the assertion and producing a flake that gives no signal of being timing-related (see #4188 for the precedent: `0.02 / 0.04 / 0.06` flaked, `0.05 / 0.20 / 0.50` does not). When tempted to shrink the gap to "speed up the test," shrink the smallest sleep instead and keep the 4× ratio.

### Subagent responsibilities

Subagents MUST install dependencies, run ALL lint/format/test commands for every package touched, fix failures before committing, and only push after all local checks pass.
