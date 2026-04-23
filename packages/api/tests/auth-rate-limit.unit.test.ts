import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock the redis module
// ---------------------------------------------------------------------------

const mockIncr = vi.hoisted(() => vi.fn());
const mockExpire = vi.hoisted(() => vi.fn());
const mockConnect = vi.hoisted(() => vi.fn());
const mockCreateClient = vi.hoisted(() => vi.fn());

vi.mock('redis', () => ({
  createClient: mockCreateClient,
}));

// Note: we use dynamic import (vi.resetModules + await import) in each test
// to get fresh module state, since rate-limit.ts caches the Redis client.

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setupRedisClient(options?: { connectFails?: boolean }): void {
  const client = {
    incr: mockIncr,
    expire: mockExpire,
    connect: mockConnect,
  };

  if (options?.connectFails) {
    mockConnect.mockRejectedValue(new Error('Connection refused'));
  } else {
    mockConnect.mockResolvedValue(undefined);
  }

  mockCreateClient.mockReturnValue(client);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('checkLoginRateLimit', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
    // Ensure the real rate-limit path is exercised even if the ambient env
    // (e.g. auth.integration.test.ts running in the same process, or a
    // developer's shell) has set the opt-out flag. See issue #2744.
    delete process.env.DISABLE_LOGIN_RATE_LIMIT;
  });

  it('allows the first request from an IP', async () => {
    // Re-import to get fresh module state (redisClient = null)
    vi.resetModules();
    setupRedisClient();
    mockIncr.mockResolvedValue(1);
    mockExpire.mockResolvedValue(true);

    const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
    const allowed = await freshCheck('192.168.1.1');

    expect(allowed).toBe(true);
    expect(mockIncr).toHaveBeenCalled();
    // First request (count === 1) should set expiry
    expect(mockExpire).toHaveBeenCalled();
  });

  it('allows requests up to the limit (10)', async () => {
    vi.resetModules();
    setupRedisClient();
    mockIncr.mockResolvedValue(10);

    const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
    const allowed = await freshCheck('192.168.1.2');

    expect(allowed).toBe(true);
  });

  it('blocks requests beyond the limit', async () => {
    vi.resetModules();
    setupRedisClient();
    mockIncr.mockResolvedValue(11);

    const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
    const allowed = await freshCheck('192.168.1.3');

    expect(allowed).toBe(false);
  });

  it('sets expiry only on the first increment (count === 1)', async () => {
    vi.resetModules();
    setupRedisClient();
    mockIncr.mockResolvedValue(5); // Not the first request

    const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
    await freshCheck('192.168.1.4');

    // Should NOT call expire for count > 1
    expect(mockExpire).not.toHaveBeenCalled();
  });

  it('fails open (allows request) when Redis is unavailable', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true });

    const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
    const allowed = await freshCheck('192.168.1.5');

    expect(allowed).toBe(true);
    // incr should not be called since connection failed
    expect(mockIncr).not.toHaveBeenCalled();
  });

  it('bypasses the rate limit when DISABLE_LOGIN_RATE_LIMIT=1', async () => {
    vi.resetModules();
    setupRedisClient();
    // Even if Redis would report the IP over the limit, the opt-out flag
    // should short-circuit before any Redis call. See issue #2744.
    mockIncr.mockResolvedValue(999);
    process.env.DISABLE_LOGIN_RATE_LIMIT = '1';

    try {
      const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
      const allowed = await freshCheck('192.168.1.6');

      expect(allowed).toBe(true);
      expect(mockCreateClient).not.toHaveBeenCalled();
      expect(mockIncr).not.toHaveBeenCalled();
    } finally {
      delete process.env.DISABLE_LOGIN_RATE_LIMIT;
    }
  });

  it('does not bypass the rate limit when DISABLE_LOGIN_RATE_LIMIT is set to something other than "1"', async () => {
    vi.resetModules();
    setupRedisClient();
    mockIncr.mockResolvedValue(11); // over the limit
    process.env.DISABLE_LOGIN_RATE_LIMIT = 'true'; // wrong value — not "1"

    try {
      const { checkLoginRateLimit: freshCheck } = await import('../src/auth/rate-limit');
      const allowed = await freshCheck('192.168.1.7');

      expect(allowed).toBe(false);
      expect(mockIncr).toHaveBeenCalled();
    } finally {
      delete process.env.DISABLE_LOGIN_RATE_LIMIT;
    }
  });
});
