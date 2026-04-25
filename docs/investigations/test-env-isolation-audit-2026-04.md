# Test-Environment Isolation Audit — April 2026

**Issue:** #3009  
**Date:** 2026-04-25  
**Author:** automated (ralph)

## Context

Issue #3006 identified a footgun pattern where test setup code reads an operational env var
(e.g. `DATABASE_URL`) with a graceful fallback to `localhost`, so the test silently connects
to a local dev service when run in a clean environment — but silently connects to an operational
shared service when run inside the dispatcher Fargate agent-runner (which exports `DATABASE_URL`
pointing at dev RDS, `OPENSEARCH_URL` pointing at dev OpenSearch, etc., as child-process
environment variables).

The fix for `DATABASE_URL` in `packages/api/tests/` (issue #3006, merged) introduced:

1. `setup-db.ts` hard-asserts that `TEST_DATABASE_URL` is set and its hostname is in an
   `ALLOWED_HOSTS` allowlist (`localhost`, `127.0.0.1`, `postgres`). Any unrecognised host
   (e.g. `*.rds.amazonaws.com`) causes an immediate hard error rather than silently proceeding.
2. A CI guard (`scripts/check-no-test-database-url-fallback.sh`) forbids the literal string
   `process.env.DATABASE_URL` in `packages/api/tests/`.

This audit applies the same lens to every other test directory in the monorepo to identify
remaining instances of the pattern before they cause silent data corruption in production or
dev infrastructure.

## Methodology

Grep patterns applied to each test directory:

1. **Python fallback pattern** — `os\.environ\.get\(|os\.getenv\(` scoped to
   `packages/scraper-framework/tests/`, `packages/nlp-pipeline/tests/`, `scripts/tests/`
2. **TypeScript fallback pattern** — `process\.env\.[A-Z_]+\s*\?\?` and
   `process\.env\.[A-Z_]+\s*\|\|` scoped to `packages/api/tests/`, `packages/web/src/`
3. **Explicit operational var names** — `DATABASE_URL`, `OPENSEARCH_URL`,
   `JUDGEMIND_ARCHIVE_BUCKET`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `GOOGLE_API_KEY`,
   `ANTHROPIC_API_KEY`, `REDIS_URL`, `AWS_*` in all test directories
4. File-level reads for all specific files flagged in the scope-check to determine whether each
   hit is a footgun (var read without override, can reach a live target) or a non-finding
   (module-mocked, explicit `patch.dict` override, test-only default, or read-only with no side effects)

Directories audited:
- `packages/api/tests/` (TypeScript)
- `packages/web/src/` (TypeScript — app code exercised by tests)
- `packages/scraper-framework/tests/` (Python)
- `packages/nlp-pipeline/tests/` (Python)
- `scripts/tests/` (Python + shell)

---

## Findings

### Finding 1 — `OPENSEARCH_URL` in `packages/api/tests/search-rulings.integration.test.ts`

**File:** `packages/api/tests/search-rulings.integration.test.ts`  
**Line:** 46  
**Code:** `node: process.env.OPENSEARCH_URL ?? 'http://localhost:9200'`

**(a) Fallback to local default present:** Yes. When `OPENSEARCH_URL` is unset the client
connects to `http://localhost:9200`, which is the correct behaviour in a local Docker Compose
environment. However, the dispatcher Fargate agent-runner exports `OPENSEARCH_URL` pointing at
the dev OpenSearch cluster; when a ralph/TDD subagent runs this test inside that container the
fallback is bypassed and the test connects to live dev OpenSearch.

**(b) Side-effects possible on operational target:** Yes. `beforeAll` calls `seedOpenSearch()`
which (1) deletes the `tentative_rulings_test` index if it exists and (2) creates it fresh,
then indexes two seed documents. `afterAll` calls `cleanupOpenSearch()` which deletes the
index. A `tentative_rulings` alias is also attached to the test index on creation. If this
runs against the operational dev cluster it deletes and re-creates an index that an alias
(`tentative_rulings`) may already point to, and seeds it with two fake documents, corrupting
the live alias mapping and real search results.

**(c) Recommended remediation:** Mirror the `TEST_DATABASE_URL` / `ALLOWED_HOSTS` pattern:

- Introduce `TEST_OPENSEARCH_URL` as the test-specific variable.
- Change `search-rulings.integration.test.ts:46` to read `process.env.TEST_OPENSEARCH_URL`
  and hard-throw if it is unset (mirror `setup-db.ts` behaviour — do not silently default).
- Add a hostname allowlist check (`localhost`, `127.0.0.1`, `opensearch`) before connecting.
- Add a CI guard script `scripts/check-no-opensearch-url-fallback.sh` (mirror
  `check-no-test-database-url-fallback.sh`) to forbid `process.env.OPENSEARCH_URL` in
  `packages/api/tests/`.

**Follow-up issue:** see §Follow-up issues filed.

---

## Non-findings

### `packages/api/tests/` — `TEST_DATABASE_URL ??` occurrences

Files: `search-rulings.integration.test.ts:41`, `data-quality.integration.test.ts:38`,
`alerts.integration.test.ts:41`, `auth.integration.test.ts:25`,
`judge-analytics.integration.test.ts:31`, `graphql.integration.test.ts:37`,
`dispatcher.integration.test.ts:36`

All use `process.env.TEST_DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind_test'`
which is the already-fixed pattern — they read the test-specific var, not the operational
`DATABASE_URL`. The CI guard (`check-no-test-database-url-fallback.sh`) covers these files.
**Not a footgun.**

### `packages/api/tests/auth-tokens.unit.test.ts:61` — `JWT_SECRET ??`

Code: `const secret = process.env.JWT_SECRET ?? 'dev-jwt-secret-change-in-production';`

This is in a unit test that signs a JWT token using a local variable, then passes it to
`verifyVerificationToken` to assert that a wrong-purpose token is rejected. The test does not
reach any network endpoint or database. If `JWT_SECRET` happens to be set to the operational
secret, the test still behaves correctly (it only checks that a wrong-purpose token is
rejected, which is independent of the key value). No operational write side-effect possible.
**Not a footgun.**

The same fallback exists in `packages/api/src/auth/tokens.ts:4`. This is operational source
code (not a test file), so it is outside the scope of this audit. A separate concern: the
operational fallback means that if `JWT_SECRET` is unset in production the service uses
a well-known public default, which is a security risk — but that is a production hardening
concern, not the test-isolation footgun addressed here.

### `packages/scraper-framework/tests/test_backfill_oc_null_titles_llm.py` — `DATABASE_URL`

Lines 341–356, 386–399, 417–429: Each test saves `os.environ.get("DATABASE_URL")`, sets it to
`"postgresql://fake/db"`, runs `main()`, then restores it. All three call sites
`@patch("backfill_oc_null_titles_llm.psycopg")` — the psycopg module is fully replaced by a
`MagicMock`, so no real database connection is attempted. The explicit set of
`DATABASE_URL = "postgresql://fake/db"` overrides any inherited operational value before the
code runs. **Not a footgun** — the test pattern is an older manual-backup/restore style
instead of `patch.dict`, but the psycopg mock makes the connection call unreachable.

### `packages/scraper-framework/tests/test_runner.py` — `DATABASE_URL`

Lines 759–785: `TestConnectDb` tests exercise the `_connect_db()` helper directly. Line 762
calls `env.pop("DATABASE_URL", None)` with `patch.dict(os.environ, env, clear=True)` to
verify the function returns `None` when the var is absent. Line 771 patches
`framework.runner.psycopg.connect` with a `MagicMock` before setting
`{"DATABASE_URL": "postgresql://localhost/test"}`. The psycopg connect call is always mocked;
no real connection is made. **Not a footgun.**

### `packages/scraper-framework/tests/test_reingest_from_s3.py` — `DATABASE_URL`, `JUDGEMIND_ARCHIVE_BUCKET`, `REDIS_URL`, `OPENSEARCH_URL`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`

Lines 10752, 10766: `@patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})`
on two CLI-flag tests. Both tests also `@patch("reingest_from_s3.run_reingest")`, replacing the
entire reingest pipeline with a mock. No DB connection possible. **Not a footgun.**

Lines 9432–9706: Multiple `patch.dict` calls setting `JUDGEMIND_ARCHIVE_BUCKET`, `REDIS_URL`,
and `OPENSEARCH_URL` explicitly (e.g. `"OPENSEARCH_URL": ""`). These set fake values in the
dict; the surrounding test context also patches `reingest_from_s3.boto3`, `reingest_from_s3.psycopg`,
or the target client constructors — no real connections are made. **Not a footgun.**

Lines 3614–4096: `ANTHROPIC_API_KEY`/`GOOGLE_API_KEY` set via `patch.dict` to `"test-key"`.
The LLM client constructors are additionally mocked or the tests verify behaviour when no client
is available. No real API call made. **Not a footgun.**

### `packages/scraper-framework/tests/test_rebuild_db.py` — `DATABASE_URL`, `OPENSEARCH_URL`

Lines 590, 1077, 1124–1163, 1224, 1433, 1524, 1715, 1982, 2087, 2184, 2260, 2333: All uses are
inside `patch.dict(os.environ, {...})` blocks setting `DATABASE_URL` to `"postgres://test"`.
The surrounding context patches `concurrent.futures.ProcessPoolExecutor`,
`concurrent.futures.as_completed`, and other I/O — no real DB or OpenSearch calls reach a live
server. **Not a footgun.**

### `packages/scraper-framework/tests/test_ingestion_main.py` — `DATABASE_URL`, `REDIS_URL`, `OPENSEARCH_URL`, `JUDGEMIND_ARCHIVE_BUCKET`

Line 24–27: `_make_env()` returns a dict with all four vars set to localhost/test values. This
dict is applied via `@patch.dict("os.environ", _make_env(), clear=False)`. The test additionally
patches `ingestion.__main__.IngestionWorker`, `ingestion.__main__.make_s3_client`,
`ingestion.__main__.OpenSearch`, and `ingestion.__main__.redis.Redis` — all connection
constructors are mocked. No real connections. **Not a footgun.**

### `packages/scraper-framework/tests/test_llm_providers.py` — `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`

All usages are inside `patch.dict("os.environ", {...})` blocks that set fake keys like
`"test-key"` or `"fake-key-for-test"`. The LLM provider code paths that would make real API
calls are either not invoked or their network layer is separately mocked. **Not a footgun.**

### `packages/nlp-pipeline/tests/test_entity_extraction.py:235` and `test_summarizer.py:161` — `ANTHROPIC_API_KEY`

Both use `patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"})` and additionally
`patch("entity_extraction.extractor.anthropic.Anthropic")` / `patch("summarization.summarizer.anthropic.Anthropic")`
— the Anthropic client constructor is mocked so no real API call is possible. **Not a footgun.**

### `scripts/tests/test_backfill_previous_version_id.py` — `DATABASE_URL`

Lines 241–287: All inside `patch.dict(os.environ, {"DATABASE_URL": "postgresql://fake/db"})`.
The surrounding test context patches `psycopg.connect` or the module's psycopg reference.
No real DB connection. **Not a footgun.**

### `scripts/tests/test_check_s3_orphan_rate.py` — `DATABASE_URL`

Lines 467, 502: Inside `patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})` with
psycopg module mocked via `patch.dict(sys.modules, ...)`. Line 525: removes `DATABASE_URL`
from env to test the missing-var exit path. **Not a footgun.**

### `scripts/tests/test_audit_correctly_labeled_s3_orphans.py` — `DATABASE_URL`

Lines 551, 599: Inside `patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})` with
psycopg module mocked. Line 617: removes `DATABASE_URL` to test exit path. **Not a footgun.**

### `scripts/tests/test_cleanup_legacy_date_partitioned_s3.py` — `DATABASE_URL`

Lines 211, 260, 309: Inside `patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})` with
psycopg mocked. **Not a footgun.**

### `scripts/tests/test_dev_db_query_runner.py` — `DATABASE_URL`

Lines 178, 199, 223, 249, 268, 301, 354: All inside `patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"})` plus `patch.dict(sys.modules, {"psycopg": mock_psycopg})`. The psycopg module is entirely replaced by a mock. **Not a footgun.**

### `scripts/tests/test_emit_failure.py` — `DATABASE_URL`

Lines 158, 273: Inside `patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})` plus
`patch.dict(sys.modules, {"psycopg": mock_psycopg})`. Line 241: uses a bogus host
`postgresql://bogus-host-that-does-not-exist:5432/x` to verify the connection-error path; this
is the test exercising the error path intentionally (no silent fallback to operational target).
**Not a footgun.**

### `scripts/tests/test_spotcheck_sample.py` — `DATABASE_URL`

Line 73: `patch.dict("os.environ", {"DATABASE_URL": "postgres://test"})` combined with
`patch.dict("sys.modules", {"psycopg": mock_psycopg})`. **Not a footgun.**

### `scripts/tests/test_gemini_review.py` — `GOOGLE_API_KEY`

All uses are `patch.dict("os.environ", {"GOOGLE_API_KEY": ""}, clear=False)` or
`{"GOOGLE_API_KEY": "test-key"}` — the key is always explicitly set (empty or fake) and the
Gemini HTTP client is additionally mocked. **Not a footgun.**

### `packages/web/src/lib/display-helpers.ts:557,565` — `NEXT_PUBLIC_GRAPHQL_URL ??`

`buildDownloadUrl` and `buildDocumentContentUrl` both fall back to
`'http://localhost:3001/graphql'` when `NEXT_PUBLIC_GRAPHQL_URL` is unset. These are pure URL
builders — they return a string, they do not open a connection or make an HTTP request. The
test in `display-helpers.test.ts` explicitly tests the fallback path (lines 260–263, 276–279)
by deleting the env var and asserting the localhost URL is returned. No network side-effect.
**Not a footgun.**

### `packages/web/src/lib/apollo-client.ts:13` — `NEXT_PUBLIC_GRAPHQL_URL ??`

The Apollo client constructor reads `process.env.NEXT_PUBLIC_GRAPHQL_URL ?? 'http://localhost:3001/graphql'`
at module load time. In Next.js, `NEXT_PUBLIC_*` vars are inlined at build time and are
browser-safe constants; the operational value of this var is a public API URL, not a secret
or writable data store. The `apollo-client.test.ts` tests import the module and exercise the
Apollo client's in-memory cache — they do not issue any real GraphQL requests (no `fetch` call
is made). **Not a footgun.**

