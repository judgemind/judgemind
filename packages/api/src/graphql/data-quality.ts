/**
 * Data quality metrics resolvers and data access.
 *
 * Provides two queries:
 * - dataQualityMetrics: time-series metrics with filtering
 * - dataQualityOverview: per-county health status summary
 *
 * Both queries are admin-only.
 */

import type { Pool } from 'pg';
import { GraphQLError } from 'graphql';
import type { AuthUser } from '../auth';

type Row = Record<string, unknown>;

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
// Cursor helpers (same pattern as resolvers.ts)
// ---------------------------------------------------------------------------

function encodeCursor(parts: string[]): string {
  return Buffer.from(parts.join('|')).toString('base64');
}

function decodeCursor(cursor: string): string[] {
  return Buffer.from(cursor, 'base64').toString('utf8').split('|');
}

function pageSize(first: number | undefined | null): number {
  const n = first ?? 20;
  // Cap raised from 100 to 2000 so the data quality dashboard can fetch a
  // full 7-day window of metrics (8 counties * 8 metrics * 168 hourly
  // snapshots ≈ 10,752 rows).  The old 100-row cap meant charts received
  // only ~1 hour of data, making them appear nearly empty.
  return Math.min(Math.max(1, n), 2000);
}

// ---------------------------------------------------------------------------
// Health status logic
// ---------------------------------------------------------------------------

interface CountyMetrics {
  county: string;
  rulingCount24h: number | null;
  fieldCompletenessPct: number | null;
  scraperLastSuccessAgeHours: number | null;
  lastUpdated: string | null;
}

/**
 * Compute health status from the latest metrics for a county.
 *
 * - Green: ruling_count_24h > 0 AND field_completeness_pct >= 90
 *          AND scraper_last_success_age_hours < 6
 * - Yellow: any metric slightly degraded (completeness 70-90%, scraper age 6-24h)
 * - Red: scraper down > 24h OR completeness < 70% OR zero rulings when expected
 */
export function computeHealthStatus(metrics: CountyMetrics): string {
  const { rulingCount24h, fieldCompletenessPct, scraperLastSuccessAgeHours } = metrics;

  // If we have no data at all, report red
  if (rulingCount24h === null && fieldCompletenessPct === null && scraperLastSuccessAgeHours === null) {
    return 'red';
  }

  // Check for red conditions
  if (scraperLastSuccessAgeHours !== null && scraperLastSuccessAgeHours > 24) return 'red';
  if (fieldCompletenessPct !== null && fieldCompletenessPct < 70) return 'red';
  if (rulingCount24h !== null && rulingCount24h === 0) return 'red';

  // Check for yellow conditions
  if (fieldCompletenessPct !== null && fieldCompletenessPct < 90) return 'yellow';
  if (scraperLastSuccessAgeHours !== null && scraperLastSuccessAgeHours >= 6) return 'yellow';

  // Check for green: all available metrics look good
  const isGreen =
    (rulingCount24h === null || rulingCount24h > 0) &&
    (fieldCompletenessPct === null || fieldCompletenessPct >= 90) &&
    (scraperLastSuccessAgeHours === null || scraperLastSuccessAgeHours < 6);

  return isGreen ? 'green' : 'yellow';
}

// ---------------------------------------------------------------------------
// Data access — dataQualityMetrics
// ---------------------------------------------------------------------------

interface MetricsArgs {
  county?: string;
  metricName?: string;
  startDate: string;
  endDate: string;
  first?: number;
  after?: string;
}

async function queryMetrics(
  pool: Pool,
  args: MetricsArgs,
): Promise<{
  edges: Array<{ node: Row; cursor: string }>;
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
}> {
  const limit = pageSize(args.first);
  const conditions: string[] = [];
  const params: unknown[] = [];
  let i = 1;

  // Required time range filter
  conditions.push(`recorded_at >= $${i++}::timestamptz`);
  params.push(args.startDate);
  conditions.push(`recorded_at <= $${i++}::timestamptz`);
  params.push(args.endDate);

  if (args.county) {
    conditions.push(`county = $${i++}`);
    params.push(args.county);
  }
  if (args.metricName) {
    conditions.push(`metric_name = $${i++}`);
    params.push(args.metricName);
  }

  // Keyset pagination — order by (recorded_at DESC, id DESC)
  if (args.after) {
    const [recordedAt, id] = decodeCursor(args.after);
    conditions.push(`(recorded_at, id) < ($${i++}::timestamptz, $${i++}::bigint)`);
    params.push(recordedAt, id);
  }

  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
  params.push(limit + 1);
  const { rows } = await pool.query<Row>(
    `SELECT * FROM data_quality_metrics ${where} ORDER BY recorded_at DESC, id DESC LIMIT $${i}`,
    params,
  );

  const hasNextPage = rows.length > limit;
  const edges = rows.slice(0, limit);
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
}

