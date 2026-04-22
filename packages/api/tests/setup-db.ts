/**
 * Shared database setup for integration tests.
 *
 * Runs node-pg-migrate programmatically to apply all pending migrations.
 * This is idempotent — re-running on an already-migrated database is a no-op.
 *
 * When multiple test files run in parallel, the first to acquire the migration
 * lock wins; others get an "Another migration is already running" error, which
 * we silently ignore since the winning process will apply the same migrations.
 *
 * TEST DB ISOLATION (#3006)
 * -------------------------
 * Integration-test DB setup reads ONLY `TEST_DATABASE_URL`, never the
 * operational `DATABASE_URL`. This prevents test tooling that happens to run
 * in an environment with `DATABASE_URL` set (e.g. a ralph subagent inside
 * the dispatcher Fargate container, which exposes dev RDS in its env) from
 * applying migrations to a shared/operational database.
 *
 * A hostname allowlist inside `applyMigrations()` provides a second layer of
 * defense: even if `TEST_DATABASE_URL` is accidentally set to an RDS endpoint
 * or another shared DB, migrations are refused. See `ALLOWED_HOSTS` below.
 *
 * The deploy migration path (`.github/workflows/deploy-api.yml` →
 * `judgemind-api-migrate-dev` ECS task) is intentionally separate and does
 * NOT call `applyMigrations()`; it invokes `node-pg-migrate` directly with
 * the operational `DATABASE_URL`.
 */

import { execSync } from 'child_process';
import { join } from 'path';

/**
 * Hostnames that are safe to migrate during tests. Any other hostname
 * (notably `*.rds.amazonaws.com`) is implicitly rejected. Extend only when
 * adding a new test-only DB location (e.g. a Testcontainers alias).
 *
 * Kept exported so the unit test in `setup-db.unit.test.ts` can assert on
 * the current set without hardcoding it in two places.
 */
export const ALLOWED_HOSTS = new Set(['localhost', '127.0.0.1', 'postgres']);

let applied = false;

export function applyMigrations(): void {
  if (applied) return;
  applied = true;

  const apiDir = join(__dirname, '..');
  const testDbUrl = process.env.TEST_DATABASE_URL;

  if (!testDbUrl) {
    throw new Error(
      'TEST_DATABASE_URL is not set; integration tests require an explicit ' +
        'test database URL. Set TEST_DATABASE_URL to a local/test Postgres ' +
        '(e.g. postgresql://judgemind:localdev@localhost:5432/judgemind_test) ' +
        'or run unit-only tests via `npm run test:unit`.'
    );
  }

  let parsed: URL;
  try {
    parsed = new URL(testDbUrl);
  } catch {
    throw new Error(
      `applyMigrations refused: TEST_DATABASE_URL is not a valid URL: ` +
        `'${testDbUrl}'. Expected a Postgres connection string.`
    );
  }

  if (!ALLOWED_HOSTS.has(parsed.hostname)) {
    throw new Error(
      `applyMigrations refused: TEST_DATABASE_URL host '${parsed.hostname}' ` +
        `is not in the test-DB allowlist. Tests must never migrate a ` +
        `shared database. Allowlist: ${[...ALLOWED_HOSTS].join(', ')}`
    );
  }

  try {
    execSync('npx node-pg-migrate up --no-timestamp', {
      cwd: apiDir,
      // NODE_TLS_REJECT_UNAUTHORIZED=0 bypasses SSL cert verification for the
      // migration child process only. This is intentional for test setup — CI
      // uses a plain localhost postgres (no SSL), and local dev uses a
      // plain-TCP Docker Postgres. Production code is unaffected.
      //
      // DATABASE_URL is the env var node-pg-migrate consumes (configured via
      // .node-pg-migraterc `database-url-var`). We pass testDbUrl through it
      // to the child process only — this file never reads DATABASE_URL.
      env: { ...process.env, DATABASE_URL: testDbUrl, NODE_TLS_REJECT_UNAUTHORIZED: '0' },
      stdio: 'pipe',
    });
  } catch (err: unknown) {
    const stderr = (err as { stderr?: Buffer }).stderr?.toString() ?? '';
    // Another migration is already running — safe to ignore since that process
    // will apply the same migrations we need.
    if (stderr.includes('Another migration is already running')) {
      return;
    }
    throw err;
  }
}

/**
 * Reset the memoized "already applied" flag. Test-only helper used by the
 * unit test suite so each test can exercise the full validation path.
 */
export function _resetAppliedForTesting(): void {
  applied = false;
}
