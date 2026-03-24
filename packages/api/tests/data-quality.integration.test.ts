/**
 * Integration tests for data quality metrics GraphQL resolvers.
 *
 * Tests both dataQualityMetrics and dataQualityOverview queries against a
 * real PostgreSQL database. Verifies admin-only access, filtering, pagination,
 * and health status computation.
 *
 * DATA ISOLATION: This file uses the following county names (see test-counties.ts):
 *   - "Los Angeles"  — healthy metrics (green health status)
 *   - "Orange"       — degraded metrics (yellow health status)
 *   - "San Diego"    — unhealthy metrics (red health status)
 *   - "Riverside"    — aggregation tests (4-hour / daily resolution)
 *
 * Do NOT add counties that overlap with other integration test files.
 * See tests/test-counties.ts for the full registry.
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
let adminToken: string;
let userToken: string;
let adminUserId: string;
let regularUserId: string;
const insertedMetricIds: string[] = [];

async function seedData(): Promise<void> {
  // Create admin user
  const { rows: adminRows } = await pool.query<{ id: string }>(
    `INSERT INTO users (email, email_verified, role, password_hash)
     VALUES ('dq-admin@test.com', true, 'admin', 'not-a-real-hash')
     RETURNING id`,
  );
  adminUserId = adminRows[0].id;
  adminToken = signAccessToken({ sub: adminUserId, email: 'dq-admin@test.com', role: 'admin' });

  // Create regular user
  const { rows: userRows } = await pool.query<{ id: string }>(
    `INSERT INTO users (email, email_verified, role, password_hash)
     VALUES ('dq-user@test.com', true, 'user', 'not-a-real-hash')
     RETURNING id`,
  );
  regularUserId = userRows[0].id;
  userToken = signAccessToken({ sub: regularUserId, email: 'dq-user@test.com', role: 'user' });

  // Insert data quality metrics
  const metricsData = [
    // Los Angeles metrics — healthy
    { county: 'Los Angeles', metric_name: 'ruling_count_24h', metric_value: 42, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'Los Angeles', metric_name: 'field_completeness_pct', metric_value: 95, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'Los Angeles', metric_name: 'scraper_last_success_age_hours', metric_value: 2, recorded_at: '2026-03-01T10:00:00Z' },
    // Orange metrics — degraded (yellow)
    { county: 'Orange', metric_name: 'ruling_count_24h', metric_value: 5, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'Orange', metric_name: 'field_completeness_pct', metric_value: 80, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'Orange', metric_name: 'scraper_last_success_age_hours', metric_value: 3, recorded_at: '2026-03-01T10:00:00Z' },
    // San Diego metrics — unhealthy (red: scraper down)
    { county: 'San Diego', metric_name: 'ruling_count_24h', metric_value: 0, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'San Diego', metric_name: 'scraper_last_success_age_hours', metric_value: 48, recorded_at: '2026-03-01T10:00:00Z' },
    // Older LA metric (for time range filtering test)
    { county: 'Los Angeles', metric_name: 'ruling_count_24h', metric_value: 30, recorded_at: '2026-02-01T10:00:00Z' },
    // --- Aggregation test data: uses "Riverside" county to avoid polluting existing tests ---
    // Same 4-hour bucket (08:00-11:59): two hourly records
    { county: 'Riverside', metric_name: 'ruling_count_24h', metric_value: 42, recorded_at: '2026-03-01T10:00:00Z' },
    { county: 'Riverside', metric_name: 'ruling_count_24h', metric_value: 38, recorded_at: '2026-03-01T11:00:00Z' },
    // Different 4-hour bucket (12:00-15:59)
    { county: 'Riverside', metric_name: 'ruling_count_24h', metric_value: 50, recorded_at: '2026-03-01T13:00:00Z' },
    // Second day for daily aggregation tests
    { county: 'Riverside', metric_name: 'ruling_count_24h', metric_value: 60, recorded_at: '2026-03-02T10:00:00Z' },
    { county: 'Riverside', metric_name: 'ruling_count_24h', metric_value: 40, recorded_at: '2026-03-02T14:00:00Z' },
  ];

  for (const m of metricsData) {
    const { rows } = await pool.query<{ id: string }>(
      `INSERT INTO data_quality_metrics (county, metric_name, metric_value, recorded_at)
       VALUES ($1, $2, $3, $4::timestamptz)
       RETURNING id`,
      [m.county, m.metric_name, m.metric_value, m.recorded_at],
    );
    insertedMetricIds.push(rows[0].id);
  }
}

async function cleanupData(): Promise<void> {
  for (const id of insertedMetricIds) {
    await pool.query(`DELETE FROM data_quality_metrics WHERE id = $1`, [id]);
  }
  if (adminUserId) {
    await pool.query(`DELETE FROM users WHERE id = $1`, [adminUserId]);
  }
  if (regularUserId) {
    await pool.query(`DELETE FROM users WHERE id = $1`, [regularUserId]);
  }
}

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
// Helpers
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
  return JSON.parse(res.body) as { data?: Record<string, unknown>; errors?: Array<{ message: string; extensions?: { code: string } }> };
}

// ---------------------------------------------------------------------------
// Tests — dataQualityMetrics
// ---------------------------------------------------------------------------

describe('dataQualityMetrics', () => {
  it('rejects unauthenticated requests', async () => {
    const body = await gql(`{
      dataQualityMetrics(startDate: "2026-01-01T00:00:00Z", endDate: "2026-12-31T23:59:59Z") {
        edges { node { id } }
        pageInfo { hasNextPage }
      }
    }`);
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
  });

  it('rejects non-admin users', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(startDate: "2026-01-01T00:00:00Z", endDate: "2026-12-31T23:59:59Z") {
          edges { node { id } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      userToken,
    );
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('FORBIDDEN');
  });

  it('returns metrics for admin users', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(startDate: "2026-01-01T00:00:00Z", endDate: "2026-12-31T23:59:59Z") {
          edges { node { id county metricName metricValue recordedAt } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: Record<string, unknown>; cursor: string }>;
    expect(edges.length).toBeGreaterThan(0);
    // Check that results have the expected shape
    const firstNode = edges[0].node;
    expect(firstNode).toHaveProperty('id');
    expect(firstNode).toHaveProperty('county');
    expect(firstNode).toHaveProperty('metricName');
    expect(firstNode).toHaveProperty('metricValue');
    expect(firstNode).toHaveProperty('recordedAt');
    expect(typeof edges[0].cursor).toBe('string');
  });

  it('filters by county', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Los Angeles"
          startDate: "2026-01-01T00:00:00Z"
          endDate: "2026-12-31T23:59:59Z"
        ) {
          edges { node { county } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { county: string } }>;
    expect(edges.length).toBeGreaterThan(0);
    edges.forEach((e) => expect(e.node.county).toBe('Los Angeles'));
  });

  it('filters by metricName', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          metricName: "ruling_count_24h"
          startDate: "2026-01-01T00:00:00Z"
          endDate: "2026-12-31T23:59:59Z"
        ) {
          edges { node { metricName } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { metricName: string } }>;
    expect(edges.length).toBeGreaterThan(0);
    edges.forEach((e) => expect(e.node.metricName).toBe('ruling_count_24h'));
  });

  it('filters by time range', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Los Angeles"
          metricName: "ruling_count_24h"
          startDate: "2026-02-15T00:00:00Z"
          endDate: "2026-12-31T23:59:59Z"
        ) {
          edges { node { metricValue } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { metricValue: number } }>;
    // Should only get the March metric (42), not the February one (30)
    expect(edges).toHaveLength(1);
    expect(edges[0].node.metricValue).toBe(42);
  });

  it('supports cursor pagination', async () => {
    const page1 = await gql(
      `{
        dataQualityMetrics(
          startDate: "2026-01-01T00:00:00Z"
          endDate: "2026-12-31T23:59:59Z"
          first: 2
        ) {
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
    expect(p1Edges).toHaveLength(2);
    expect((p1.pageInfo as Record<string, unknown>).hasNextPage).toBe(true);

    const cursor = (p1.pageInfo as Record<string, unknown>).endCursor as string;
    const page2 = await gql(
      `query($after: String) {
        dataQualityMetrics(
          startDate: "2026-01-01T00:00:00Z"
          endDate: "2026-12-31T23:59:59Z"
          first: 2
          after: $after
        ) {
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
    // Page 2 should have different IDs than page 1
    const p1Ids = p1Edges.map((e) => e.node.id);
    p2Edges.forEach((e) => expect(p1Ids).not.toContain(e.node.id));
  });

  it('aggregates with four_hour resolution', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Riverside"
          metricName: "ruling_count_24h"
          startDate: "2026-03-01T00:00:00Z"
          endDate: "2026-03-01T23:59:59Z"
          resolution: four_hour
        ) {
          edges { node { id recordedAt county metricName metricValue } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { id: string; recordedAt: string; metricValue: number; county: string; metricName: string }; cursor: string }>;
    // March 1 has three Riverside ruling_count_24h entries:
    //   10:00 (42), 11:00 (38) → same 08:00 bucket → avg = 40
    //   13:00 (50) → 12:00 bucket → avg = 50
    expect(edges.length).toBe(2);
    // Ordered by bucket DESC — 12:00 bucket first, then 08:00 bucket
    expect(edges[0].node.metricValue).toBe(50);
    expect(edges[1].node.metricValue).toBe(40);
    // Both should be Riverside ruling_count_24h
    edges.forEach((e) => {
      expect(e.node.county).toBe('Riverside');
      expect(e.node.metricName).toBe('ruling_count_24h');
    });
    // Cursors should be present
    expect(edges[0].cursor).toBeTruthy();
    expect(edges[1].cursor).toBeTruthy();
    // Synthetic IDs should be present and distinct
    expect(edges[0].node.id).toBeTruthy();
    expect(edges[0].node.id).not.toBe(edges[1].node.id);
  });

  it('aggregates with daily resolution', async () => {
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Riverside"
          metricName: "ruling_count_24h"
          startDate: "2026-03-01T00:00:00Z"
          endDate: "2026-03-02T23:59:59Z"
          resolution: daily
        ) {
          edges { node { recordedAt metricValue } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { recordedAt: string; metricValue: number } }>;
    // Two days of data:
    //   March 1: 42 + 38 + 50 = avg 43.333...
    //   March 2: 60 + 40 = avg 50
    expect(edges.length).toBe(2);
    // Ordered by bucket DESC — March 2 first
    expect(edges[0].node.metricValue).toBe(50);
    // March 1 average: (42 + 38 + 50) / 3 ≈ 43.33
    expect(edges[1].node.metricValue).toBeCloseTo(43.33, 1);
  });

  it('defaults to hourly resolution when not specified', async () => {
    // Without resolution parameter, should return raw rows (same as before)
    const body = await gql(
      `{
        dataQualityMetrics(
          county: "Riverside"
          metricName: "ruling_count_24h"
          startDate: "2026-03-01T00:00:00Z"
          endDate: "2026-03-01T23:59:59Z"
        ) {
          edges { node { metricValue } }
          pageInfo { hasNextPage }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const conn = body.data?.dataQualityMetrics as Record<string, unknown>;
    const edges = conn.edges as Array<{ node: { metricValue: number } }>;
    // Three individual hourly rows for March 1
    expect(edges.length).toBe(3);
    // Raw values — not averaged
    const values = edges.map((e) => e.node.metricValue).sort();
    expect(values).toEqual([38, 42, 50]);
  });

  it('supports cursor pagination with aggregated resolution', async () => {
    const page1 = await gql(
      `{
        dataQualityMetrics(
          county: "Riverside"
          metricName: "ruling_count_24h"
          startDate: "2026-03-01T00:00:00Z"
          endDate: "2026-03-02T23:59:59Z"
          resolution: daily
          first: 1
        ) {
          edges { node { recordedAt metricValue } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`,
      undefined,
      adminToken,
    );
    expect(page1.errors).toBeUndefined();
    const p1 = page1.data?.dataQualityMetrics as Record<string, unknown>;
    const p1Edges = p1.edges as Array<{ node: { recordedAt: string; metricValue: number }; cursor: string }>;
    expect(p1Edges).toHaveLength(1);
    expect((p1.pageInfo as Record<string, unknown>).hasNextPage).toBe(true);

    // Fetch page 2 using cursor
    const cursor = (p1.pageInfo as Record<string, unknown>).endCursor as string;
    const page2 = await gql(
      `query($after: String) {
        dataQualityMetrics(
          county: "Riverside"
          metricName: "ruling_count_24h"
          startDate: "2026-03-01T00:00:00Z"
          endDate: "2026-03-02T23:59:59Z"
          resolution: daily
          first: 1
          after: $after
        ) {
          edges { node { recordedAt metricValue } }
          pageInfo { hasNextPage }
        }
      }`,
      { after: cursor },
      adminToken,
    );
    expect(page2.errors).toBeUndefined();
    const p2 = page2.data?.dataQualityMetrics as Record<string, unknown>;
    const p2Edges = p2.edges as Array<{ node: { recordedAt: string; metricValue: number } }>;
    expect(p2Edges).toHaveLength(1);
    // Page 1 should be March 2 (50), page 2 should be March 1 (~43.33)
    expect(p1Edges[0].node.metricValue).toBe(50);
    expect(p2Edges[0].node.metricValue).toBeCloseTo(43.33, 1);
  });
});

// ---------------------------------------------------------------------------
// Tests — dataQualityOverview
// ---------------------------------------------------------------------------

describe('dataQualityOverview', () => {
  it('rejects unauthenticated requests', async () => {
    const body = await gql(`{ dataQualityOverview { county healthStatus } }`);
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
  });

  it('rejects non-admin users', async () => {
    const body = await gql(
      `{ dataQualityOverview { county healthStatus } }`,
      undefined,
      userToken,
    );
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('FORBIDDEN');
  });

  it('returns per-county overview for admin', async () => {
    const body = await gql(
      `{ dataQualityOverview {
        county healthStatus rulingCount24h fieldCompletenessPct
        scraperLastSuccessAgeHours lastUpdated
      } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const overview = body.data?.dataQualityOverview as Array<Record<string, unknown>>;
    expect(overview.length).toBeGreaterThanOrEqual(3);

    // Find our seeded counties
    const la = overview.find((o) => o.county === 'Los Angeles');
    const oc = overview.find((o) => o.county === 'Orange');
    const sd = overview.find((o) => o.county === 'San Diego');

    expect(la).toBeDefined();
    expect(la!.healthStatus).toBe('green');
    expect(la!.rulingCount24h).toBe(42);
    expect(la!.fieldCompletenessPct).toBe(95);
    expect(la!.scraperLastSuccessAgeHours).toBe(2);
    expect(la!.lastUpdated).toBeDefined();

    expect(oc).toBeDefined();
    expect(oc!.healthStatus).toBe('yellow');

    expect(sd).toBeDefined();
    expect(sd!.healthStatus).toBe('red');
  });
});