// ---------------------------------------------------------------------------
// Data access — dataQualityOverview
// ---------------------------------------------------------------------------

async function queryOverview(pool: Pool): Promise<Row[]> {
  // Get the latest value for each (county, metric_name) pair using DISTINCT ON.
  // This is efficient with the idx_dqm_county_metric_time index.
  const { rows } = await pool.query<Row>(`
    WITH latest AS (
      SELECT DISTINCT ON (county, metric_name)
        county,
        metric_name,
        metric_value,
        recorded_at
      FROM data_quality_metrics
      ORDER BY county, metric_name, recorded_at DESC
    )
    SELECT
      county,
      MAX(CASE WHEN metric_name = 'ruling_count_24h' THEN metric_value END) AS ruling_count_24h,
      MAX(CASE WHEN metric_name = 'field_completeness_pct' THEN metric_value END) AS field_completeness_pct,
      MAX(CASE WHEN metric_name = 'scraper_last_success_age_hours' THEN metric_value END) AS scraper_last_success_age_hours,
      MAX(recorded_at) AS last_updated
    FROM latest
    GROUP BY county
    ORDER BY county
  `);

  return rows.map((row) => {
    const metrics: CountyMetrics = {
      county: row.county as string,
      rulingCount24h: row.ruling_count_24h !== null ? Number(row.ruling_count_24h) : null,
      fieldCompletenessPct:
        row.field_completeness_pct !== null ? Number(row.field_completeness_pct) : null,
      scraperLastSuccessAgeHours:
        row.scraper_last_success_age_hours !== null
          ? Number(row.scraper_last_success_age_hours)
          : null,
      lastUpdated: row.last_updated ? String(row.last_updated) : null,
    };

    return {
      county: metrics.county,
      healthStatus: computeHealthStatus(metrics),
      rulingCount24h: metrics.rulingCount24h,
      fieldCompletenessPct: metrics.fieldCompletenessPct,
      scraperLastSuccessAgeHours: metrics.scraperLastSuccessAgeHours,
      lastUpdated: metrics.lastUpdated,
    };
  });
}

// ---------------------------------------------------------------------------
// Context type
// ---------------------------------------------------------------------------

interface DataQualityContext {
  pool: Pool;
  user: AuthUser | null;
}

// ---------------------------------------------------------------------------
// Resolvers
// ---------------------------------------------------------------------------

export const dataQualityResolvers = {
  Query: {
    dataQualityMetrics: async (
      _: unknown,
      args: MetricsArgs,
      { pool, user }: DataQualityContext,
    ) => {
      requireAdmin(user);
      return queryMetrics(pool, args);
    },

    dataQualityOverview: async (_: unknown, __: unknown, { pool, user }: DataQualityContext) => {
      requireAdmin(user);
      return queryOverview(pool);
    },
  },

  DataQualityMetric: {
    recordedAt: (row: Row) => row.recorded_at,
    metricName: (row: Row) => row.metric_name,
    metricValue: (row: Row) => Number(row.metric_value),
    metadata: (row: Row) => (row.metadata ? JSON.stringify(row.metadata) : null),
  },

  DataQualityOverview: {
    healthStatus: (row: Row) => row.healthStatus,
    rulingCount24h: (row: Row) => (row.rulingCount24h !== undefined ? row.rulingCount24h : null),
    fieldCompletenessPct: (row: Row) =>
      row.fieldCompletenessPct !== undefined ? row.fieldCompletenessPct : null,
    scraperLastSuccessAgeHours: (row: Row) =>
      row.scraperLastSuccessAgeHours !== undefined ? row.scraperLastSuccessAgeHours : null,
    lastUpdated: (row: Row) => row.lastUpdated ?? null,
  },
};

// Export for testing
export { requireAdmin, type CountyMetrics };
