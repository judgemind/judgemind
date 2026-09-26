/**
 * Unit tests for the `npm test` DB probe (scripts/db-probe.mjs, #4711).
 *
 * The probe decides whether `npm test` (the pre-push target) runs the
 * integration suite. It used to force SSL, so against a plain local postgres
 * (docker-compose, `docker run postgres:16`) the handshake failed and the
 * integration tests were skipped silently. These tests pin the SSL-mode
 * selection, the plain-then-SSL fallback, the env handed to vitest, and the
 * loud skip banner.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  SSL_NO_VERIFY,
  errorMessage,
  stripSslMode,
  sslCandidates,
  probeDatabase,
  testEnvFor,
  skipBanner,
} from '../scripts/db-probe.mjs';

const BASE = 'postgresql://judgemind:localdev@localhost:5432/judgemind_test';

describe('stripSslMode', () => {
  it('returns the URL unchanged and sslMode null when no sslmode param', () => {
    expect(stripSslMode(BASE)).toEqual({ connectionString: BASE, sslMode: null });
  });

  it('strips sslmode as the only param', () => {
    expect(stripSslMode(`${BASE}?sslmode=require`)).toEqual({
      connectionString: BASE,
      sslMode: 'require',
    });
  });

  it('strips sslmode among other params and keeps the rest', () => {
    expect(stripSslMode(`${BASE}?connect_timeout=10&sslmode=disable&application_name=x`)).toEqual(
      {
        connectionString: `${BASE}?connect_timeout=10&application_name=x`,
        sslMode: 'disable',
      },
    );
  });

  it('lowercases the sslmode value', () => {
    expect(stripSslMode(`${BASE}?sslmode=REQUIRE`).sslMode).toBe('require');
  });
});

describe('sslCandidates', () => {
  it('tries plain first, then SSL, when no sslmode is given', () => {
    expect(sslCandidates(null)).toEqual([false, SSL_NO_VERIFY]);
  });

  it('tries plain first, then SSL, for prefer and allow', () => {
    expect(sslCandidates('prefer')).toEqual([false, SSL_NO_VERIFY]);
    expect(sslCandidates('allow')).toEqual([false, SSL_NO_VERIFY]);
  });

  it('never tries SSL for disable', () => {
    expect(sslCandidates('disable')).toEqual([false]);
  });

  it.each(['require', 'verify-ca', 'verify-full', 'no-verify'])(
    'only tries SSL for %s (RDS-tunnel path)',
    (mode) => {
      expect(sslCandidates(mode)).toEqual([SSL_NO_VERIFY]);
    },
  );

  it('falls back to plain-then-SSL for an unknown mode', () => {
    expect(sslCandidates('bogus')).toEqual([false, SSL_NO_VERIFY]);
  });
});

/** Build a fake pg.Pool constructor whose query outcome depends on `ssl`. */
function fakePool(outcome: (ssl: unknown) => Error | null) {
  const configs: Array<Record<string, unknown>> = [];
  const end = vi.fn(async () => {});
  class FakePool {
    config: Record<string, unknown>;
    constructor(config: Record<string, unknown>) {
      this.config = config;
      configs.push(config);
    }
    async query() {
      const err = outcome(this.config.ssl);
      if (err) throw err;
      return { rows: [{ '?column?': 1 }] };
    }
    end = end;
  }
  return { Pool: FakePool, configs, end };
}

describe('probeDatabase', () => {
  it('succeeds without SSL against a plain local postgres', async () => {
    const { Pool, configs, end } = fakePool((ssl) =>
      ssl ? new Error('The server does not support SSL connections') : null,
    );
    const result = await probeDatabase(BASE, { Pool, env: {} });
    expect(result).toEqual({ ok: true, ssl: false, attempts: [] });
    expect(configs).toHaveLength(1);
    expect(configs[0]).toMatchObject({ connectionString: BASE, ssl: false });
    expect(end).toHaveBeenCalledTimes(1);
  });

  it('falls back to SSL when the server requires it and no sslmode is set', async () => {
    const { Pool, configs } = fakePool((ssl) =>
      ssl ? null : new Error('no pg_hba.conf entry for host, SSL off'),
    );
    const result = await probeDatabase(BASE, { Pool, env: {} });
    expect(result.ok).toBe(true);
    expect(result.ssl).toEqual(SSL_NO_VERIFY);
    expect(result.attempts).toEqual([
      { ssl: false, error: 'no pg_hba.conf entry for host, SSL off' },
    ]);
    expect(configs.map((c) => c.ssl)).toEqual([false, SSL_NO_VERIFY]);
  });

  it('uses SSL only (and strips sslmode) when the URL says sslmode=require', async () => {
    const { Pool, configs } = fakePool(() => null);
    const result = await probeDatabase(`${BASE}?sslmode=require`, { Pool, env: {} });
    expect(result).toEqual({ ok: true, ssl: SSL_NO_VERIFY, attempts: [] });
    // sslmode in the URL overrides the pg `ssl` option, so it must be stripped.
    expect(configs).toEqual([
      expect.objectContaining({ connectionString: BASE, ssl: SSL_NO_VERIFY }),
    ]);
  });

  it('honors PGSSLMODE when the URL has no sslmode', async () => {
    const { Pool, configs } = fakePool(() => null);
    await probeDatabase(BASE, { Pool, env: { PGSSLMODE: 'disable' } });
    expect(configs.map((c) => c.ssl)).toEqual([false]);
  });

  it('lets the URL sslmode win over PGSSLMODE', async () => {
    const { Pool, configs } = fakePool(() => null);
    await probeDatabase(`${BASE}?sslmode=disable`, { Pool, env: { PGSSLMODE: 'require' } });
    expect(configs.map((c) => c.ssl)).toEqual([false]);
  });

  it('reports every failed attempt when the DB is unreachable', async () => {
    const { Pool, end } = fakePool(() => new Error('connect ECONNREFUSED 127.0.0.1:5432'));
    const result = await probeDatabase(BASE, { Pool, env: {} });
    expect(result.ok).toBe(false);
    expect(result.attempts).toEqual([
      { ssl: false, error: 'connect ECONNREFUSED 127.0.0.1:5432' },
      { ssl: SSL_NO_VERIFY, error: 'connect ECONNREFUSED 127.0.0.1:5432' },
    ]);
    expect(end).toHaveBeenCalledTimes(2);
  });

  it('survives a pool.end() that rejects', async () => {
    const { Pool } = fakePool(() => new Error('boom'));
    const Rejecting = class extends Pool {
      end = vi.fn(async () => {
        throw new Error('end failed');
      });
    };
    const result = await probeDatabase(BASE, { Pool: Rejecting, env: {} });
    expect(result.ok).toBe(false);
  });

  it('records a non-Error rejection as a string', async () => {
    const { Pool } = fakePool(() => null);
    const Weird = class extends Pool {
      async query(): Promise<never> {
        throw 'plain string failure';
      }
    };
    const result = await probeDatabase(`${BASE}?sslmode=disable`, { Pool: Weird, env: {} });
    expect(result.attempts).toEqual([{ ssl: false, error: 'plain string failure' }]);
  });
});

