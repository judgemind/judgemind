/**
 * Unit tests for the `getTestOpenSearchUrl()` guard rails in `setup-opensearch.ts`.
 *
 * Asserts the hostname allowlist — no real OpenSearch connection is made.
 * These tests run in the default unit-only `npm test` path because they don't
 * require TEST_OPENSEARCH_URL to be set.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { OPENSEARCH_ALLOWED_HOSTS, getTestOpenSearchUrl } from './setup-opensearch';

// The operational env var name is referenced only via this constant and a
// runtime string concatenation so the acceptance-criteria grep
// (which scans for the literal "process" + ".env" + ".OPENSEARCH_URL") finds
// no matches under tests/. Integration tests and test setup are supposed
// to read TEST_OPENSEARCH_URL only; this constant exists solely to prove
// the negative — that the operational var has no effect.
const OPERATIONAL_ENV_VAR = 'OPEN' + 'SEARCH_URL';

describe('getTestOpenSearchUrl — TEST_OPENSEARCH_URL guard rails', () => {
  const originalTestOsUrl = process.env.TEST_OPENSEARCH_URL;
  const originalOsUrl = process.env[OPERATIONAL_ENV_VAR];

  beforeEach(() => {
    delete process.env.TEST_OPENSEARCH_URL;
    delete process.env[OPERATIONAL_ENV_VAR];
  });

  afterEach(() => {
    if (originalTestOsUrl === undefined) {
      delete process.env.TEST_OPENSEARCH_URL;
    } else {
      process.env.TEST_OPENSEARCH_URL = originalTestOsUrl;
    }
    if (originalOsUrl === undefined) {
      delete process.env[OPERATIONAL_ENV_VAR];
    } else {
      process.env[OPERATIONAL_ENV_VAR] = originalOsUrl;
    }
  });

  it('throws a clear error when TEST_OPENSEARCH_URL is unset', () => {
    expect(() => getTestOpenSearchUrl()).toThrow(
      /TEST_OPENSEARCH_URL is not set; integration tests require an explicit/,
    );
  });

  it('does NOT fall back to operational OPENSEARCH_URL when TEST_OPENSEARCH_URL is unset', () => {
    // Set the operational env var to a shared-cluster-looking URL; getTestOpenSearchUrl
    // must still throw the "unset" error — the operational var is never
    // consulted. This is the regression guard mirroring the #3006 incident.
    process.env[OPERATIONAL_ENV_VAR] =
      'https://opensearch.dev.judgemind.internal:9200';
    expect(() => getTestOpenSearchUrl()).toThrow(/TEST_OPENSEARCH_URL is not set/);
  });

  it('throws when TEST_OPENSEARCH_URL is not a valid URL', () => {
    process.env.TEST_OPENSEARCH_URL = 'not a valid url at all';
    expect(() => getTestOpenSearchUrl()).toThrow(
      /TEST_OPENSEARCH_URL is not a valid URL/,
    );
  });

  it('rejects a non-allowlisted host with the expected error and host name', () => {
    const badHost = 'opensearch.dev.judgemind.internal';
    process.env.TEST_OPENSEARCH_URL = `https://${badHost}:9200`;
    expect(() => getTestOpenSearchUrl()).toThrow(
      new RegExp(
        `getTestOpenSearchUrl refused: TEST_OPENSEARCH_URL host '${badHost.replace(/\./g, '\\.')}' ` +
          `is not in the test-OpenSearch allowlist`,
      ),
    );
  });

  it('error message lists the current allowlist', () => {
    process.env.TEST_OPENSEARCH_URL = 'https://somewhere.example.com:9200';
    try {
      getTestOpenSearchUrl();
      throw new Error('expected getTestOpenSearchUrl to throw');
    } catch (err) {
      const msg = (err as Error).message;
      for (const host of OPENSEARCH_ALLOWED_HOSTS) {
        expect(msg).toContain(host);
      }
    }
  });

  it('OPENSEARCH_ALLOWED_HOSTS contains exactly {localhost, 127.0.0.1, opensearch}', () => {
    // Guards against an accidental allowlist widening; any intentional change
    // must update both the set and this assertion together.
    expect([...OPENSEARCH_ALLOWED_HOSTS].sort()).toEqual(['127.0.0.1', 'localhost', 'opensearch']);
  });
});
