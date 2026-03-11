import type { Pool } from 'pg';
import { GraphQLError } from 'graphql';
import type { AuthUser } from '../auth';

type Row = Record<string, unknown>;

interface DataQualityContext {
  pool: Pool;
  user: AuthUser | null;
}

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

function requireAdmin(user: AuthUser | null): AuthUser {
  if (!user) {
    throw new GraphQLError('Not authenticated', {
      extensions: { code: 'UNAUTHENTICATED' },
    });
  }
  if (user.role !== 'admin') {
    throw new GraphQLError('Admin access required', {
      extensions: { code: 'FORBIDDEN' },
    });
  }
  return user;
}

// ---------------------------------------------------------------------------
// Cursor helpers (keyset pagination by recorded_at DESC, id DESC)
// ---------------------------------------------------------------------------

function encodeCursor(parts: string[]): string {
  return Buffer.from(parts.join('|')).toString('base64');
}

function decodeCursor(cursor: string): string[] {
  return Buffer.from(cursor, 'base64').toString('utf8').split('|');
}

/** Clamp page size: default 100, max 1000. Higher defaults for time-series data. */
function pageSize(limit: number | undefined | null): number {
  const n = limit ?? 100;
  return Math.min(Math.max(1, n), 1000);
}

// ---------------------------------------------------------------------------
// Health status computation
// ---------------------------------------------------------------------------

interface LatestMetrics {
  ruling_count_24h?: number;
  field_completeness_pct?: number;
  scraper_last_success_age_hours?: number;
}

function computeHealthStatus(metrics: LatestMetrics): string {
  const { ruling_count_24h, field_completeness_pct, scraper_last_success_age_hours } = metrics;

  // Red conditions: any critical failure
  if (scraper_last_success_age_hours !== undefined && scraper_last_success_age_hours > 24) {
    return 'red';
  }
  if (field_completeness_pct !== undefined && field_completeness_pct < 70) {
    return 'red';
  }
  if (ruling_count_24h !== undefined && ruling_count_24h === 0) {
    return 'red';
  }

  // Yellow conditions: degraded but not critical
  if (scraper_last_success_age_hours !== undefined && scraper_last_success_age_hours >= 6) {
    return 'yellow';
  }
  if (
    field_completeness_pct !== undefined &&
    field_completeness_pct < 90
  ) {
    return 'yellow';
  }

  // Green: all metrics healthy (or no data yet — optimistic default)
  return 'green';
}

// ---------------------------------------------------------------------------
// Resolvers
// ---------------------------------------------------------------------------

export const dataQualityResolvers = {
  Query: {
    dataQualityMetrics: async (
      _: unknown,
      {
        county,
        metricName,
        startDate,
        endDate,
        limit,
        after,
      }: {
        county?: string;
        metricName?: string;
        startDate: string;
        endDate: string;
        limit?: number;
        after?: string;
      },
      { pool, user }: DataQualityContext,
    ) => {
      requireAdmin(user);

      const size = pageSize(limit);
      const conditions: string[] = [];
      const params: unknown[] = [];
      let i = 1;

      // Required time range
      conditions.push(`recorded_at >= $${i++}::timestamptz`);
      params.push(startDate);
      conditions.push(`recorded_at <= $${i++}::timestamptz`);
      params.push(endDate);

      // Optional filters
      if (county) {
        conditions.push(`county = $${i++}`);
        params.push(county);
      }
      if (metricName) {
        conditions.push(`metric_name = $${i++}`);
        params.push(metricName);
      }

      // Keyset pagination — order by (recorded_at DESC, id DESC)
      if (after) {
        const [recordedAt, id] = decodeCursor(after);
        conditions.push(`(recorded_at, id) < ($${i++}::timestamptz, $${i++}::bigint)`);
        params.push(recordedAt, id);
      }

      const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
      params.push(size + 1);
      const { rows } = await pool.query<Row>(
        `SELECT * FROM data_quality_metrics ${where} ORDER BY recorded_at DESC, id DESC LIMIT $${i}`,
        params,
      );

      const hasNextPage = rows.length > size;
      const edges = rows.slice(0, size);
      return {
        edges: edges.map((row) => ({
          node: row,
          cursor: encodeCursor([String(row.recorded_at), String(row.id)]),
        })),
        pageInfo: {
          hasNextPage,
          endCursor:
            edges.length > 0
              ? encodeCursor([
                  String(edges[edges.length - 1].recorded_at),
                  String(edges[edges.length - 1].id),
                ])
              : null,
        },
      };
    },

    dataQualityOverview: async (
      _: unknown,
      __: unknown,
      { pool, user }: DataQualityContext,
    ) => {
      requireAdmin(user);

      // Fetch the latest value for each (county, metric_name) pair using
      // DISTINCT ON — efficient with the existing composite index.
      const { rows } = await pool.query<Row>(`
        SELECT DISTINCT ON (county, metric_name)
          county, metric_name, metric_value, recorded_at
        FROM data_quality_metrics
        ORDER BY county, metric_name, recorded_at DESC
      `);

      // Group by county
      const countyMap = new Map<
        string,
        { metrics: LatestMetrics; lastUpdated: string | null }
      >();

      for (const row of rows) {
        const county = row.county as string;
        if (!countyMap.has(county)) {
          countyMap.set(county, { metrics: {}, lastUpdated: null });
        }
        const entry = countyMap.get(county)!;
        const metricName = row.metric_name as string;
        const metricValue = Number(row.metric_value);

        if (metricName === 'ruling_count_24h') {
          entry.metrics.ruling_count_24h = metricValue;
        } else if (metricName === 'field_completeness_pct') {
          entry.metrics.field_completeness_pct = metricValue;
        } else if (metricName === 'scraper_last_success_age_hours') {
          entry.metrics.scraper_last_success_age_hours = metricValue;
        }

        // Track most recent recorded_at across all metrics for this county
        const recordedAt = row.recorded_at as string;
        if (!entry.lastUpdated || recordedAt > entry.lastUpdated) {
          entry.lastUpdated = recordedAt;
        }
      }

      const results: Array<{
        county: string;
        healthStatus: string;
        rulingCount24h: number | null;
        fieldCompletenessPct: number | null;
        scraperLastSuccessAgeHours: number | null;
        lastUpdated: string | null;
      }> = [];

      for (const [county, { metrics, lastUpdated }] of countyMap) {
        results.push({
          county,
          healthStatus: computeHealthStatus(metrics),
          rulingCount24h: metrics.ruling_count_24h ?? null,
          fieldCompletenessPct: metrics.field_completeness_pct ?? null,
          scraperLastSuccessAgeHours: metrics.scraper_last_success_age_hours ?? null,
          lastUpdated,
        });
      }

      // Sort alphabetically by county for stable ordering
      results.sort((a, b) => a.county.localeCompare(b.county));

      return results;
    },
  },

  // Field resolvers — snake_case DB columns -> camelCase GraphQL fields
  DataQualityMetric: {
    recordedAt: (row: Row) => row.recorded_at,
    metricName: (row: Row) => row.metric_name,
    metricValue: (row: Row) => Number(row.metric_value),
    metadata: (row: Row) => (row.metadata ? JSON.stringify(row.metadata) : null),
  },
};

// Export for testing
export { computeHealthStatus };
export type { LatestMetrics };