### `packages/web/src/app/layout.tsx:9` — `NEXT_PUBLIC_SITE_URL ||`

`process.env.NEXT_PUBLIC_SITE_URL || 'https://dev.judgemind.org'` is used only to set
`metadata.metadataBase`, which is a Next.js Metadata API value used during SSR. It does not
trigger a network request or write. **Not a footgun.**

### `packages/scraper-framework/tests/test_backfill_la_entity_descriptors.py` — `DATABASE_URL`

Lines 316, 353, 387: `patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"})`. Each
test also patches the psycopg module reference in the backfill module. **Not a footgun.**

### `packages/scraper-framework/tests/test_event_bus.py` — `REDIS_URL`

Lines 74, 151: Tests use `patch.dict(os.environ, ...)` to either pop `REDIS_URL` (testing the
disabled code path) or set it to `"redis://redis:6379"` while mocking `redis.Redis` directly.
No real Redis connection. **Not a footgun.**

### `packages/scraper-framework/tests/test_check_scraper_zero_record_streak.py` — `DATABASE_URL`

Line 367: `monkeypatch.delenv("DATABASE_URL", raising=False)` — removes the var to test the
missing-var path. No fallback to operational target. **Not a footgun.**

### `packages/scraper-framework/tests/test_audit_document_validation_rules.py` — `DATABASE_URL`

