#!/usr/bin/env node
/**
 * Test runner that detects DB availability and runs the appropriate test scope.
 *
 * - TEST_DATABASE_URL set AND reachable: runs all tests (unit + integration)
 *   with coverage.
 * - TEST_DATABASE_URL unset OR unreachable: runs unit tests only, after
 *   printing a banner that says why and how to enable the integration tests.
 *   A silent skip hid the integration suite from pre-push for months (#4711).
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
 * SSL (#4711): the probe in ./db-probe.mjs honors the URL's `sslmode` (then
 * PGSSLMODE), and with neither set tries plain TCP before SSL. It used to
 * force SSL, which fails against a plain local postgres. Whatever worked is
 * passed to vitest as PGSSLMODE so the test pools connect the same way.
 * NODE_TLS_REJECT_UNAUTHORIZED=0 lets SSL connections to RDS tunnels accept
 * self-signed certs; that is acceptable in test code and a no-op for the
 * plain-TCP postgres used by CI and docker-compose.
 */

import { execSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import pg from 'pg';
import { probeDatabase, skipBanner, testEnvFor } from './db-probe.mjs';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const apiDir = join(__dirname, '..');

const TEST_DB_URL = process.env.TEST_DATABASE_URL;

// If TEST_DATABASE_URL is unset, skip the reachability probe entirely and go
// straight to unit-only mode. This is the guardrail that keeps ralph's
// pre-push test run from ever touching an operational DB (#3006).
const probe = TEST_DB_URL ? await probeDatabase(TEST_DB_URL, { Pool: pg.Pool }) : null;
const dbAvailable = probe?.ok === true;

let env = process.env;
if (dbAvailable) {
  console.log(
    `DB available — running all tests (unit + integration), ${probe.ssl ? 'with' : 'without'} SSL`,
  );
  env = testEnvFor(TEST_DB_URL, probe.ssl);
} else {
  console.error(
    skipBanner(
      TEST_DB_URL
        ? { reason: 'unreachable', url: TEST_DB_URL, attempts: probe.attempts }
        : { reason: 'unset' },
    ),
  );
}

// When DB is available: run all tests WITH --coverage (pre-push hook checks
// coverage/lcov.info against the floor in coverage-baselines.json).
// When DB is not available: run unit tests WITHOUT --coverage so the pre-push
// hook's coverage floor check is skipped (no lcov.info → check is a no-op).
// The floor is validated by CI which always has a local postgres container.
if (!dbAvailable) {
  // Remove stale coverage directory so the pre-push hook's coverage floor
  // check doesn't read a stale lcov.info from a previous full-suite run and
  // falsely fail.
  spawnSync('rm', ['-rf', join(apiDir, 'coverage')], { stdio: 'ignore' });
}

const pattern = dbAvailable ? '' : ' .unit.test';
const coverageFlag = dbAvailable ? ' --coverage' : '';
const cmd = `npx vitest run${coverageFlag} --passWithNoTests${pattern}`;

let status = 0;
try {
  execSync(cmd, { cwd: apiDir, stdio: 'inherit', env });
} catch {
  status = 1;
}

if (!dbAvailable) {
  // Repeat at the end: vitest output scrolls the first banner off-screen.
  console.error('\nReminder: integration tests were SKIPPED (see banner above).');
}
process.exit(status);
