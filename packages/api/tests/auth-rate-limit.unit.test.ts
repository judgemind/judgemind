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
});