Lines 685–745: Mix of `monkeypatch.delenv` (tests missing-var exit code) and `monkeypatch.setenv`
to `"postgresql://fake"`. The psycopg connect call is patched separately. **Not a footgun.**

### `packages/scraper-framework/tests/test_check_short_unsubstantive_rulings.py` — `DATABASE_URL`

Lines 323–339: `monkeypatch.delenv` and `monkeypatch.setenv` to `"postgresql://fake/db"`, with
psycopg mocked. **Not a footgun.**

### `packages/api/tests/email.test.ts` — `SES_CONFIGURATION_SET`, `EMAIL_FROM`

Lines 23–24, 51, 65: `SES_CONFIGURATION_SET` is set to `'judgemind-dev'` or deleted to test
the configuration-set presence/absence code path; `EMAIL_FROM` is set to `'alerts@judgemind.org'`
to test the FROM-address overwrite. Both are explicit controlled values. The SES client is fully
replaced at module load by `vi.mock('../src/email/client', ...)` — the mock's `send` function
is a `vi.fn()` that never contacts real SES. No side-effect on operational SES. **Not a footgun.**

### `packages/api/tests/auth-resolvers.unit.test.ts` — `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`

Lines 65–66, 71–72, 83–84 (and corresponding lines throughout the describe block): The test
saves the current values of `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `beforeEach` and
restores them in `afterEach`. Before each test invocation, both vars are set to fake values
(`'test-client-id'`, `'test-client-secret'`). `vi.resetModules()` is called in `afterEach` so
that the next dynamic import via `loadResolvers()` picks up the freshly-set env vars at
module-scope. `globalThis.fetch` is replaced with a `vi.fn()` mock for every test that needs
the Google token-exchange endpoint — no real Google OAuth call is made. No side-effect on
operational Google services or any DB/S3 target. **Not a footgun.**

### `packages/api/tests/auth.integration.test.ts` — `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`

Lines 587–605: The `initiateGoogleAuth` test saves/restores `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` via local variables (`origId`, `origSecret`) and sets them to
`'test-client-id'` / `'test-client-secret'` for the duration of the test. However, the
resolver captures these vars at module load time (not per-call), so the in-test assignment may
not affect the resolver's cached values — the test itself notes this with a comment. Regardless,
the mutation only constructs a Google OAuth redirect URL; it does not exchange a token, make a
network request, or write to any external service. No side-effect on operational targets. The
mild code-quality concern about module-scope variable capture is not a test-isolation footgun
in the #3006 sense. **Not a footgun.**

### No operational env var usages found in `packages/nlp-pipeline/tests/`

The grepping found zero `os.environ.get`/`os.getenv` calls across the entire
`packages/nlp-pipeline/tests/` directory. The two `ANTHROPIC_API_KEY` hits are `patch.dict`
blocks in `test_entity_extraction.py` and `test_summarizer.py`, both with the Anthropic client
additionally mocked (documented above). **No footguns in this directory.**

### No operational env var usages found in `scripts/tests/` (excluding the one documented above)

All `DATABASE_URL` occurrences in `scripts/tests/*.py` use `patch.dict(os.environ, ...)` with
psycopg mocked via `patch.dict(sys.modules, ...)`. Shell test files (`test_wait_for_rollout.sh`,
`test_ecs_post_deploy_healthcheck.sh`, `test_agent_runner_entrypoint.sh`) that set `DATABASE_URL`
or `AWS_*` do so via inline variable assignment in a controlled sub-shell invocation of the
script under test, which is the correct pattern for shell-script testing. **No footguns in this directory.**

---

## Summary

| Directory | Footguns found | Notes |
|---|---|---|
| `packages/api/tests/` | **1** (`OPENSEARCH_URL`) | See Finding 1 |
| `packages/web/src/` | 0 | `NEXT_PUBLIC_*` vars are URL builders only, no write side-effects |
| `packages/scraper-framework/tests/` | 0 | All hits use `patch.dict` with psycopg/boto3/redis mocked |
| `packages/nlp-pipeline/tests/` | 0 | No env var reads without mock |
| `scripts/tests/` | 0 | All hits use `patch.dict` or inline shell assignment |

---

## Follow-up issues filed

- #3271 — `fix(tests): isolate OPENSEARCH_URL in packages/api/tests (#3006-class)`
