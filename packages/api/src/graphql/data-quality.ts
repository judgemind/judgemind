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
import { readFileSync } from 'node:fs';
import { resolve, join } from 'node:path';
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
  // Cap at 5000 — with server-side aggregation (four_hour or daily buckets),
  // a 7-day all-counties view needs ~2,700 rows (8 counties * 8 metrics * 42
  // four-hour buckets).  5000 gives headroom for more counties/metrics.
  return Math.min(Math.max(1, n), 5000);
}

// ---------------------------------------------------------------------------
// Per-county threshold loader
// ---------------------------------------------------------------------------

interface CountyThresholds {
  redHours: number;
  yellowHours: number;
}

/**
 * Default fallback thresholds — calibrated against the twice-daily scraper
 * schedule (6:15 AM and 6:15 PM PT) plus a 1-hour FlexibleTimeWindow slack:
 *   yellow: ≥ 13h  (one missed cycle)
 *   red:    > 25h  (two missed cycles)
 */
const DEFAULT_RED_HOURS = 25;
const DEFAULT_YELLOW_HOURS = 13;

/**
 * Load per-county max_expected_gap_hours overrides from data-quality-baselines.json.
 *
 * Priority for the baselines file:
 *   1. /app/data-quality-baselines.json  (Docker / ECS path)
 *   2. <repo-root>/data-quality-baselines.json  (local dev / CI)
 *
 * The alerter (scripts/data-quality-check.py) reads max_expected_gap_hours from
 * the same file and uses it directly as DAILY_SCRAPER_STALE_HOURS.  Both tools
 * derive redHours from this field, keeping alerter and dashboard in sync.
 *
 * Formula: redHours = max_expected_gap_hours ?? 25; yellowHours = redHours - 12
 *
 * Returns an empty Map on any error (no throw — missing baselines fall back to
 * the hardcoded defaults in computeHealthStatus).
 */
export function loadCountyThresholds(): Map<string, CountyThresholds> {
  const candidates = [
    '/app/data-quality-baselines.json',
    // __dirname is packages/api/dist/graphql at runtime; go up 4 to repo root
    resolve(join(resolve(__dirname), '..', '..', '..', '..', 'data-quality-baselines.json')),
    // fallback for test environments where __dirname may vary
    resolve(join(process.cwd(), 'data-quality-baselines.json')),
  ];

  let raw: string | null = null;
  for (const candidate of candidates) {
    try {
      raw = readFileSync(candidate, 'utf8');
      break;
    } catch {
      // try next
    }
  }

  if (!raw) {
    return new Map();
  }

  try {
    const data = JSON.parse(raw) as Record<string, unknown>;
    const counties = data['counties'] as Record<string, Record<string, unknown>> | undefined;
    if (!counties || typeof counties !== 'object') {
      return new Map();
    }

    const result = new Map<string, CountyThresholds>();
    for (const [name, cfg] of Object.entries(counties)) {
      if (!cfg || typeof cfg !== 'object') continue;
      const gap = cfg['max_expected_gap_hours'];
      if (gap !== undefined && typeof gap === 'number' && gap > 0) {
        const redHours = gap;
        const yellowHours = redHours - 12;
        result.set(name, { redHours, yellowHours });
      }
    }
    return result;
  } catch {
    return new Map();
  }
}

// Module-level singleton — loaded once per process (matches alerter behavior).
// Tests can call loadCountyThresholds() directly to bypass the singleton.
let _countyThresholds: Map<string, CountyThresholds> | null = null;

function getCountyThresholds(): Map<string, CountyThresholds> {
  if (_countyThresholds === null) {
    _countyThresholds = loadCountyThresholds();
  }
  return _countyThresholds;
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
  redThresholdHours?: number | null;
  yellowThresholdHours?: number | null;
}

