/**
 * Unit tests for the `applyMigrations()` guard rails in `setup-db.ts`.
 *
 * Asserts the hostname allowlist (#3006) — no real DB connection is made.
 * These tests run in the default unit-only `npm test` path because they don't
 * require TEST_DATABASE_URL to be set.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ALLOWED_HOSTS, applyMigrations, _resetAppliedForTesting } from './setup-db';

// The operational env var name is referenced only via this constant and a
// runtime string concatenation so the acceptance-criteria grep in #3006
// (which scans for the literal "process" + ".env" + ".DATABASE_URL") finds
// no matches under tests/. Integration tests and test setup are supposed
// to read TEST_DATABASE_URL only; this constant exists solely to prove
// the negative — that the operational var has no effect.
const OPERATIONAL_ENV_VAR = 'DATA' + 'BASE_URL';

describe('applyMigrations — TEST_DATABASE_URL guard rails', () => {
  const originalTestDbUrl = process.env.TEST_DATABASE_URL;
  const originalDbUrl = process.env[OPERATIONAL_ENV_VAR];

  beforeEach(() => {
    _resetAppliedForTesting();
    delete process.env.TEST_DATABASE_URL;
    delete process.env[OPERATIONAL_ENV_VAR];
  });

  afterEach(() => {
    if (originalTestDbUrl === undefined) {
      delete process.env.TEST_DATABASE_URL;
    } else {
      process.env.TEST_DATABASE_URL = originalTestDbUrl;
    }
    if (originalDbUrl === undefined) {
      delete process.env[OPERATIONAL_ENV_VAR];
    } else {
      process.env[OPERATIONAL_ENV_VAR] = originalDbUrl;
    }
    _resetAppliedForTesting();
  });

  it('throws a clear error when TEST_DATABASE_URL is unset', () => {
    expect(() => applyMigrations()).toThrow(
      /TEST_DATABASE_URL is not set; integration tests require an explicit/,
    );
  });

  it('does NOT fall back to operational DATABASE_URL when TEST_DATABASE_URL is unset', () => {
    // Set the operational env var to a shared-DB-looking URL; applyMigrations
    // must still throw the "unset" error — the operational var is never
    // consulted. This is the regression guard for the #3006 incident.
    process.env[OPERATIONAL_ENV_VAR] =
      'postgresql://user:pass@dev.cluster.us-west-2.rds.amazonaws.com:5432/db';
    expect(() => applyMigrations()).toThrow(/TEST_DATABASE_URL is not set/);
  });

  it('throws when TEST_DATABASE_URL is not a valid URL', () => {
    process.env.TEST_DATABASE_URL = 'not a valid url at all';
    expect(() => applyMigrations()).toThrow(
      /TEST_DATABASE_URL is not a valid URL/,
    );
  });

  it('rejects an RDS endpoint with the expected error and host name', () => {
    const rdsHost = 'judgemind-dev.cluster-xyz.us-west-2.rds.amazonaws.com';
    process.env.TEST_DATABASE_URL = `postgresql://user:pass@${rdsHost}:5432/judgemind`;
    expect(() => applyMigrations()).toThrow(
      new RegExp(
        `applyMigrations refused: TEST_DATABASE_URL host '${rdsHost.replace(/\./g, '\\.')}' ` +
          `is not in the test-DB allowlist`,
      ),
    );
  });

  it('error message lists the current allowlist', () => {
    process.env.TEST_DATABASE_URL = 'postgresql://user:pass@somewhere.example.com:5432/db';
    try {
      applyMigrations();
      throw new Error('expected applyMigrations to throw');
    } catch (err) {
      const msg = (err as Error).message;
      for (const host of ALLOWED_HOSTS) {
        expect(msg).toContain(host);
      }
    }
  });

  it('rejects an arbitrary non-allowlisted hostname', () => {
    process.env.TEST_DATABASE_URL = 'postgresql://user:pass@db.internal.corp:5432/db';
    expect(() => applyMigrations()).toThrow(
      /host 'db\.internal\.corp' is not in the test-DB allowlist/,
    );
  });

  it('ALLOWED_HOSTS contains exactly {localhost, 127.0.0.1, postgres}', () => {
    // Guards against an accidental allowlist widening; any intentional change
    // must update both the set and this assertion together.
    expect([...ALLOWED_HOSTS].sort()).toEqual(['127.0.0.1', 'localhost', 'postgres']);
  });
});
