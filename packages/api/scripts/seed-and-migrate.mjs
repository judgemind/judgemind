/**
 * Seed pgmigrations with already-applied migrations, then run node-pg-migrate.
 *
 * The initial database schema (and subsequent migrations) were applied outside
 * of node-pg-migrate's tracking system. This wrapper ensures the pgmigrations
 * table records all previously-applied migrations before running `up`, so
 * node-pg-migrate doesn't try to re-apply them.
 *
 * This script is idempotent: if records already exist, the INSERT is a no-op.
 *
 * Usage (from packages/api/):
 *   node scripts/seed-and-migrate.mjs
 *
 * Requires DATABASE_URL in the environment.
 */

import { readdirSync } from 'node:fs';
import { join, basename } from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import pg from 'pg';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const apiDir = join(__dirname, '..');

async function seedMigrations() {
  const dbUrl = process.env.DATABASE_URL;
  if (!dbUrl) {
    console.error('ERROR: DATABASE_URL is not set');
    process.exit(1);
  }

  const client = new pg.Client({ connectionString: dbUrl });
  await client.connect();

  try {
    // Check if pgmigrations table exists (node-pg-migrate creates it on first run)
    const tableCheck = await client.query(`
      SELECT EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'pgmigrations'
      ) AS exists
    `);

    if (!tableCheck.rows[0].exists) {
      console.log('pgmigrations table does not exist yet — node-pg-migrate will create it.');
      return;
    }

    // Check if there are already records
    const countResult = await client.query('SELECT COUNT(*)::int AS count FROM pgmigrations');
    const existingCount = countResult.rows[0].count;

    if (existingCount > 0) {
      console.log(`pgmigrations already has ${existingCount} record(s) — skipping seed.`);
      return;
    }

    // Read migration filenames from the migrations directory
    const migrationsDir = join(apiDir, 'migrations');
    const files = readdirSync(migrationsDir)
      .filter((f) => f.endsWith('.sql'))
      .sort((a, b) => {
        const numA = parseInt(a.split('_')[0], 10);
        const numB = parseInt(b.split('_')[0], 10);
        return numA - numB;
      });

    if (files.length === 0) {
      console.log('No migration files found — nothing to seed.');
      return;
    }

    // Verify the initial schema tables exist (sanity check that migrations were applied)
    const courtsCheck = await client.query(`
      SELECT EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'courts'
      ) AS exists
    `);

    if (!courtsCheck.rows[0].exists) {
      console.log('Initial schema not applied (courts table missing) — skipping seed.');
      return;
    }

    // Seed pgmigrations with all migration files
    // node-pg-migrate stores the filename without the .sql extension in the name column
    const names = files.map((f) => f.replace(/\.sql$/, ''));
    const now = new Date().toISOString();

    for (const name of names) {
      await client.query(
        `INSERT INTO pgmigrations (name, run_on)
         SELECT $1::varchar, $2::timestamp
         WHERE NOT EXISTS (SELECT 1 FROM pgmigrations WHERE name = $1::varchar)`,
        [name, now]
      );
    }

    console.log(`Seeded pgmigrations with ${names.length} migration(s): ${names.join(', ')}`);
  } finally {
    await client.end();
  }
}

async function main() {
  console.log('=== Seed pgmigrations ===');
  await seedMigrations();

  console.log('\n=== Run node-pg-migrate up ===');
  try {
    execSync('npx node-pg-migrate up --no-timestamp', {
      cwd: apiDir,
      env: process.env,
      stdio: 'inherit',
    });
  } catch (err) {
    console.error('Migration failed');
    process.exit(1);
  }

  console.log('\n=== Migrations complete ===');
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