/**
 * Compute health status from the latest metrics for a county.
 *
 * Thresholds are calibrated against the twice-daily scraper schedule (6:15 AM
 * and 6:15 PM PT) plus a 1-hour slack for FlexibleTimeWindow and runtime.
 * Per-county overrides can be provided via redThresholdHours / yellowThresholdHours
 * (read from data-quality-baselines.json max_expected_gap_hours).
 *
 * Defaults (no override):
 * - Green: ruling_count_24h > 0 AND field_completeness_pct >= 90
 *          AND scraper_last_success_age_hours < 13
 * - Yellow: any metric slightly degraded (completeness 70-90%, scraper age 13-25h)
 * - Red: scraper down > 25h OR completeness < 70% OR zero rulings when expected
 */
export function computeHealthStatus(metrics: CountyMetrics): string {
  const { rulingCount24h, fieldCompletenessPct, scraperLastSuccessAgeHours } = metrics;

  // Per-county threshold overrides — fall back to module defaults.
  const redHours = metrics.redThresholdHours ?? DEFAULT_RED_HOURS;
  const yellowHours = metrics.yellowThresholdHours ?? DEFAULT_YELLOW_HOURS;

  // If we have no data at all, report red
  if (rulingCount24h === null && fieldCompletenessPct === null && scraperLastSuccessAgeHours === null) {
    return 'red';
  }

  // Check for red conditions
  if (scraperLastSuccessAgeHours !== null && scraperLastSuccessAgeHours > redHours) return 'red';
  if (fieldCompletenessPct !== null && fieldCompletenessPct < 70) return 'red';
  if (rulingCount24h !== null && rulingCount24h === 0) return 'red';

  // Check for yellow conditions
  if (fieldCompletenessPct !== null && fieldCompletenessPct < 90) return 'yellow';
  if (scraperLastSuccessAgeHours !== null && scraperLastSuccessAgeHours >= yellowHours) return 'yellow';

  // Check for green: all available metrics look good
  const isGreen =
    (rulingCount24h === null || rulingCount24h > 0) &&
    (fieldCompletenessPct === null || fieldCompletenessPct >= 90) &&
    (scraperLastSuccessAgeHours === null || scraperLastSuccessAgeHours < yellowHours);

  return isGreen ? 'green' : 'yellow';
}

// ---------------------------------------------------------------------------
// Data access — dataQualityMetrics
// ---------------------------------------------------------------------------

type MetricResolution = 'hourly' | 'four_hour' | 'daily';

interface MetricsArgs {
  county?: string;
  metricName?: string;
  startDate: string;
  endDate: string;
  resolution?: MetricResolution;
  first?: number;
  after?: string;
}

/**
 * SQL expression for time-bucket truncation.  For `four_hour` we truncate to
 * the nearest 4-hour boundary (00:00, 04:00, 08:00, …).  PostgreSQL's
 * `date_trunc` does not natively support 4-hour intervals, so we compute
 * `date_trunc('hour', recorded_at) - (extract(hour …) % 4) * interval '1h'`.
 */
function bucketExpression(resolution: MetricResolution): string | null {
  switch (resolution) {
    case 'four_hour':
      return `(date_trunc('hour', recorded_at) - (EXTRACT(HOUR FROM recorded_at)::int % 4) * INTERVAL '1 hour')`;
    case 'daily':
      return `date_trunc('day', recorded_at)`;
    default:
      return null; // hourly — no aggregation
  }
}

