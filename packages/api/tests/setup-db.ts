/**
 * Shared database setup for integration tests.
 *
 * Runs node-pg-migrate programmatically to apply all pending migrations.
 * This is idempotent — re-running on an already-migrated database is a no-op.
 *
 * When multiple test files run in parallel, the first to acquire the migration
 * lock wins; others get an "Another migration is already running" error, which
 * we silently ignore since the winning process will apply the same migrations.
 */

import { execSync } from 'child_process';
import { join } from 'path';

let applied = false;

export function applyMigrations(): void {
  if (applied) return;
  applied = true;

  const apiDir = join(__dirname, '..');
  const dbUrl =
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind';

  try {
    execSync('npx node-pg-migrate up --no-timestamp', {
      cwd: apiDir,
      env: { ...process.env, DATABASE_URL: dbUrl },
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
