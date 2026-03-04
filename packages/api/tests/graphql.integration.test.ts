/**
 * Integration tests for the GraphQL schema — run against a real PostgreSQL
 * database. Each test run inserts its own seed rows and deletes them in
 * afterAll, so the tests are safe to run against an existing schema (local dev)
 * or a fresh one (CI postgres service).
 *
 * The schema is applied idempotently: statements that fail because the object
 * already exists are silently skipped.
 */

import { readFileSync } from 'fs';
import { join } from 'path';
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';

// Match the type parsers registered in src/data-access/db.ts so DATE columns
// come back as 'YYYY-MM-DD' strings rather than millisecond timestamps.
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

// ---------------------------------------------------------------------------
// Shared pool — passed directly into buildApp() so we control the connection.
// ---------------------------------------------------------------------------

const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind',
});

let app: FastifyInstance;
let courtId: string;
let judgeId: string;
let caseId: string;
let rulingId: string;
const insertedPartyIds: string[] = [];
const insertedDocIds: string[] = [];

// ---------------------------------------------------------------------------
// Schema setup — idempotent: ignore "already exists" errors per statement
// ---------------------------------------------------------------------------

async function applySchemaIdempotent(): Promise<void> {
  const sql = readFileSync(join(__dirname, '../src/data-access/schema.sql'), 'utf8');
  try {
    // Run the entire schema as one batch. In CI (fresh DB) this succeeds.
    // Locally the schema is already applied; pg will throw on the first
    // duplicate object — that's fine, we ignore those error codes.
    await pool.query(sql);
  } catch (err: unknown) {
    // 42P07 = duplicate_table, 42710 = duplicate_object, 42P06 = duplicate_schema,
    // 42723 = duplicate_function, 42P16 = invalid_table_definition,
    // 23505 = unique_violation (CREATE EXTENSION IF NOT EXISTS can race across workers)
    const code = (err as { code?: string }).code;
    if (!['42P07', '42710', '42P06', '42723', '42P16', '23505'].includes(code ?? '')) {
      throw err;
    }
  }
}

