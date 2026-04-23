/**
 * Unit tests for judge analytics helper functions and caching logic.
 * No database or network dependencies — Redis and pg are mocked.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Statically import computeGrantRate (no mocking needed for pure functions)
// ---------------------------------------------------------------------------

import { computeGrantRate } from '../src/graphql/judge-analytics';

// ---------------------------------------------------------------------------
// Mock redis module — must be hoisted before any import touches redis
// ---------------------------------------------------------------------------

const mockGet = vi.hoisted(() => vi.fn());
const mockSet = vi.hoisted(() => vi.fn());
const mockDel = vi.hoisted(() => vi.fn());
const mockConnect = vi.hoisted(() => vi.fn());
const mockCreateClient = vi.hoisted(() => vi.fn());

vi.mock('redis', () => ({
  createClient: mockCreateClient,
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function setupRedisClient(options?: { connectFails?: boolean }): void {
  const client = {
    get: mockGet,
    set: mockSet,
    del: mockDel,
    connect: mockConnect,
  };

  if (options?.connectFails) {
    mockConnect.mockRejectedValue(new Error('Connection refused'));
  } else {
    mockConnect.mockResolvedValue(undefined);
  }

  mockCreateClient.mockReturnValue(client);
}

/** Create a mock pg Pool that returns predefined results for sequential queries. */
function createMockPool(options: {
  judgeExists: boolean;
  totalRulings?: number;
  outcomes?: Array<{ outcome: string; count: number }>;
  motionTypes?: Array<{
    motion_type: string;
    total: number;
    granted: number;
    denied: number;
    granted_in_part: number;
    other: number;
  }>;
  earliest?: string | null;
  latest?: string | null;
}) {
  const queryFn = vi.fn();

  // First call: judge existence check (SELECT id FROM judges WHERE id = $1)
  if (options.judgeExists) {
    queryFn.mockResolvedValueOnce({ rows: [{ id: 'judge-1' }] });
  } else {
    queryFn.mockResolvedValueOnce({ rows: [] });
    return { query: queryFn } as unknown;
  }

  // If judge exists, the next call is Promise.all with 4 queries:
  // total, outcomes, motionTypes, dateRange
  // Since pool.query is called 4 times inside Promise.all, we queue 4 responses.
  queryFn.mockResolvedValueOnce({
    rows: [{ count: options.totalRulings ?? 0 }],
  });
  queryFn.mockResolvedValueOnce({
    rows: (options.outcomes ?? []).map((o) => ({ outcome: o.outcome, count: o.count })),
  });
  queryFn.mockResolvedValueOnce({
    rows: (options.motionTypes ?? []).map((m) => ({
      motion_type: m.motion_type,
      total: m.total,
      granted: m.granted,
      denied: m.denied,
      granted_in_part: m.granted_in_part,
      other: m.other,
    })),
  });
  queryFn.mockResolvedValueOnce({
    rows: [{ earliest: options.earliest ?? null, latest: options.latest ?? null }],
  });

  return { query: queryFn } as unknown;
}

const SAMPLE_ANALYTICS = {
  judgeId: 'judge-1',
  totalRulings: 5,
  rulingsByOutcome: [
    { outcome: 'granted', count: 3 },
    { outcome: 'denied', count: 2 },
  ],
  rulingsByMotionType: [
    {
      motionType: 'msj',
      total: 5,
      granted: 3,
      denied: 2,
      grantedInPart: 0,
      other: 0,
      grantRate: 0.6,
    },
  ],
  earliestRuling: '2025-01-01',
  latestRuling: '2025-06-01',
};

// ---------------------------------------------------------------------------
// computeGrantRate (pure function — no mocking needed)
// ---------------------------------------------------------------------------

