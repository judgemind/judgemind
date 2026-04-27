import type { Pool } from 'pg';
import { createClient, type RedisClientType } from 'redis';
import type { Loaders } from './dataloader';

type Row = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Redis caching — lazy singleton, fail-open (same pattern as rate-limit.ts)
// ---------------------------------------------------------------------------

const CACHE_TTL_SECONDS = 60 * 60; // 1 hour

let redisClient: RedisClientType | null = null;

async function getRedis(): Promise<RedisClientType | null> {
  if (redisClient) return redisClient;
  try {
    redisClient = createClient({
      url: process.env.REDIS_URL ?? 'redis://localhost:6379',
      socket: { connectTimeout: 1000, reconnectStrategy: false },
    });
    await redisClient.connect();
    return redisClient;
  } catch {
    // Redis unavailable — proceed without cache
    redisClient = null;
    return null;
  }
}

function cacheKey(judgeId: string): string {
  return `analytics:judge:${judgeId}`;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface OutcomeCount {
  outcome: string;
  count: number;
}

interface MotionStats {
  motionType: string;
  total: number;
  granted: number;
  denied: number;
  grantedInPart: number;
  other: number;
  grantRate: number;
}

interface JudgeAnalytics {
  judgeId: string;
  totalRulings: number;
  rulingsByOutcome: OutcomeCount[];
  rulingsByMotionType: MotionStats[];
  earliestRuling: string | null;
  latestRuling: string | null;
}

// ---------------------------------------------------------------------------
// grantRate computation
// ---------------------------------------------------------------------------

/**
 * Compute grant rate: granted / (granted + denied + grantedInPart).
 * Excludes procedural outcomes (moot, continued, etc.) from denominator.
 * Returns 0 when the denominator is zero.
 */
function computeGrantRate(granted: number, denied: number, grantedInPart: number): number {
  const denominator = granted + denied + grantedInPart;
  if (denominator === 0) return 0;
  return granted / denominator;
}

// ---------------------------------------------------------------------------
// SQL queries
// ---------------------------------------------------------------------------

async function queryAnalytics(pool: Pool, judgeId: string): Promise<JudgeAnalytics> {
  // Run all 4 independent queries concurrently — total latency is max(queries)
  // instead of sum(queries). Each query uses its own connection from the pool.
  const [totalResult, outcomeResult, motionResult, dateResult] = await Promise.all([
    // Total rulings count
    pool.query<Row>('SELECT COUNT(*)::int as count FROM rulings WHERE judge_id = $1', [judgeId]),

    // By outcome
    pool.query<Row>(
      `SELECT outcome::text, COUNT(*)::int as count
       FROM rulings
       WHERE judge_id = $1 AND outcome IS NOT NULL
       GROUP BY outcome`,
      [judgeId],
    ),

    // By motion type (with outcome breakdown)
    pool.query<Row>(
      `SELECT motion_type,
         COUNT(*)::int as total,
         COUNT(*) FILTER (WHERE outcome = 'granted')::int as granted,
         COUNT(*) FILTER (WHERE outcome = 'denied')::int as denied,
         COUNT(*) FILTER (WHERE outcome IN ('granted_in_part','denied_in_part'))::int as granted_in_part,
         COUNT(*) FILTER (WHERE outcome NOT IN ('granted','denied','granted_in_part','denied_in_part') AND outcome IS NOT NULL)::int as other
       FROM rulings
       WHERE judge_id = $1 AND motion_type IS NOT NULL
       GROUP BY motion_type
       ORDER BY total DESC`,
      [judgeId],
    ),

    // Date range
    pool.query<Row>(
      `SELECT
         MIN(hearing_date)::text as earliest,
         MAX(hearing_date)::text as latest
       FROM rulings
       WHERE judge_id = $1`,
      [judgeId],
    ),
  ]);

  const totalRulings = (totalResult.rows[0]?.count as number) ?? 0;

  const rulingsByOutcome: OutcomeCount[] = outcomeResult.rows.map((row) => ({
    outcome: row.outcome as string,
    count: row.count as number,
  }));

  const rulingsByMotionType: MotionStats[] = motionResult.rows.map((row) => {
    const granted = row.granted as number;
    const denied = row.denied as number;
    const grantedInPart = row.granted_in_part as number;
    return {
      motionType: row.motion_type as string,
      total: row.total as number,
      granted,
      denied,
      grantedInPart,
      other: row.other as number,
      grantRate: computeGrantRate(granted, denied, grantedInPart),
    };
  });

  const earliestRuling = (dateResult.rows[0]?.earliest as string) ?? null;
  const latestRuling = (dateResult.rows[0]?.latest as string) ?? null;

  return {
    judgeId,
    totalRulings,
    rulingsByOutcome,
    rulingsByMotionType,
    earliestRuling,
    latestRuling,
  };
}

// ---------------------------------------------------------------------------
// Batched SQL queries — fetch analytics for multiple judges in 4 queries
// ---------------------------------------------------------------------------

/**
 * Fetch analytics for multiple judges using exactly 4 batched SQL queries,
 * each using `WHERE judge_id = ANY($1) GROUP BY judge_id`.
 *
 * Seeds a zero-analytics object for every input ID up front so judges with
 * no rulings always get an entry. Returns an empty Map (no DB calls) when
 * judgeIds is empty.
 */
export async function getJudgeAnalyticsBatch(
  pool: Pool,
  judgeIds: string[],
): Promise<Map<string, JudgeAnalytics>> {
  if (judgeIds.length === 0) return new Map();

  // Seed default zero-analytics for every requested judge
  const result = new Map<string, JudgeAnalytics>(
    judgeIds.map((id) => [
      id,
      {
        judgeId: id,
        totalRulings: 0,
        rulingsByOutcome: [],
        rulingsByMotionType: [],
        earliestRuling: null,
        latestRuling: null,
      },
    ]),
  );

  // Run all 4 queries in parallel — same shape as queryAnalytics, but batched
  const [totalResult, outcomeResult, motionResult, dateResult] = await Promise.all([
    // Query 1: total rulings count per judge
    pool.query<Row>(
      `SELECT judge_id, COUNT(*)::int AS count
       FROM rulings
       WHERE judge_id = ANY($1)
       GROUP BY judge_id`,
      [judgeIds],
    ),

    // Query 2: rulings by outcome per judge
    pool.query<Row>(
      `SELECT judge_id, outcome::text, COUNT(*)::int AS count
       FROM rulings
       WHERE judge_id = ANY($1) AND outcome IS NOT NULL
       GROUP BY judge_id, outcome`,
      [judgeIds],
    ),

    // Query 3: rulings by motion type with outcome breakdown per judge
    pool.query<Row>(
      `SELECT judge_id, motion_type,
         COUNT(*)::int AS total,
         COUNT(*) FILTER (WHERE outcome = 'granted')::int AS granted,
         COUNT(*) FILTER (WHERE outcome = 'denied')::int AS denied,
         COUNT(*) FILTER (WHERE outcome IN ('granted_in_part','denied_in_part'))::int AS granted_in_part,
         COUNT(*) FILTER (WHERE outcome NOT IN ('granted','denied','granted_in_part','denied_in_part') AND outcome IS NOT NULL)::int AS other
       FROM rulings
       WHERE judge_id = ANY($1) AND motion_type IS NOT NULL
       GROUP BY judge_id, motion_type
       ORDER BY total DESC`,
      [judgeIds],
    ),

    // Query 4: date range per judge
    pool.query<Row>(
      `SELECT judge_id,
         MIN(hearing_date)::text AS earliest,
         MAX(hearing_date)::text AS latest
       FROM rulings
       WHERE judge_id = ANY($1)
       GROUP BY judge_id`,
      [judgeIds],
    ),
  ]);

  // Merge total counts
  for (const row of totalResult.rows) {
    const entry = result.get(row.judge_id as string);
    if (entry) {
      entry.totalRulings = row.count as number;
    }
  }

  // Merge outcome counts (group by judge_id)
  const outcomesByJudge = new Map<string, OutcomeCount[]>();
  for (const row of outcomeResult.rows) {
    const id = row.judge_id as string;
    let arr = outcomesByJudge.get(id);
    if (!arr) {
      arr = [];
      outcomesByJudge.set(id, arr);
    }
    arr.push({ outcome: row.outcome as string, count: row.count as number });
  }
  for (const [id, outcomes] of outcomesByJudge) {
    const entry = result.get(id);
    if (entry) {
      entry.rulingsByOutcome = outcomes;
    }
  }

  // Merge motion type stats (group by judge_id)
  const motionsByJudge = new Map<string, MotionStats[]>();
  for (const row of motionResult.rows) {
    const id = row.judge_id as string;
    let arr = motionsByJudge.get(id);
    if (!arr) {
      arr = [];
      motionsByJudge.set(id, arr);
    }
    const granted = row.granted as number;
    const denied = row.denied as number;
    const grantedInPart = row.granted_in_part as number;
    arr.push({
      motionType: row.motion_type as string,
      total: row.total as number,
      granted,
      denied,
      grantedInPart,
      other: row.other as number,
      grantRate: computeGrantRate(granted, denied, grantedInPart),
    });
  }
  for (const [id, motions] of motionsByJudge) {
    const entry = result.get(id);
    if (entry) {
      entry.rulingsByMotionType = motions;
    }
  }

  // Merge date ranges
  for (const row of dateResult.rows) {
    const entry = result.get(row.judge_id as string);
    if (entry) {
      entry.earliestRuling = (row.earliest as string) ?? null;
      entry.latestRuling = (row.latest as string) ?? null;
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// Public API — resolver entry point
// ---------------------------------------------------------------------------

/**
 * Fetch judge analytics with Redis caching (1-hour TTL, fail-open).
 * Returns null if the judge does not exist.
 */
export async function getJudgeAnalytics(
  pool: Pool,
  judgeId: string,
): Promise<JudgeAnalytics | null> {
  // Check if judge exists
  const { rows: judgeRows } = await pool.query<Row>('SELECT id FROM judges WHERE id = $1', [
    judgeId,
  ]);
  if (judgeRows.length === 0) return null;

  // Try cache first
  const redis = await getRedis();
  if (redis) {
    try {
      const cached = await redis.get(cacheKey(judgeId));
      if (cached) {
        return JSON.parse(cached) as JudgeAnalytics;
      }
    } catch {
      // Cache read failed — proceed to DB
    }
  }

  const analytics = await queryAnalytics(pool, judgeId);

  // Write to cache (best-effort)
  if (redis) {
    try {
      await redis.set(cacheKey(judgeId), JSON.stringify(analytics), { EX: CACHE_TTL_SECONDS });
    } catch {
      // Cache write failed — non-critical
    }
  }

  return analytics;
}

/**
 * Fetch analytics for multiple judges using DataLoader for existence checks
 * and batched SQL queries for analytics. Issues at most 5 total DB queries
 * (1 DataLoader existence query + 4 analytics batch queries) regardless of
 * the number of input IDs.
 *
 * Returns an array in the same order as the input IDs. Non-existent judges
 * produce null entries. Capped at 10 judges to prevent abuse.
 *
 * Redis caching is applied per-judge at the MGET/MSET level (best-effort,
 * fail-open). Cache hits bypass getJudgeAnalyticsBatch for those IDs.
 */
export async function getMultipleJudgeAnalytics(
  pool: Pool,
  loaders: Loaders,
  judgeIds: string[],
): Promise<(JudgeAnalytics | null)[]> {
  const capped = judgeIds.slice(0, 10);
  if (capped.length === 0) return [];

  // 1 query: DataLoader-coalesced existence check for all IDs
  const judgeResults = await loaders.judgeLoader.loadMany(capped);

  // Determine which IDs actually exist
  const existingIds = capped.filter(
    (_, i) => judgeResults[i] !== null && !(judgeResults[i] instanceof Error),
  );

  if (existingIds.length === 0) {
    return capped.map(() => null);
  }

  // Try Redis MGET for existing IDs (best-effort, fail-open)
  const analyticsMap = new Map<string, JudgeAnalytics>();
  let missedIds = existingIds;

  const redis = await getRedis();
  if (redis) {
    try {
      const keys = existingIds.map(cacheKey);
      const cached = await redis.mGet(keys);
      const misses: string[] = [];
      for (let i = 0; i < existingIds.length; i++) {
        const raw = cached[i];
        if (raw) {
          analyticsMap.set(existingIds[i], JSON.parse(raw) as JudgeAnalytics);
        } else {
          misses.push(existingIds[i]);
        }
      }
      missedIds = misses;
    } catch {
      // Redis unavailable or read error — treat all as misses
      missedIds = existingIds;
    }
  }

  // 4 queries: batch fetch analytics for cache-miss IDs
  if (missedIds.length > 0) {
    const batchResult = await getJudgeAnalyticsBatch(pool, missedIds);
    for (const [id, analytics] of batchResult) {
      analyticsMap.set(id, analytics);
    }

    // Best-effort write fetched analytics back to Redis with 1h TTL
    if (redis) {
      for (const id of missedIds) {
        const analytics = analyticsMap.get(id);
        if (analytics) {
          try {
            await redis.set(cacheKey(id), JSON.stringify(analytics), { EX: CACHE_TTL_SECONDS });
          } catch {
            // Cache write failed — non-critical
          }
        }
      }
    }
  }

  // Build return array preserving input order
  return capped.map((id) => {
    const judgeResult = judgeResults[capped.indexOf(id)];
    if (judgeResult === null || judgeResult instanceof Error) return null;
    return analyticsMap.get(id) ?? null;
  });
}

/**
 * Invalidate cached analytics for a judge (call when new ruling is indexed).
 */
export async function invalidateJudgeAnalyticsCache(judgeId: string): Promise<void> {
  const redis = await getRedis();
  if (redis) {
    try {
      await redis.del(cacheKey(judgeId));
    } catch {
      // Cache invalidation failed — non-critical, TTL will expire
    }
  }
}

// Export for testing
export { computeGrantRate, type JudgeAnalytics, type MotionStats, type OutcomeCount };
