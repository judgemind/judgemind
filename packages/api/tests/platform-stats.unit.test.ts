/**
 * Unit tests for the platformStats resolver.
 * Mocks the pg Pool — no database needed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { resolvers } from '../src/graphql/resolvers';
import { clearPlatformStatsCache } from '../src/graphql/platform-stats-cache';

// ---------------------------------------------------------------------------
// Mock context
// ---------------------------------------------------------------------------

function createMockPool() {
  return {
    query: vi.fn(),
  };
}

function createContext(pool: ReturnType<typeof createMockPool>) {
  return {
    pool,
    loaders: {} as never,
    user: null,
    ip: '127.0.0.1',
    reply: {} as never,
    cookieHeader: '',
    opensearch: {} as never,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('platformStats resolver', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearPlatformStatsCache();
  });

  it('returns aggregate counts from courts, rulings, and judges tables', async () => {
    const pool = createMockPool();
    pool.query
      .mockResolvedValueOnce({ rows: [{ count: '12' }] })   // counties
      .mockResolvedValueOnce({ rows: [{ count: '5432' }] }) // rulings
      .mockResolvedValueOnce({ rows: [{ count: '87' }] });  // judges
    const ctx = createContext(pool);

    const result = await resolvers.Query.platformStats(null, {}, ctx);

    expect(result).toEqual({
      countiesCount: 12,
      rulingsCount: 5432,
      judgesCount: 87,
    });
    expect(pool.query).toHaveBeenCalledTimes(3);
  });

  it('returns zeros when tables are empty', async () => {
    const pool = createMockPool();
    pool.query
      .mockResolvedValueOnce({ rows: [{ count: '0' }] })
      .mockResolvedValueOnce({ rows: [{ count: '0' }] })
      .mockResolvedValueOnce({ rows: [{ count: '0' }] });
    const ctx = createContext(pool);

    const result = await resolvers.Query.platformStats(null, {}, ctx);

    expect(result).toEqual({
      countiesCount: 0,
      rulingsCount: 0,
      judgesCount: 0,
    });
  });

  it('queries active courts for counties count', async () => {
    const pool = createMockPool();
    pool.query
      .mockResolvedValueOnce({ rows: [{ count: '5' }] })
      .mockResolvedValueOnce({ rows: [{ count: '100' }] })
      .mockResolvedValueOnce({ rows: [{ count: '20' }] });
    const ctx = createContext(pool);

    await resolvers.Query.platformStats(null, {}, ctx);

    // First call should be counties from active courts
    const countiesCall = pool.query.mock.calls[0][0] as string;
    expect(countiesCall).toContain('courts');
    expect(countiesCall).toContain('is_active = true');

    // Second call should be rulings count
    const rulingsCall = pool.query.mock.calls[1][0] as string;
    expect(rulingsCall).toContain('rulings');

    // Third call should be active judges
    const judgesCall = pool.query.mock.calls[2][0] as string;
    expect(judgesCall).toContain('judges');
    expect(judgesCall).toContain('is_active = true');
  });
});