describe('computeGrantRate', () => {
  it('returns 0 when denominator is zero (no substantive rulings)', () => {
    expect(computeGrantRate(0, 0, 0)).toBe(0);
  });

  it('returns 1.0 when all rulings are granted', () => {
    expect(computeGrantRate(5, 0, 0)).toBe(1);
  });

  it('returns 0 when no rulings are granted', () => {
    expect(computeGrantRate(0, 5, 0)).toBe(0);
  });

  it('computes correctly with mixed outcomes', () => {
    // 3 granted, 1 denied, 1 granted_in_part => 3/5 = 0.6
    expect(computeGrantRate(3, 1, 1)).toBeCloseTo(0.6, 10);
  });

  it('excludes procedural outcomes from denominator by design', () => {
    // granted=2, denied=1, grantedInPart=1 => 2/(2+1+1) = 0.5
    // (moot/continued/etc. are not passed to this function)
    expect(computeGrantRate(2, 1, 1)).toBeCloseTo(0.5, 10);
  });

  it('handles single granted ruling', () => {
    expect(computeGrantRate(1, 0, 0)).toBe(1);
  });

  it('handles single denied ruling', () => {
    expect(computeGrantRate(0, 1, 0)).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// getJudgeAnalytics — caching and query logic
// ---------------------------------------------------------------------------

describe('getJudgeAnalytics', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
  });

  it('returns null when judge does not exist', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true }); // Redis unavailable
    const pool = createMockPool({ judgeExists: false });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'nonexistent-judge');

    expect(result).toBeNull();
  });

  it('queries DB and returns analytics when Redis is unavailable', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true });
    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 5,
      outcomes: [
        { outcome: 'granted', count: 3 },
        { outcome: 'denied', count: 2 },
      ],
      motionTypes: [
        { motion_type: 'msj', total: 5, granted: 3, denied: 2, granted_in_part: 0, other: 0 },
      ],
      earliest: '2025-01-01',
      latest: '2025-06-01',
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).not.toBeNull();
    expect(result!.judgeId).toBe('judge-1');
    expect(result!.totalRulings).toBe(5);
    expect(result!.rulingsByOutcome).toHaveLength(2);
    expect(result!.rulingsByMotionType).toHaveLength(1);
    expect(result!.rulingsByMotionType[0].grantRate).toBeCloseTo(0.6, 5);
    expect(result!.earliestRuling).toBe('2025-01-01');
    expect(result!.latestRuling).toBe('2025-06-01');
  });

  it('returns cached data on Redis cache hit without querying DB for analytics', async () => {
    vi.resetModules();
    setupRedisClient();
    mockGet.mockResolvedValue(JSON.stringify(SAMPLE_ANALYTICS));

    // Pool only needs to handle the judge existence check — analytics come from cache
    const queryFn = vi.fn();
    queryFn.mockResolvedValueOnce({ rows: [{ id: 'judge-1' }] }); // judge exists
    const pool = { query: queryFn };

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).toEqual(SAMPLE_ANALYTICS);
    // Only 1 query (judge existence check) — no analytics queries
    expect(queryFn).toHaveBeenCalledTimes(1);
    expect(mockGet).toHaveBeenCalled();
    // Should not write to cache since we got a hit
    expect(mockSet).not.toHaveBeenCalled();
  });

  it('queries DB on Redis cache miss and writes result to cache', async () => {
    vi.resetModules();
    setupRedisClient();
    mockGet.mockResolvedValue(null); // cache miss
    mockSet.mockResolvedValue('OK');

    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 3,
      outcomes: [{ outcome: 'granted', count: 3 }],
      motionTypes: [
        { motion_type: 'demurrer', total: 3, granted: 3, denied: 0, granted_in_part: 0, other: 0 },
      ],
      earliest: '2025-03-01',
      latest: '2025-05-01',
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).not.toBeNull();
    expect(result!.totalRulings).toBe(3);
    // Should have written to cache
    expect(mockSet).toHaveBeenCalledTimes(1);
  });

  it('falls through to DB when Redis cache read throws', async () => {
    vi.resetModules();
    setupRedisClient();
    mockGet.mockRejectedValue(new Error('Redis read error'));
    mockSet.mockResolvedValue('OK');

    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 2,
      outcomes: [{ outcome: 'denied', count: 2 }],
      motionTypes: [],
      earliest: '2025-04-01',
      latest: '2025-04-15',
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).not.toBeNull();
    expect(result!.totalRulings).toBe(2);
    // Should still attempt to write to cache after fetching from DB
    expect(mockSet).toHaveBeenCalled();
  });

  it('returns analytics even when Redis cache write throws', async () => {
    vi.resetModules();
    setupRedisClient();
    mockGet.mockResolvedValue(null); // cache miss
    mockSet.mockRejectedValue(new Error('Redis write error'));

    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 1,
      outcomes: [{ outcome: 'granted', count: 1 }],
      motionTypes: [
        { motion_type: 'mtd', total: 1, granted: 1, denied: 0, granted_in_part: 0, other: 0 },
      ],
      earliest: '2025-07-01',
      latest: '2025-07-01',
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    // Should still return analytics despite cache write failure
    expect(result).not.toBeNull();
    expect(result!.totalRulings).toBe(1);
    expect(result!.rulingsByMotionType[0].grantRate).toBe(1);
  });

  it('handles judge with no rulings (zero totals, null dates)', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true });

    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 0,
      outcomes: [],
      motionTypes: [],
      earliest: null,
      latest: null,
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).not.toBeNull();
    expect(result!.totalRulings).toBe(0);
    expect(result!.rulingsByOutcome).toEqual([]);
    expect(result!.rulingsByMotionType).toEqual([]);
    expect(result!.earliestRuling).toBeNull();
    expect(result!.latestRuling).toBeNull();
  });

  it('computes grantRate correctly for motion types with mixed outcomes', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true });

    const pool = createMockPool({
      judgeExists: true,
      totalRulings: 10,
      outcomes: [
        { outcome: 'granted', count: 4 },
        { outcome: 'denied', count: 3 },
        { outcome: 'granted_in_part', count: 2 },
        { outcome: 'moot', count: 1 },
      ],
      motionTypes: [
        { motion_type: 'msj', total: 6, granted: 3, denied: 1, granted_in_part: 1, other: 1 },
        { motion_type: 'mtd', total: 4, granted: 1, denied: 2, granted_in_part: 1, other: 0 },
      ],
      earliest: '2025-01-01',
      latest: '2025-12-01',
    });

    const { getJudgeAnalytics } = await import('../src/graphql/judge-analytics');
    const result = await getJudgeAnalytics(pool as never, 'judge-1');

    expect(result).not.toBeNull();
    // MSJ grantRate = 3 / (3 + 1 + 1) = 0.6
    expect(result!.rulingsByMotionType[0].grantRate).toBeCloseTo(0.6, 5);
    // MTD grantRate = 1 / (1 + 2 + 1) = 0.25
    expect(result!.rulingsByMotionType[1].grantRate).toBeCloseTo(0.25, 5);
  });
});

