/**
 * In-memory TTL cache for platformStats counts.
 *
 * The platformStats resolver runs three COUNT(*) queries on every call.
 * Since these are aggregate counts displayed on the home page, they don't
 * need to be precise to the second — a 5-minute TTL is acceptable.
 *
 * This avoids expensive sequential scans on every page load as the
 * rulings table grows. An in-flight promise is cached to prevent
 * cache stampedes — concurrent requests during a cache miss share
 * the same database query rather than each issuing their own.
 */

import type { Pool } from 'pg';

export interface PlatformStats {
  countiesCount: number;
  rulingsCount: number;
  judgesCount: number;
}

interface CacheEntry {
  data: PlatformStats;
  expiresAt: number; // epoch ms
}

/** Default TTL: 5 minutes (in milliseconds). */
const DEFAULT_TTL_MS = 5 * 60 * 1000;

let cached: CacheEntry | null = null;
let inFlightPromise: Promise<PlatformStats> | null = null;

/**
 * Fetch platform stats, returning cached values if fresh.
 *
 * Concurrent callers during a cache miss share the same in-flight
 * promise, preventing cache stampedes (duplicate queries).
 *
 * @param pool - pg Pool for database queries
 * @param ttlMs - cache TTL in milliseconds (default 5 minutes)
 * @returns PlatformStats with counts for counties, rulings, and judges
 */
export async function getPlatformStats(
  pool: Pool,
  ttlMs: number = DEFAULT_TTL_MS,
): Promise<PlatformStats> {
  const now = Date.now();

  if (cached && now < cached.expiresAt) {
    return cached.data;
  }

  // If a fetch is already in flight, coalesce onto it
  if (inFlightPromise) {
    return inFlightPromise;
  }

  inFlightPromise = (async () => {
    try {
      const [countiesResult, rulingsResult, judgesResult] = await Promise.all([
        pool.query<{ count: string }>(
          `SELECT COUNT(DISTINCT county) AS count FROM courts WHERE is_active = true`,
        ),
        pool.query<{ count: string }>(`SELECT COUNT(*) AS count FROM rulings`),
        pool.query<{ count: string }>(
          `SELECT COUNT(*) AS count FROM judges WHERE is_active = true`,
        ),
      ]);

      const data: PlatformStats = {
        countiesCount: parseInt(countiesResult.rows[0].count, 10),
        rulingsCount: parseInt(rulingsResult.rows[0].count, 10),
        judgesCount: parseInt(judgesResult.rows[0].count, 10),
      };

      cached = { data, expiresAt: Date.now() + ttlMs };
      return data;
    } finally {
      inFlightPromise = null;
    }
  })();

  return inFlightPromise;
}

/**
 * Clear the cache. Useful for testing and for manual invalidation.
 */
export function clearPlatformStatsCache(): void {
  cached = null;
  inFlightPromise = null;
}