describe('errorMessage', () => {
  it('uses the message of a plain Error', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
  });

  it('joins the inner errors of an empty-message AggregateError (localhost ECONNREFUSED)', () => {
    const agg = new AggregateError(
      [new Error('connect ECONNREFUSED ::1:1'), new Error('connect ECONNREFUSED 127.0.0.1:1')],
      '',
    );
    expect(errorMessage(agg)).toBe(
      'connect ECONNREFUSED ::1:1; connect ECONNREFUSED 127.0.0.1:1',
    );
  });

  it('falls back to the error code, then the name, when the message is empty', () => {
    const withCode = Object.assign(new Error(''), { code: 'ECONNREFUSED' });
    expect(errorMessage(withCode)).toBe('ECONNREFUSED');
    expect(errorMessage(new AggregateError([], ''))).toBe('AggregateError');
  });

  it('stringifies non-Error values', () => {
    expect(errorMessage('nope')).toBe('nope');
  });
});

describe('testEnvFor', () => {
  it('pins PGSSLMODE=disable for vitest when the plain probe succeeded', () => {
    const env = testEnvFor(BASE, false, { FOO: 'bar' });
    expect(env.PGSSLMODE).toBe('disable');
    expect(env.NODE_TLS_REJECT_UNAUTHORIZED).toBe('0');
    expect(env.FOO).toBe('bar');
  });

  it('pins PGSSLMODE=no-verify when only the SSL probe succeeded', () => {
    const env = testEnvFor(BASE, SSL_NO_VERIFY, {});
    expect(env.PGSSLMODE).toBe('no-verify');
  });

  it('leaves PGSSLMODE alone when the URL carries its own sslmode', () => {
    const env = testEnvFor(`${BASE}?sslmode=require`, SSL_NO_VERIFY, { PGSSLMODE: 'x' });
    expect(env.PGSSLMODE).toBe('x');
  });

  it('does not mutate the input env', () => {
    const input = { FOO: 'bar' };
    testEnvFor(BASE, false, input);
    expect(input).toEqual({ FOO: 'bar' });
  });
});

describe('skipBanner', () => {
  it('explains that TEST_DATABASE_URL is unset and how to enable integration tests', () => {
    const banner = skipBanner({ reason: 'unset' });
    expect(banner).toMatch(/INTEGRATION TESTS SKIPPED/);
    expect(banner).toMatch(/TEST_DATABASE_URL is not set/);
    expect(banner).toMatch(/TEST_DATABASE_URL=postgresql:\/\//);
    expect(banner).toMatch(/docker compose up -d postgres opensearch/);
    expect(banner).toMatch(/TEST_OPENSEARCH_URL=http:\/\/localhost:9200/);
  });

  it('lists each probe failure and redacts the password', () => {
    const banner = skipBanner({
      reason: 'unreachable',
      url: 'postgresql://judgemind:s3cret@localhost:5432/judgemind_test',
      attempts: [
        { ssl: false, error: 'connect ECONNREFUSED 127.0.0.1:5432' },
        { ssl: SSL_NO_VERIFY, error: 'connect ECONNREFUSED 127.0.0.1:5432' },
      ],
    });
    expect(banner).toMatch(/INTEGRATION TESTS SKIPPED/);
    expect(banner).toMatch(/without SSL: connect ECONNREFUSED/);
    expect(banner).toMatch(/with SSL: connect ECONNREFUSED/);
    expect(banner).toContain('judgemind:***@localhost');
    expect(banner).not.toContain('s3cret');
  });

  it('does not throw on an unparseable URL', () => {
    const banner = skipBanner({ reason: 'unreachable', url: 'not a url', attempts: [] });
    expect(banner).toContain('not a url');
  });
});
