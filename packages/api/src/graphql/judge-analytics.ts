import type { Pool } from 'pg';
import { createClient, type RedisClientType } from 'redis';

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
  // Total rulings count
  const totalResult = await pool.query<Row>(
    'SELECT COUNT(*)::int as count FROM rulings WHERE judge_id = $1',
    [judgeId],
  );
  const totalRulings = (totalResult.rows[0]?.count as number) ?? 0;

  // By outcome
  const outcomeResult = await pool.query<Row>(
    `SELECT outcome::text, COUNT(*)::int as count
     FROM rulings
     WHERE judge_id = $1 AND outcome IS NOT NULL
     GROUP BY outcome`,
    [judgeId],
  );
  const rulingsByOutcome: OutcomeCount[] = outcomeResult.rows.map((row) => ({
    outcome: row.outcome as string,
    count: row.count as number,
  }));

  // By motion type (with outcome breakdown)
  const motionResult = await pool.query<Row>(
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
  );
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

  // Date range
  const dateResult = await pool.query<Row>(
    `SELECT
       MIN(hearing_date)::text as earliest,
       MAX(hearing_date)::text as latest
     FROM rulings
     WHERE judge_id = $1`,
    [judgeId],
  );
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