// ---------------------------------------------------------------------------
// invalidateJudgeAnalyticsCache
// ---------------------------------------------------------------------------

describe('invalidateJudgeAnalyticsCache', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
  });

  it('deletes the cache key when Redis is available', async () => {
    vi.resetModules();
    setupRedisClient();
    mockDel.mockResolvedValue(1);

    const { invalidateJudgeAnalyticsCache } = await import('../src/graphql/judge-analytics');
    await invalidateJudgeAnalyticsCache('judge-1');

    expect(mockDel).toHaveBeenCalledWith('analytics:judge:judge-1');
  });

  it('does not throw when Redis del fails', async () => {
    vi.resetModules();
    setupRedisClient();
    mockDel.mockRejectedValue(new Error('Redis del error'));

    const { invalidateJudgeAnalyticsCache } = await import('../src/graphql/judge-analytics');

    // Should not throw — cache invalidation failure is non-critical
    await expect(invalidateJudgeAnalyticsCache('judge-1')).resolves.toBeUndefined();
  });

  it('is a silent no-op when Redis is unavailable', async () => {
    vi.resetModules();
    setupRedisClient({ connectFails: true });

    const { invalidateJudgeAnalyticsCache } = await import('../src/graphql/judge-analytics');
    await expect(invalidateJudgeAnalyticsCache('judge-1')).resolves.toBeUndefined();

    // del should not be called since Redis connection failed
    expect(mockDel).not.toHaveBeenCalled();
  });
});