async function queryMetrics(
  pool: Pool,
  args: MetricsArgs,
): Promise<{
  edges: Array<{ node: Row; cursor: string }>;
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
}> {
  const limit = pageSize(args.first);
  const resolution: MetricResolution = args.resolution ?? 'hourly';
  const bucket = bucketExpression(resolution);

  // --- Aggregated path (four_hour / daily) ---
  if (bucket) {
    return queryAggregatedMetrics(pool, args, bucket, limit);
  }

  // --- Raw (hourly) path — original behaviour ---
  const conditions: string[] = [];
  const params: unknown[] = [];
  let i = 1;

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

/**
 * Aggregated query: groups rows into time buckets and returns AVG(metric_value).
 * Metadata from the most recent record within each bucket is preserved so
 * field-level completeness breakdowns still render in the UI.
 *
 * Cursor pagination is based on (bucket DESC, county DESC, metric_name DESC)
 * since individual row ids are lost during aggregation.  The synthetic id
 * returned to GraphQL is a deterministic string so the client can key on it.
 */
async function queryAggregatedMetrics(
  pool: Pool,
  args: MetricsArgs,
  bucketExpr: string,
  limit: number,
): Promise<{
  edges: Array<{ node: Row; cursor: string }>;
  pageInfo: { hasNextPage: boolean; endCursor: string | null };
}> {
  const conditions: string[] = [];
  const params: unknown[] = [];
  let i = 1;

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

  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';

  // HAVING clause for cursor-based pagination on aggregated results.
  // We paginate on (bucket DESC, county DESC, metric_name DESC).
  let havingClause = '';
  if (args.after) {
    const [curBucket, curCounty, curMetric] = decodeCursor(args.after);
    havingClause = `HAVING (${bucketExpr}, county, metric_name) < ($${i++}::timestamptz, $${i++}, $${i++})`;
    params.push(curBucket, curCounty, curMetric);
  }

  params.push(limit + 1);

  const sql = `
    SELECT
      ${bucketExpr} AS bucket,
      county,
      metric_name,
      AVG(metric_value) AS metric_value,
      (array_agg(metadata ORDER BY recorded_at DESC))[1] AS metadata
    FROM data_quality_metrics
    ${where}
    GROUP BY bucket, county, metric_name
    ${havingClause}
    ORDER BY bucket DESC, county DESC, metric_name DESC
    LIMIT $${i}
  `;

  const { rows } = await pool.query<Row>(sql, params);

  const hasNextPage = rows.length > limit;
  const edges = rows.slice(0, limit);

  return {
    edges: edges.map((row) => {
      const cursorStr = encodeCursor([
        String(row.bucket),
        String(row.county),
        String(row.metric_name),
      ]);
      return {
        node: {
          // Synthetic id from the bucket key — deterministic and unique per bucket
          id: Buffer.from(`${row.bucket}|${row.county}|${row.metric_name}`).toString('base64'),
          recorded_at: row.bucket,
          county: row.county,
          metric_name: row.metric_name,
          metric_value: row.metric_value,
          metadata: row.metadata ?? null,
        },
        cursor: cursorStr,
      };
    }),
    pageInfo: {
      hasNextPage,
      endCursor:
        edges.length > 0
          ? encodeCursor([
              String(edges[edges.length - 1].bucket),
              String(edges[edges.length - 1].county),
              String(edges[edges.length - 1].metric_name),
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

  const thresholds = getCountyThresholds();

  return rows.map((row) => {
    const countyName = row.county as string;
    const countyThresholds = thresholds.get(countyName);

    const metrics: CountyMetrics = {
      county: countyName,
      rulingCount24h: row.ruling_count_24h !== null ? Number(row.ruling_count_24h) : null,
      fieldCompletenessPct:
        row.field_completeness_pct !== null ? Number(row.field_completeness_pct) : null,
      scraperLastSuccessAgeHours:
        row.scraper_last_success_age_hours !== null
          ? Number(row.scraper_last_success_age_hours)
          : null,
      lastUpdated: row.last_updated ? String(row.last_updated) : null,
      redThresholdHours: countyThresholds?.redHours ?? null,
      yellowThresholdHours: countyThresholds?.yellowHours ?? null,
    };

    return {
      county: metrics.county,
      healthStatus: computeHealthStatus(metrics),
      rulingCount24h: metrics.rulingCount24h,
      fieldCompletenessPct: metrics.fieldCompletenessPct,
      scraperLastSuccessAgeHours: metrics.scraperLastSuccessAgeHours,
      lastUpdated: metrics.lastUpdated,
      redThresholdHours: metrics.redThresholdHours,
      yellowThresholdHours: metrics.yellowThresholdHours,
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
export { requireAdmin, type CountyMetrics, type CountyThresholds, type MetricResolution };
