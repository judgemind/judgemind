/**
 * Unit tests for the platformStats in-memory TTL cache.
 * Verifies caching behavior: hits, misses, TTL expiry, and cache clearing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getPlatformStats,
  clearPlatformStatsCache,
} from '../src/graphql/platform-stats-cache';

// ---------------------------------------------------------------------------
// Mock pool
// ---------------------------------------------------------------------------

function createMockPool() {
  return {
    query: vi.fn(),
  };
}

function setupPoolResponses(pool: ReturnType<typeof createMockPool>, counts: { counties: string; rulings: string; judges: string }) {
  pool.query
    .mockResolvedValueOnce({ rows: [{ count: counts.counties }] })
    .mockResolvedValueOnce({ rows: [{ count: counts.rulings }] })
    .mockResolvedValueOnce({ rows: [{ count: counts.judges }] });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('platform stats cache', () => {
  beforeEach(() => {
    clearPlatformStatsCache();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('queries the database on the first call (cache miss)', async () => {
    const pool = createMockPool();
    setupPoolResponses(pool, { counties: '12', rulings: '5432', judges: '87' });

    const result = await getPlatformStats(pool as never);

    expect(result).toEqual({
      countiesCount: 12,
      rulingsCount: 5432,
      judgesCount: 87,
    });
    expect(pool.query).toHaveBeenCalledTimes(3);
  });

  it('returns cached values on the second call without querying the database', async () => {
    const pool = createMockPool();
    setupPoolResponses(pool, { counties: '10', rulings: '1000', judges: '50' });

    // First call — populates cache
    const result1 = await getPlatformStats(pool as never);
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Second call — should use cache
    const result2 = await getPlatformStats(pool as never);
    expect(pool.query).toHaveBeenCalledTimes(3); // no additional queries
    expect(result2).toEqual(result1);
  });

  it('queries the database again after TTL expires', async () => {
    vi.useFakeTimers();
    const pool = createMockPool();

    // First call
    setupPoolResponses(pool, { counties: '10', rulings: '1000', judges: '50' });
    await getPlatformStats(pool as never, 60_000); // 1-minute TTL
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Advance time past TTL
    vi.advanceTimersByTime(61_000);

    // Second call — cache expired, should query again
    setupPoolResponses(pool, { counties: '11', rulings: '1100', judges: '55' });
    const result = await getPlatformStats(pool as never, 60_000);

    expect(pool.query).toHaveBeenCalledTimes(6); // 3 original + 3 new
    expect(result).toEqual({
      countiesCount: 11,
      rulingsCount: 1100,
      judgesCount: 55,
    });
  });

  it('does not query again before TTL expires', async () => {
    vi.useFakeTimers();
    const pool = createMockPool();

    setupPoolResponses(pool, { counties: '10', rulings: '1000', judges: '50' });
    await getPlatformStats(pool as never, 60_000);
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Advance time but stay within TTL
    vi.advanceTimersByTime(59_000);

    const result = await getPlatformStats(pool as never, 60_000);
    expect(pool.query).toHaveBeenCalledTimes(3); // still cached
    expect(result).toEqual({
      countiesCount: 10,
      rulingsCount: 1000,
      judgesCount: 50,
    });
  });

  it('clearPlatformStatsCache forces the next call to query the database', async () => {
    const pool = createMockPool();

    // First call — populates cache
    setupPoolResponses(pool, { counties: '10', rulings: '1000', judges: '50' });
    await getPlatformStats(pool as never);
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Clear cache
    clearPlatformStatsCache();

    // Next call should query again
    setupPoolResponses(pool, { counties: '12', rulings: '1200', judges: '60' });
    const result = await getPlatformStats(pool as never);

    expect(pool.query).toHaveBeenCalledTimes(6);
    expect(result).toEqual({
      countiesCount: 12,
      rulingsCount: 1200,
      judgesCount: 60,
    });
  });

  it('returns zeros when tables are empty', async () => {
    const pool = createMockPool();
    setupPoolResponses(pool, { counties: '0', rulings: '0', judges: '0' });

    const result = await getPlatformStats(pool as never);

    expect(result).toEqual({
      countiesCount: 0,
      rulingsCount: 0,
      judgesCount: 0,
    });
  });

  it('queries correct SQL statements', async () => {
    const pool = createMockPool();
    setupPoolResponses(pool, { counties: '5', rulings: '100', judges: '20' });

    await getPlatformStats(pool as never);

    const countiesCall = pool.query.mock.calls[0][0] as string;
    expect(countiesCall).toContain('courts');
    expect(countiesCall).toContain('is_active = true');
    expect(countiesCall).toContain('COUNT(DISTINCT county)');

    const rulingsCall = pool.query.mock.calls[1][0] as string;
    expect(rulingsCall).toContain('rulings');
    expect(rulingsCall).toContain('COUNT(*)');

    const judgesCall = pool.query.mock.calls[2][0] as string;
    expect(judgesCall).toContain('judges');
    expect(judgesCall).toContain('is_active = true');
    expect(judgesCall).toContain('COUNT(*)');
  });

  it('supports custom TTL values', async () => {
    vi.useFakeTimers();
    const pool = createMockPool();

    // Use a very short TTL (100ms)
    setupPoolResponses(pool, { counties: '1', rulings: '2', judges: '3' });
    await getPlatformStats(pool as never, 100);
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Still within TTL
    vi.advanceTimersByTime(50);
    await getPlatformStats(pool as never, 100);
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Past TTL
    vi.advanceTimersByTime(51);
    setupPoolResponses(pool, { counties: '4', rulings: '5', judges: '6' });
    const result = await getPlatformStats(pool as never, 100);
    expect(pool.query).toHaveBeenCalledTimes(6);
    expect(result).toEqual({
      countiesCount: 4,
      rulingsCount: 5,
      judgesCount: 6,
    });
  });

  it('deduplicates concurrent calls during a cache miss (prevents cache stampede)', async () => {
    const pool = createMockPool();

    // All three query mocks return immediately
    pool.query
      .mockResolvedValue({ rows: [{ count: '42' }] });

    // Fire two calls concurrently — both should share the same in-flight promise
    const [result1, result2] = await Promise.all([
      getPlatformStats(pool as never),
      getPlatformStats(pool as never),
    ]);

    // Both should return the same data
    expect(result1).toEqual(result2);
    expect(result1).toEqual({
      countiesCount: 42,
      rulingsCount: 42,
      judgesCount: 42,
    });
    // Only 3 queries total — the second call coalesced onto the first
    expect(pool.query).toHaveBeenCalledTimes(3);

    // Subsequent call also hits cache
    const result3 = await getPlatformStats(pool as never);
    expect(result3).toEqual(result1);
    expect(pool.query).toHaveBeenCalledTimes(3);
  });
});
