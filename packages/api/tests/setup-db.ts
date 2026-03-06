/**
 * Shared database setup for integration tests.
 *
 * Runs node-pg-migrate programmatically to apply all pending migrations.
 * This is idempotent — re-running on an already-migrated database is a no-op.
 */

import { execSync } from 'child_process';
import { join } from 'path';

export function applyMigrations(): void {
  const apiDir = join(__dirname, '..');
  const dbUrl =
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind';

  execSync('npx node-pg-migrate up --no-timestamp', {
    cwd: apiDir,
    env: { ...process.env, DATABASE_URL: dbUrl },
    stdio: 'pipe',
  });
}