async function seedData(): Promise<void> {
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', 'Los Angeles', 'Superior Court of California, County of Los Angeles', 'ca-la-test', 'America/Los_Angeles')
     RETURNING id`,
  );
  courtId = cRows[0].id;

  const { rows: jRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Crowfoot, William A.', $1, 'Dept. 3', true)
     RETURNING id`,
    [courtId],
  );
  judgeId = jRows[0].id;

  const { rows: csRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, case_number_normalized, court_id, case_type, case_status, case_title, filed_at)
     VALUES ('24NNCV02551', '24nncv02551', $1, 'civil', 'active', 'Smith v. Jones', '2024-01-15')
     RETURNING id`,
    [courtId],
  );
  caseId = csRows[0].id;

  await pool.query(
    `INSERT INTO case_judges (case_id, judge_id, is_current) VALUES ($1, $2, true)`,
    [caseId, judgeId],
  );

  const { rows: pRows } = await pool.query<{ id: string }>(
    `INSERT INTO parties (canonical_name, party_type) VALUES ('Smith, John', 'individual') RETURNING id`,
  );
  insertedPartyIds.push(pRows[0].id);
  await pool.query(
    `INSERT INTO case_parties (case_id, party_id, role) VALUES ($1, $2, 'plaintiff')`,
    [caseId, pRows[0].id],
  );

  const { rows: dRows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/la/ca-la-test/doc1.html', 'judgemind-document-archive-dev',
             'html', 'abc123def456', 'https://www.lacourt.ca.gov',
             'ca-la-tentatives-civil', NOW(), '2026-03-02', 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  const docId = dRows[0].id;
  insertedDocIds.push(docId);

  // Ruling 1 — granted / msj / 2026-03-02
  const { rows: rRows } = await pool.query<{ id: string }>(
    `INSERT INTO rulings
       (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
        is_tentative, department, ruling_text)
     VALUES ($1, $2, $3, $4, '2026-03-02', 'granted', 'msj', true, 'Dept. 3',
             'TENTATIVE RULING: Motion for Summary Judgment is GRANTED.')
     RETURNING id`,
    [docId, caseId, judgeId, courtId],
  );
  rulingId = rRows[0].id;

  // Ruling 2 — denied / mtd / 2026-03-01 (for filter / pagination tests)
  await pool.query(
    `INSERT INTO rulings
       (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
        is_tentative, department, ruling_text)
     VALUES ($1, $2, $3, $4, '2026-03-01', 'denied', 'mtd', true, 'Dept. 3',
             'TENTATIVE RULING: Motion to Dismiss is DENIED.')`,
    [docId, caseId, judgeId, courtId],
  );
}

async function cleanupData(): Promise<void> {
  if (!courtId) return; // setup never ran
  // Delete in FK order
  await pool.query(`DELETE FROM rulings WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM documents WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM case_parties WHERE case_id = $1`, [caseId]);
  await pool.query(`DELETE FROM case_judges WHERE case_id = $1`, [caseId]);
  await pool.query(`DELETE FROM cases WHERE court_id = $1`, [courtId]);
  for (const id of insertedPartyIds) {
    await pool.query(`DELETE FROM parties WHERE id = $1`, [id]);
  }
  await pool.query(`DELETE FROM judges WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM courts WHERE id = $1`, [courtId]);
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeAll(async () => {
  await applySchemaIdempotent();
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

describe('GraphQL schema — integration', () => {
  describe('case(id)', () => {
    it('returns a case by ID with all scalar fields', async () => {
      const body = await gql(
        `query($id: ID!) { case(id: $id) { id caseNumber caseTitle caseType caseStatus } }`,
        { id: caseId },
      );
      expect(body.errors).toBeUndefined();
      expect(body.data?.case).toMatchObject({
        id: caseId,
        caseNumber: '24NNCV02551',
        caseTitle: 'Smith v. Jones',
        caseType: 'civil',
        caseStatus: 'active',
      });
    });

    it('returns null for unknown ID', async () => {
      const body = await gql(
        `query { case(id: "00000000-0000-0000-0000-000000000000") { id } }`,
      );
      expect(body.errors).toBeUndefined();
      expect(body.data?.case).toBeNull();
    });

    it('resolves nested court via DataLoader', async () => {
      const body = await gql(
        `query($id: ID!) { case(id: $id) { court { courtName county state courtCode } } }`,
        { id: caseId },
      );
      expect(body.errors).toBeUndefined();
      expect((body.data?.case as Record<string, unknown>).court).toMatchObject({
        courtName: 'Superior Court of California, County of Los Angeles',
        county: 'Los Angeles',
        state: 'CA',
        courtCode: 'ca-la-test',
      });
    });

    it('resolves judges list', async () => {
      const body = await gql(
        `query($id: ID!) { case(id: $id) { judges { canonicalName department } } }`,
        { id: caseId },
      );
      expect(body.errors).toBeUndefined();
      const judges = (body.data?.case as Record<string, unknown>).judges as unknown[];
      expect(judges).toHaveLength(1);
      expect(judges[0]).toMatchObject({ canonicalName: 'Crowfoot, William A.', department: 'Dept. 3' });
    });

    it('resolves parties list', async () => {
      const body = await gql(
        `query($id: ID!) { case(id: $id) { parties { canonicalName partyType } } }`,
        { id: caseId },
      );
      expect(body.errors).toBeUndefined();
      const parties = (body.data?.case as Record<string, unknown>).parties as unknown[];
      expect(parties).toHaveLength(1);
      expect(parties[0]).toMatchObject({ canonicalName: 'Smith, John', partyType: 'individual' });
    });
  });

  describe('cases(filters, pagination)', () => {
    it('returns CaseConnection with edges and pageInfo', async () => {
      const body = await gql(`{
        cases { edges { node { id caseNumber } cursor } pageInfo { hasNextPage endCursor } }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.cases as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { id: string }; cursor: string }>;
      expect(Array.isArray(edges)).toBe(true);
      expect(edges.some((e) => e.node.id === caseId)).toBe(true);
      expect(typeof edges[0].cursor).toBe('string');
      expect(conn.pageInfo).toHaveProperty('hasNextPage');
    });

    it('filters by courtId', async () => {
      const body = await gql(
        `query($id: ID!) { cases(courtId: $id) { edges { node { id } } } }`,
        { id: courtId },
      );
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.cases as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === caseId)).toBe(true);
    });

    it('filters by caseStatus', async () => {
      const body = await gql(`{ cases(caseStatus: "active") { edges { node { id caseStatus } } } }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.cases as Record<string, unknown>).edges as Array<{ node: { id: string; caseStatus: string } }>);
      expect(edges.some((e) => e.node.id === caseId)).toBe(true);
      edges.forEach((e) => expect(e.node.caseStatus).toBe('active'));
    });

    it('filters by caseType', async () => {
      const body = await gql(`{ cases(caseType: "civil") { edges { node { id caseType } } } }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.cases as Record<string, unknown>).edges as Array<{ node: { id: string; caseType: string } }>);
      expect(edges.some((e) => e.node.id === caseId)).toBe(true);
      edges.forEach((e) => expect(e.node.caseType).toBe('civil'));
    });

    it('cursor pagination: first=1 returns one edge with a cursor', async () => {
      const body = await gql(`{
        cases(first: 1) {
          edges { node { id } cursor }
          pageInfo { endCursor }
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.cases as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { id: string }; cursor: string }>;
      expect(edges).toHaveLength(1);
      expect(typeof edges[0].cursor).toBe('string');
      expect((conn.pageInfo as Record<string, unknown>).endCursor).toBe(edges[0].cursor);
    });
  });

  describe('judge(id) and judges(filters, pagination)', () => {
    it('returns a judge by ID', async () => {
      const body = await gql(
        `query($id: ID!) { judge(id: $id) { id canonicalName department isActive } }`,
        { id: judgeId },
      );
      expect(body.errors).toBeUndefined();
      expect(body.data?.judge).toMatchObject({
        id: judgeId,
        canonicalName: 'Crowfoot, William A.',
        department: 'Dept. 3',
        isActive: true,
      });
    });

    it('resolves judge court via DataLoader', async () => {
      const body = await gql(
        `query($id: ID!) { judge(id: $id) { court { courtCode } } }`,
        { id: judgeId },
      );
      expect(body.errors).toBeUndefined();
      expect((body.data?.judge as Record<string, unknown>).court).toMatchObject({ courtCode: 'ca-la-test' });
    });

    it('judges returns JudgeConnection', async () => {
      const body = await gql(`{
        judges { edges { node { id canonicalName } cursor } pageInfo { hasNextPage } }
      }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.judges as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === judgeId)).toBe(true);
    });

    it('judges filters by courtId', async () => {
      const body = await gql(
        `query($id: ID!) { judges(courtId: $id) { edges { node { id } } } }`,
        { id: courtId },
      );
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.judges as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === judgeId)).toBe(true);
    });
  });

  describe('ruling(id) and rulings(filters, pagination)', () => {
    it('returns a ruling by ID', async () => {
      const body = await gql(
        `query($id: ID!) { ruling(id: $id) { id hearingDate outcome motionType isTentative department } }`,
        { id: rulingId },
      );
      expect(body.errors).toBeUndefined();
      expect(body.data?.ruling).toMatchObject({
        id: rulingId,
        hearingDate: '2026-03-02',
        outcome: 'granted',
        motionType: 'msj',
        isTentative: true,
        department: 'Dept. 3',
      });
    });

    it('ruling resolves court via DataLoader', async () => {
      const body = await gql(
        `query($id: ID!) { ruling(id: $id) { court { courtCode county } } }`,
        { id: rulingId },
      );
      expect(body.errors).toBeUndefined();
      expect((body.data?.ruling as Record<string, unknown>).court).toMatchObject({
        courtCode: 'ca-la-test',
        county: 'Los Angeles',
      });
    });

    it('ruling resolves judge via DataLoader', async () => {
      const body = await gql(
        `query($id: ID!) { ruling(id: $id) { judge { canonicalName } } }`,
        { id: rulingId },
      );
      expect(body.errors).toBeUndefined();
      expect((body.data?.ruling as Record<string, unknown>).judge).toMatchObject({
        canonicalName: 'Crowfoot, William A.',
      });
    });

    it('rulings returns RulingConnection', async () => {
      const body = await gql(`{
        rulings { edges { node { id outcome } cursor } pageInfo { hasNextPage } }
      }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by judgeId', async () => {
      const body = await gql(
        `query($id: ID!) { rulings(judgeId: $id) { edges { node { id } } } }`,
        { id: judgeId },
      );
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by caseId', async () => {
      const body = await gql(
        `query($id: ID!) { rulings(caseId: $id) { edges { node { id } } } }`,
        { id: caseId },
      );
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by courtId', async () => {
      const body = await gql(
        `query($id: ID!) { rulings(courtId: $id) { edges { node { id } } } }`,
        { id: courtId },
      );
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by county', async () => {
      const body = await gql(`{ rulings(county: "Los Angeles") { edges { node { id } } } }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by outcome', async () => {
      const granted = await gql(`{ rulings(outcome: "granted") { edges { node { id outcome } } } }`);
      expect(granted.errors).toBeUndefined();
      const gEdges = ((granted.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string; outcome: string } }>);
      expect(gEdges.some((e) => e.node.id === rulingId)).toBe(true);
      gEdges.filter((e) => e.node.id === rulingId).forEach((e) => expect(e.node.outcome).toBe('granted'));
    });

    it('filters by dateFrom / dateTo', async () => {
      const body = await gql(`{
        rulings(dateFrom: "2026-03-02", dateTo: "2026-03-02") { edges { node { id hearingDate } } }
      }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string; hearingDate: string } }>);
      expect(edges.length).toBeGreaterThan(0);
      edges.forEach((e) => expect(e.node.hearingDate).toBe('2026-03-02'));
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('filters by caseNumber', async () => {
      const body = await gql(`{ rulings(caseNumber: "24NNCV02551") { edges { node { id } } } }`);
      expect(body.errors).toBeUndefined();
      const edges = ((body.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(edges.some((e) => e.node.id === rulingId)).toBe(true);
    });

    it('cursor pagination: first=1 then after cursor gives next page', async () => {
      // We seeded 2 rulings; first=1 must produce hasNextPage=true
      const page1 = await gql(`{
        rulings(first: 1) {
          edges { node { id } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`);
      expect(page1.errors).toBeUndefined();
      const p1 = page1.data?.rulings as Record<string, unknown>;
      expect((p1.pageInfo as Record<string, unknown>).hasNextPage).toBe(true);

      const cursor = (p1.pageInfo as Record<string, unknown>).endCursor as string;
      const page2 = await gql(
        `query($after: String) { rulings(first: 1, after: $after) { edges { node { id } } } }`,
        { after: cursor },
      );
      expect(page2.errors).toBeUndefined();
      const p1Id = ((p1.edges as Array<{ node: { id: string } }>)[0]).node.id;
      const p2Edges = ((page2.data?.rulings as Record<string, unknown>).edges as Array<{ node: { id: string } }>);
      expect(p2Edges).toHaveLength(1);
      expect(p2Edges[0].node.id).not.toBe(p1Id);
    });
  });
});
