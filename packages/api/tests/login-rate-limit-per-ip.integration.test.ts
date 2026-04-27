/**
 * Integration test: login rate-limit keys on client IP resolved from
 * X-Forwarded-For when trustProxy is enabled.
 *
 * Verifies AC3 of issue #3643: per-IP rate-limiting works correctly behind
 * the ALB now that Fastify is configured with trustProxy: true.
 *
 * Strategy:
 * - Mock the `redis` module with a per-key in-memory counter (mirrors
 *   auth-rate-limit.unit.test.ts style).
 * - Use a stub pool that returns no user rows (login always fails with
 *   UNAUTHENTICATED — the important thing is reaching the rate-limit check).
 * - Disable `DISABLE_LOGIN_RATE_LIMIT` so the real rate-limit path runs.
 * - Send 11 login mutations from IP 1.2.3.4; assert the 11th is RATE_LIMITED.
 * - Send 1 login mutation from IP 5.6.7.8; assert it is UNAUTHENTICATED (not
 *   rate-limited — a fresh IP with 0 prior attempts).
 *
 * NOTE: This file uses vi.resetModules() + dynamic import in beforeAll to
 * get a fresh rate-limit module instance, since rate-limit.ts caches the Redis
 * client at module scope. The per-key counter is maintained in a Map held in
 * the mock closure.
 */

import { describe, it, expect, vi, beforeAll, afterAll } from 'vitest';
import type { Pool } from 'pg';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mock redis — must be declared before any imports that transitively import it
// ---------------------------------------------------------------------------

// Hoisted mock functions — these are captured by vi.mock() factory below
const mockIncr = vi.hoisted(() => vi.fn());
const mockExpire = vi.hoisted(() => vi.fn());
const mockConnect = vi.hoisted(() => vi.fn());
const mockCreateClient = vi.hoisted(() => vi.fn());

vi.mock('redis', () => ({
  createClient: mockCreateClient,
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Minimal stub Pool — returns empty rows for all queries, no-ops on end(). */
function makeStubPool(): Pool {
  return {
    query: vi.fn(async () => ({ rows: [], rowCount: 0 })),
    end: vi.fn(async () => undefined),
  } as unknown as Pool;
}

async function gql(
  app: FastifyInstance,
  query: string,
  variables?: Record<string, unknown>,
  headers?: Record<string, string>,
) {
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json', ...headers },
    payload: JSON.stringify({ query, variables }),
  });
  return {
    body: JSON.parse(res.body) as {
      data?: Record<string, unknown>;
      errors?: Array<{ message: string; extensions?: { code?: string } }>;
    },
    statusCode: res.statusCode,
  };
}

const LOGIN_MUTATION = `
  mutation($email: String!, $password: String!) {
    login(email: $email, password: $password) {
      accessToken
      user { id email }
    }
  }
`;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('login rate-limit — per-IP keying via XFF / trustProxy', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    // Reset module registry so rate-limit.ts starts with a fresh redisClient = null
    vi.resetModules();
    // Clear any leftover mock state from other test files
    vi.resetAllMocks();
    delete process.env.DISABLE_LOGIN_RATE_LIMIT;

    // Per-key counter, reset here for a clean state
    const keyCounters = new Map<string, number>();

    // Re-implement the mocks fresh after resetAllMocks()
    mockIncr.mockImplementation(async (key: string) => {
      const next = (keyCounters.get(key) ?? 0) + 1;
      keyCounters.set(key, next);
      return next;
    });
    mockExpire.mockResolvedValue(true);
    mockConnect.mockResolvedValue(undefined);
    mockCreateClient.mockReturnValue({
      incr: mockIncr,
      expire: mockExpire,
      connect: mockConnect,
    });

    // Dynamic import after vi.resetModules() so rate-limit.ts re-initialises
    const { buildApp } = await import('../src/app');
    app = await buildApp(makeStubPool());
  }, 20_000);

  afterAll(async () => {
    await app?.close();
    delete process.env.DISABLE_LOGIN_RATE_LIMIT;
  });

  it('allows the first 10 login attempts from IP 1.2.3.4 (UNAUTHENTICATED, not RATE_LIMITED)', async () => {
    for (let i = 1; i <= 10; i++) {
      const { body } = await gql(
        app,
        LOGIN_MUTATION,
        { email: 'nobody@nowhere.invalid', password: 'irrelevant' },
        { 'x-forwarded-for': '1.2.3.4' },
      );
      // Each attempt should fail with UNAUTHENTICATED (user not found),
      // not RATE_LIMITED — the rate limit hasn't been exceeded yet.
      expect(body.errors, `attempt ${i} should have errors`).toBeDefined();
      expect(
        body.errors![0].extensions?.code,
        `attempt ${i} should be UNAUTHENTICATED not RATE_LIMITED`,
      ).toBe('UNAUTHENTICATED');
    }
  });

  it('rate-limits the 11th login attempt from IP 1.2.3.4', async () => {
    const { body } = await gql(
      app,
      LOGIN_MUTATION,
      { email: 'nobody@nowhere.invalid', password: 'irrelevant' },
      { 'x-forwarded-for': '1.2.3.4' },
    );

    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('RATE_LIMITED');
  });

  it('does NOT rate-limit a fresh IP (5.6.7.8) — gets UNAUTHENTICATED', async () => {
    const { body } = await gql(
      app,
      LOGIN_MUTATION,
      { email: 'nobody@nowhere.invalid', password: 'irrelevant' },
      { 'x-forwarded-for': '5.6.7.8' },
    );

    expect(body.errors).toBeDefined();
    // Should be UNAUTHENTICATED (user not found), not RATE_LIMITED
    expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
  });
});
