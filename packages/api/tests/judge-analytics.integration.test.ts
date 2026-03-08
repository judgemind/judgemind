/**
 * Integration tests for the judgeAnalytics GraphQL query.
 *
 * Tests run against a real PostgreSQL database. Each test run inserts its own
 * seed rows and deletes them in afterAll, so they are safe to run against an
 * existing schema (local dev) or a fresh one (CI postgres service).
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import { applyMigrations } from './setup-db';

// Match type parsers from src/data-access/db.ts
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind',
});

let app: FastifyInstance;
let courtId: string;
let judgeId: string;
let judgeIdNoData: string;
let caseId: string;

async function seedData(): Promise<void> {
  // Court
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', 'San Francisco', 'Superior Court of California, County of San Francisco',
             'ca-sf-analytics-test', 'America/Los_Angeles')
     RETURNING id`,
  );
  courtId = cRows[0].id;

  // Judge with rulings
  const { rows: jRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Analytics, Test Judge', $1, 'Dept. A', true)
     RETURNING id`,
    [courtId],
  );
  judgeId = jRows[0].id;

  // Judge with no rulings
  const { rows: j2Rows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Empty, No Data Judge', $1, 'Dept. B', true)
     RETURNING id`,
    [courtId],
  );
  judgeIdNoData = j2Rows[0].id;

  // Case
  const { rows: csRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, court_id, case_type, case_status)
     VALUES ('ANALYTICS001', $1, 'civil', 'active')
     RETURNING id`,
    [courtId],
  );
  caseId = csRows[0].id;

  // Document (required FK for rulings)
  const { rows: dRows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        captured_at, status)
     VALUES ($1, $2, 'ruling', 'ca/sf/analytics-test/doc.html',
             'judgemind-document-archive-dev', 'html', 'analytics-test-hash',
             NOW(), 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  const docId = dRows[0].id;

  // Seed rulings with various outcomes and motion types:
  //
  // MSJ: 3 granted, 1 denied, 1 granted_in_part = 5 total
  //   grantRate = 3 / (3 + 1 + 1) = 0.6
  // MTD: 1 denied, 1 moot = 2 total
  //   grantRate = 0 / (0 + 1 + 0) = 0
  // Demurrer: 1 granted = 1 total
  //   grantRate = 1 / (1 + 0 + 0) = 1.0
  // No motion_type: 1 granted (should NOT appear in rulingsByMotionType)
  // No outcome: 1 with motion_type 'msj' (should appear in motionType total but not in outcome counts)

  const rulings = [
    // MSJ rulings
    { date: '2025-01-10', outcome: 'granted', motion: 'msj' },
    { date: '2025-02-15', outcome: 'granted', motion: 'msj' },
    { date: '2025-03-20', outcome: 'granted', motion: 'msj' },
    { date: '2025-04-01', outcome: 'denied', motion: 'msj' },
    { date: '2025-05-12', outcome: 'granted_in_part', motion: 'msj' },
    // MTD rulings
    { date: '2025-06-01', outcome: 'denied', motion: 'mtd' },
    { date: '2025-07-15', outcome: 'moot', motion: 'mtd' },
    // Demurrer
    { date: '2025-08-01', outcome: 'granted', motion: 'demurrer' },
    // No motion_type (outcome only)
    { date: '2025-09-01', outcome: 'granted', motion: null },
    // No outcome (motion_type only)
    { date: '2025-10-01', outcome: null, motion: 'msj' },
  ];

  for (const r of rulings) {
    await pool.query(
      `INSERT INTO rulings
         (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
          is_tentative, department)
       VALUES ($1, $2, $3, $4, $5, $6, $7, true, 'Dept. A')`,
      [docId, caseId, judgeId, courtId, r.date, r.outcome, r.motion],
    );
  }
}

async function cleanupData(): Promise<void> {
  if (!courtId) return;
  await pool.query(`DELETE FROM rulings WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM documents WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM cases WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM judges WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM courts WHERE id = $1`, [courtId]);
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

async function gql(query: string, variables?: Record<string, unknown>) {
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json' },
    payload: JSON.stringify({ query, variables }),
  });
  return JSON.parse(res.body) as { data?: Record<string, unknown>; errors?: unknown[] };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('judgeAnalytics query — integration', () => {
  it('returns correct aggregated data for a judge with rulings', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          judgeId
          totalRulings
          rulingsByOutcome { outcome count }
          rulingsByMotionType { motionType total granted denied grantedInPart other grantRate }
          earliestRuling
          latestRuling
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    expect(analytics).not.toBeNull();
    expect(analytics.judgeId).toBe(judgeId);
    expect(analytics.totalRulings).toBe(10);
  });

  it('returns correct rulingsByOutcome counts', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          rulingsByOutcome { outcome count }
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    const outcomes = analytics.rulingsByOutcome as Array<{ outcome: string; count: number }>;

    // We seeded: 5 granted, 2 denied, 1 granted_in_part, 1 moot = 9 with outcome
    const byOutcome = new Map(outcomes.map((o) => [o.outcome, o.count]));
    expect(byOutcome.get('granted')).toBe(5);
    expect(byOutcome.get('denied')).toBe(2);
    expect(byOutcome.get('granted_in_part')).toBe(1);
    expect(byOutcome.get('moot')).toBe(1);
  });

  it('returns correct rulingsByMotionType with grantRate', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          rulingsByMotionType { motionType total granted denied grantedInPart other grantRate }
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    const motionStats = analytics.rulingsByMotionType as Array<{
      motionType: string;
      total: number;
      granted: number;
      denied: number;
      grantedInPart: number;
      other: number;
      grantRate: number;
    }>;

    const byType = new Map(motionStats.map((m) => [m.motionType, m]));

    // MSJ: 6 total (5 with outcome + 1 without), 3 granted, 1 denied, 1 granted_in_part, 0 other
    const msj = byType.get('msj');
    expect(msj).toBeDefined();
    expect(msj!.total).toBe(6);
    expect(msj!.granted).toBe(3);
    expect(msj!.denied).toBe(1);
    expect(msj!.grantedInPart).toBe(1);
    expect(msj!.other).toBe(0);
    // grantRate = 3 / (3 + 1 + 1) = 0.6
    expect(msj!.grantRate).toBeCloseTo(0.6, 5);

    // MTD: 2 total, 0 granted, 1 denied, 0 granted_in_part, 1 other (moot)
    const mtd = byType.get('mtd');
    expect(mtd).toBeDefined();
    expect(mtd!.total).toBe(2);
    expect(mtd!.granted).toBe(0);
    expect(mtd!.denied).toBe(1);
    expect(mtd!.grantedInPart).toBe(0);
    expect(mtd!.other).toBe(1);
    // grantRate = 0 / (0 + 1 + 0) = 0
    expect(mtd!.grantRate).toBeCloseTo(0, 5);

    // Demurrer: 1 total, 1 granted
    const demurrer = byType.get('demurrer');
    expect(demurrer).toBeDefined();
    expect(demurrer!.total).toBe(1);
    expect(demurrer!.granted).toBe(1);
    // grantRate = 1 / (1 + 0 + 0) = 1.0
    expect(demurrer!.grantRate).toBeCloseTo(1.0, 5);
  });

  it('returns correct date range', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          earliestRuling
          latestRuling
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    expect(analytics.earliestRuling).toBe('2025-01-10');
    expect(analytics.latestRuling).toBe('2025-10-01');
  });

  it('returns empty arrays for a judge with no classified rulings', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          judgeId
          totalRulings
          rulingsByOutcome { outcome count }
          rulingsByMotionType { motionType total granted denied grantedInPart other grantRate }
          earliestRuling
          latestRuling
        }
      }`,
      { id: judgeIdNoData },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    expect(analytics).not.toBeNull();
    expect(analytics.judgeId).toBe(judgeIdNoData);
    expect(analytics.totalRulings).toBe(0);
    expect(analytics.rulingsByOutcome).toEqual([]);
    expect(analytics.rulingsByMotionType).toEqual([]);
    expect(analytics.earliestRuling).toBeNull();
    expect(analytics.latestRuling).toBeNull();
  });

  it('returns null for a non-existent judge', async () => {
    const body = await gql(
      `query {
        judgeAnalytics(judgeId: "00000000-0000-0000-0000-000000000000") {
          judgeId
          totalRulings
        }
      }`,
    );
    expect(body.errors).toBeUndefined();
    expect(body.data?.judgeAnalytics).toBeNull();
  });

  it('does not include rulings without motion_type in rulingsByMotionType', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          rulingsByMotionType { motionType }
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    const motionTypes = (analytics.rulingsByMotionType as Array<{ motionType: string }>).map(
      (m) => m.motionType,
    );
    // Should only have msj, mtd, demurrer — not null
    expect(motionTypes).toHaveLength(3);
    expect(motionTypes).toContain('msj');
    expect(motionTypes).toContain('mtd');
    expect(motionTypes).toContain('demurrer');
  });

  it('orders rulingsByMotionType by total descending', async () => {
    const body = await gql(
      `query($id: ID!) {
        judgeAnalytics(judgeId: $id) {
          rulingsByMotionType { motionType total }
        }
      }`,
      { id: judgeId },
    );
    expect(body.errors).toBeUndefined();
    const analytics = body.data?.judgeAnalytics as Record<string, unknown>;
    const motionStats = analytics.rulingsByMotionType as Array<{
      motionType: string;
      total: number;
    }>;
    // msj (6) > mtd (2) > demurrer (1)
    expect(motionStats[0].motionType).toBe('msj');
    expect(motionStats[1].motionType).toBe('mtd');
    expect(motionStats[2].motionType).toBe('demurrer');
  });
});
