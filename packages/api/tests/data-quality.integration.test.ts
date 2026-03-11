/**
 * Integration tests for data quality metrics GraphQL resolvers.
 *
 * Tests both `dataQualityMetrics` and `dataQualityOverview` queries,
 * including auth, filtering, pagination, and health status computation.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import { applyMigrations } from './setup-db';
import { signAccessToken } from '../src/auth';

// Match type parsers from src/data-access/db.ts
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind',
});

let app: FastifyInstance;
let adminUserId: string;
let regularUserId: string;
let adminToken: string;
let regularToken: string;

// ---------------------------------------------------------------------------
// Seed / cleanup
// ---------------------------------------------------------------------------

async function seedData(): Promise<void> {
  // Create admin user
  const { rows: adminRows } = await pool.query<{ id: string }>(
    `INSERT INTO users (email, password_hash, is_active, email_verified, role)
     VALUES ('dq-admin@test.com', 'hash', true, true, 'admin')
     RETURNING id`,
  );
  adminUserId = adminRows[0].id;
  adminToken = signAccessToken({ sub: adminUserId, email: 'dq-admin@test.com', role: 'admin' });

  // Create regular user
  const { rows: regularRows } = await pool.query<{ id: string }>(
    `INSERT INTO users (email, password_hash, is_active, email_verified, role)
     VALUES ('dq-user@test.com', 'hash', true, true, 'user')
     RETURNING id`,
  );
  regularUserId = regularRows[0].id;
  regularToken = signAccessToken({ sub: regularUserId, email: 'dq-user@test.com', role: 'user' });

  // Seed data quality metrics for two counties
  // Los Angeles — healthy county (green)
  await pool.query(
    `INSERT INTO data_quality_metrics (recorded_at, county, metric_name, metric_value, metadata)
     VALUES
       ('2026-03-10T10:00:00Z', 'Los Angeles', 'ruling_count_24h', 42, NULL),
       ('2026-03-10T10:00:00Z', 'Los Angeles', 'field_completeness_pct', 95, '{"judge": 100, "motion_type": 90}'),
       ('2026-03-10T10:00:00Z', 'Los Angeles', 'scraper_last_success_age_hours', 1.5, NULL),
       ('2026-03-10T09:00:00Z', 'Los Angeles', 'ruling_count_24h', 40, NULL),
       ('2026-03-10T09:00:00Z', 'Los Angeles', 'field_completeness_pct', 94, NULL)`,
  );

  // Orange — degraded county (yellow: completeness 75%, scraper age 8h)
  await pool.query(
    `INSERT INTO data_quality_metrics (recorded_at, county, metric_name, metric_value, metadata)
     VALUES
       ('2026-03-10T10:00:00Z', 'Orange', 'ruling_count_24h', 10, NULL),
       ('2026-03-10T10:00:00Z', 'Orange', 'field_completeness_pct', 75, NULL),
       ('2026-03-10T10:00:00Z', 'Orange', 'scraper_last_success_age_hours', 8, NULL)`,
  );

  // San Francisco — critical county (red: scraper down 30h)
  await pool.query(
    `INSERT INTO data_quality_metrics (recorded_at, county, metric_name, metric_value, metadata)
     VALUES
       ('2026-03-10T10:00:00Z', 'San Francisco', 'ruling_count_24h', 0, NULL),
       ('2026-03-10T10:00:00Z', 'San Francisco', 'field_completeness_pct', 60, NULL),
       ('2026-03-10T10:00:00Z', 'San Francisco', 'scraper_last_success_age_hours', 30, NULL)`,
  );
}

async function cleanupData(): Promise<void> {
  await pool.query(
    `DELETE FROM data_quality_metrics WHERE county IN ('Los Angeles', 'Orange', 'San Francisco')`,
  );
  if (adminUserId) {
    await pool.query(`DELETE FROM users WHERE id = $1`, [adminUserId]);
  }
  if (regularUserId) {
    await pool.query(`DELETE FROM users WHERE id = $1`, [regularUserId]);
  }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeAll(async () => {
  applyMigrations();
  await seedData();
  app = await buildApp(pool);
}, 30_000);

afterAll(async () => {
  await app?.close();
  await cleanupData();
  await pool.end();
}, 15_000);

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

async function gql(
  query: string,
  variables?: Record<string, unknown>,
  token?: string,
) {
  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers,
    payload: JSON.stringify({ query, variables }),
  });
  return JSON.parse(res.body) as { data?: Record<string, unknown>; errors?: unknown[] };
}

// ---------------------------------------------------------------------------
// Tests — dataQualityMetrics
// ---------------------------------------------------------------------------

describe('dataQualityMetrics', () => {
  it('requires authentication', async () => {
    const body = await gql(`{
      dataQualityMetrics(startDate: "2026-03-01", endDate: "2026-03-31") {
        edges { node { id } }
      }
    }`);
    expect(body.errors).toBeDefined();
    expect(body.errors![0]).toHaveProperty('extensions');
    const ext = (body.errors![0] as Record<string, unknown>).extensions as Record<string, string>;
    expect(ext.code).toBe('UNAUTHENTICATED');
  });

  it('requires admin role', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(startDate: "2026-03-01", endDate: "2026-03-31") {
          edges { node { id } }
        }
      }`,
      undefined,
      regularToken,
    );
    expect(body.errors).toBeDefined();
    const ext = (body.errors![0] as Record<string, unknown>).extensions as Record<string, string>;
    expect(ext.code).toBe('FORBIDDEN');
  });

  it('returns metrics within time range', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(startDate: "2026-03-10T00:00:00Z", endDate: "2026-03-10T23:59:59Z") {
          edges {
            node { id recordedAt county metricName metricValue metadata }
            cursor
          }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{
      node: { id: string; county: string; metricName: string; metricValue: number };
      cursor: string;
    }>;
    // We seeded 10 rows total for this date
    expect(edges.length).toBe(10);
    // All should have valid fields
    for (const edge of edges) {
      expect(edge.node.id).toBeTruthy();
      expect(edge.node.county).toBeTruthy();
      expect(edge.node.metricName).toBeTruthy();
      expect(typeof edge.node.metricValue).toBe('number');
      expect(typeof edge.cursor).toBe('string');
    }
  });

  it('filters by county', async () => {
    const body = await gql(
      `query($county: String!) {
        dataQualityMetrics(county: $county, startDate: "2026-03-10T00:00:00Z", endDate: "2026-03-10T23:59:59Z") {
          edges { node { county } }
        }
      }`,
      { county: 'Los Angeles' },
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { county: string } }>;
    expect(edges.length).toBe(5); // 3 at 10:00 + 2 at 09:00
    for (const edge of edges) {
      expect(edge.node.county).toBe('Los Angeles');
    }
  });

  it('filters by metricName', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(metricName: "ruling_count_24h", startDate: "2026-03-10T00:00:00Z", endDate: "2026-03-10T23:59:59Z") {
          edges { node { metricName county } }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { metricName: string } }>;
    expect(edges.length).toBe(4); // LA 10:00, LA 09:00, Orange, SF
    for (const edge of edges) {
      expect(edge.node.metricName).toBe('ruling_count_24h');
    }
  });

  it('supports keyset pagination', async () => {
    // Page 1: get first 3 results
    const page1 = await gql(
      `{
        dataQualityMetrics(startDate: "2026-03-10T00:00:00Z", endDate: "2026-03-10T23:59:59Z", limit: 3) {
          edges { node { id } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(page1.errors).toBeUndefined();
    const p1 = page1.data?.dataQualityMetrics as Record<string, unknown>;
    const p1Edges = p1.edges as Array<{ node: { id: string }; cursor: string }>;
    expect(p1Edges).toHaveLength(3);
    expect((p1.pageInfo as Record<string, unknown>).hasNextPage).toBe(true);

    const cursor = (p1.pageInfo as Record<string, unknown>).endCursor as string;

    // Page 2: get next page using cursor
    const page2 = await gql(
      `query($after: String!) {
        dataQualityMetrics(startDate: "2026-03-10T00:00:00Z", endDate: "2026-03-10T23:59:59Z", limit: 3, after: $after) {
          edges { node { id } }
          pageInfo { hasNextPage }
        }
      }`,
      { after: cursor },
      adminToken,
    );
    expect(page2.errors).toBeUndefined();
    const p2 = page2.data?.dataQualityMetrics as Record<string, unknown>;
    const p2Edges = p2.edges as Array<{ node: { id: string } }>;
    expect(p2Edges.length).toBeGreaterThan(0);

    // Ensure no overlap between pages
    const p1Ids = new Set(p1Edges.map((e) => e.node.id));
    for (const edge of p2Edges) {
      expect(p1Ids.has(edge.node.id)).toBe(false);
    }
  });

  it('returns empty connection outside time range', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(startDate: "2020-01-01", endDate: "2020-01-02") {
          edges { node { id } }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as unknown[];
    expect(edges).toHaveLength(0);
    expect((conn.pageInfo as Record<string, unknown>).hasNextPage).toBe(false);
    expect((conn.pageInfo as Record<string, unknown>).endCursor).toBeNull();
  });

  it('returns metadata when present', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Los Angeles"
          metricName: "field_completeness_pct"
          startDate: "2026-03-10T10:00:00Z"
          endDate: "2026-03-10T10:00:00Z"
        ) {
          edges { node { metadata } }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { metadata: string | null } }>;
    // The 10:00 entry has metadata, the 09:00 one does not
    expect(edges.length).toBeGreaterThanOrEqual(1);
    // At least one should have metadata (serialized as JSON string)
    const withMetadata = edges.filter((e) => e.node.metadata !== null);
    expect(withMetadata.length).toBeGreaterThanOrEqual(1);
    // Verify it's a valid JSON string
    const parsed = JSON.parse(withMetadata[0].node.metadata!);
    expect(parsed).toHaveProperty('judge');
    expect(parsed).toHaveProperty('motion_type');
  });
});

// ---------------------------------------------------------------------------
// Tests — dataQualityOverview
// ---------------------------------------------------------------------------

describe('dataQualityOverview', () => {
  it('requires authentication', async () => {
    const body = await gql(`{ dataQualityOverview { county healthStatus } }`);
    expect(body.errors).toBeDefined();
    const ext = (body.errors![0] as Record<string, unknown>).extensions as Record<string, string>;
    expect(ext.code).toBe('UNAUTHENTICATED');
  });

  it('requires admin role', async () => {
    const body = await gql(
      `{ dataQualityOverview { county healthStatus } }`,
      undefined,
      regularToken,
    );
    expect(body.errors).toBeDefined();
    const ext = (body.errors![0] as Record<string, unknown>).extensions as Record<string, string>;
    expect(ext.code).toBe('FORBIDDEN');
  });

  it('returns health overview for all counties', async () => {
    const body = await gql(
      `{ dataQualityOverview { county healthStatus rulingCount24h fieldCompletenessPct scraperLastSuccessAgeHours lastUpdated } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const overview = body.data?.dataQualityOverview as Array<{
      county: string;
      healthStatus: string;
      rulingCount24h: number | null;
      fieldCompletenessPct: number | null;
      scraperLastSuccessAgeHours: number | null;
      lastUpdated: string | null;
    }>;

    // Should have at least our 3 seeded counties
    expect(overview.length).toBeGreaterThanOrEqual(3);

    // Find our test counties
    const la = overview.find((o) => o.county === 'Los Angeles');
    const oc = overview.find((o) => o.county === 'Orange');
    const sf = overview.find((o) => o.county === 'San Francisco');

    expect(la).toBeDefined();
    expect(oc).toBeDefined();
    expect(sf).toBeDefined();

    // LA is healthy: green
    expect(la!.healthStatus).toBe('green');
    expect(la!.rulingCount24h).toBe(42);
    expect(la!.fieldCompletenessPct).toBe(95);
    expect(la!.scraperLastSuccessAgeHours).toBe(1.5);
    expect(la!.lastUpdated).toBeTruthy();

    // Orange is degraded: yellow (completeness 75%, scraper age 8h)
    expect(oc!.healthStatus).toBe('yellow');
    expect(oc!.rulingCount24h).toBe(10);
    expect(oc!.fieldCompletenessPct).toBe(75);
    expect(oc!.scraperLastSuccessAgeHours).toBe(8);

    // SF is critical: red (scraper down 30h, zero rulings, completeness < 70%)
    expect(sf!.healthStatus).toBe('red');
    expect(sf!.rulingCount24h).toBe(0);
    expect(sf!.fieldCompletenessPct).toBe(60);
    expect(sf!.scraperLastSuccessAgeHours).toBe(30);
  });

  it('sorts counties alphabetically', async () => {
    const body = await gql(
      `{ dataQualityOverview { county } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const overview = body.data?.dataQualityOverview as Array<{ county: string }>;
    const counties = overview.map((o) => o.county);
    const sorted = [...counties].sort((a, b) => a.localeCompare(b));
    expect(counties).toEqual(sorted);
  });
});
