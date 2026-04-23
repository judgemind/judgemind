#!/usr/bin/env node
/**
 * Test runner that detects DB availability and runs the appropriate test scope.
 *
 * - TEST_DATABASE_URL set AND reachable: runs all tests (unit + integration)
 *   with NODE_TLS_REJECT_UNAUTHORIZED=0 so pg pool connections succeed against
 *   RDS-style self-signed certs (used locally against RDS tunnels; CI's
 *   postgres service container uses plain TCP and is unaffected).
 * - TEST_DATABASE_URL unset OR unreachable: runs unit tests only (integration
 *   tests require a DB).
 *
 * This is the script wired to `npm test` (the pre-push hook target). The
 * `test:unit` and `test:integration` scripts in package.json are unchanged and
 * used by developers for targeted runs.
 *
 * WHY TEST_DATABASE_URL (#3006): the operational `DATABASE_URL` env var is
 * exposed to many service containers (including the dispatcher Fargate task,
 * which spawns ralph subagents). Reading `DATABASE_URL` here would cause
 * ralph's pre-push `npm test` to probe — and if reachable, migrate — the
 * operational dev RDS database. Using a dedicated `TEST_DATABASE_URL` that
 * is only set in test environments (local dev, CI) keeps test DB setup
 * strictly out of the operational code path.
 *
 * SSL note: NODE_TLS_REJECT_UNAUTHORIZED=0 is acceptable here because:
 *   1. This is test code, not production.
 *   2. CI uses a local postgres container (no SSL), so the flag is a no-op there.
 *   3. In any test scenario we point at RDS, the self-signed cert chain is a
 *      known operational reality — strict cert validation adds no security
 *      value in the test context.
 */

import { execSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import pg from 'pg';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const apiDir = join(__dirname, '..');

const TEST_DB_URL = process.env.TEST_DATABASE_URL;

/**
 * Check DB reachability using a permissive SSL connection (3s timeout).
 * Returns true if a query succeeds, false otherwise.
 */
async function isDbAvailable(url) {
  const pool = new pg.Pool({
    connectionString: url,
    ssl: { rejectUnauthorized: false },
    connectionTimeoutMillis: 3000,
  });
  try {
    await pool.query('SELECT 1');
    await pool.end();
    return true;
  } catch {
    await pool.end().catch(() => {});
    return false;
  }
}

// If TEST_DATABASE_URL is unset, skip the reachability probe entirely and go
// straight to unit-only mode. This is the guardrail that keeps ralph's
// pre-push test run from ever touching an operational DB (#3006).
const dbAvailable = TEST_DB_URL ? await isDbAvailable(TEST_DB_URL) : false;

if (dbAvailable) {
  console.log('DB available — running all tests (unit + integration)');
} else {
  console.log('DB not available — running unit tests only');
}

// When DB is available, set NODE_TLS_REJECT_UNAUTHORIZED=0 so that pg Pool
// instances inside individual integration test files can also connect
// without failing on self-signed certificate chains. This env var is
// inherited by vitest worker processes.
const env = { ...process.env };
if (dbAvailable) {
  env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
}

// When DB is available: run all tests WITH --coverage (pre-push hook checks
// coverage/lcov.info against the floor in coverage-baselines.json).
// When DB is not available: run unit tests WITHOUT --coverage so the pre-push
// hook's coverage floor check is skipped (no lcov.info → check is a no-op).
// The floor is validated by CI which always has a local postgres container.
//
// Also remove any stale lcov.info from a previous full-suite run so the
// pre-push hook's coverage floor check doesn't read stale data and fail.
if (!dbAvailable) {
  // Remove stale coverage directory so the pre-push hook's coverage floor
  // check doesn't read a stale lcov.info from a previous full-suite run and
  // falsely fail. When running unit-only we deliberately skip coverage
  // generation; CI (which always has a DB) enforces the floor.
  spawnSync('rm', ['-rf', join(apiDir, 'coverage')], { stdio: 'ignore' });
}

const pattern = dbAvailable ? '' : ' .unit.test';
const coverageFlag = dbAvailable ? ' --coverage' : '';
const cmd = `npx vitest run${coverageFlag} --passWithNoTests${pattern}`;

try {
  execSync(cmd, { cwd: apiDir, stdio: 'inherit', env });
  process.exit(0);
} catch {
  process.exit(1);
}
