/**
 * Integration tests for the searchRulings GraphQL query.
 *
 * Requires both PostgreSQL and OpenSearch to be running locally.
 * OpenSearch must be accessible at http://localhost:9200 (or OPENSEARCH_URL).
 * PostgreSQL must be accessible at the standard DATABASE_URL.
 *
 * The tests seed data into both PG (court, judge, case, ruling) and OpenSearch
 * (tentative_rulings index), then exercise the GraphQL query with various
 * combinations of full-text queries, metadata filters, and pagination.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import { Client } from '@opensearch-project/opensearch';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import { applyMigrations } from './setup-db';

// Match date type parsers from src/data-access/db.ts
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

// ---------------------------------------------------------------------------
// Shared connections
// ---------------------------------------------------------------------------

const pool = new Pool({
  connectionString:
    process.env.DATABASE_URL ?? 'postgresql://judgemind:localdev@localhost:5432/judgemind',
});

const osClient = new Client({
  node: process.env.OPENSEARCH_URL ?? 'http://localhost:9200',
  ssl: { rejectUnauthorized: false },
});

const TEST_INDEX = 'tentative_rulings_test';

let app: FastifyInstance;
let courtId: string;
let judgeId: string;
let caseId: string;
let rulingId1: string;
let rulingId2: string;
let docId1: string;
let docId2: string;

// ---------------------------------------------------------------------------
// Schema + seed data
// ---------------------------------------------------------------------------

async function seedPgData(): Promise<void> {
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', 'Los Angeles', 'Superior Court of California, County of Los Angeles', 'ca-la-search-test', 'America/Los_Angeles')
     RETURNING id`,
  );
  courtId = cRows[0].id;

  const { rows: jRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Johnson, Robert M.', $1, 'Dept. 5', true)
     RETURNING id`,
    [courtId],
  );
  judgeId = jRows[0].id;

  const { rows: csRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, case_number_normalized, court_id, case_type, case_status, case_title, filed_at)
     VALUES ('23STCV01234', '23stcv01234', $1, 'civil', 'active', 'Doe v. Roe', '2023-06-15')
     RETURNING id`,
    [courtId],
  );
  caseId = csRows[0].id;

  // Document 1
  const { rows: d1 } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/la/search-test/doc1.html', 'judgemind-document-archive-dev',
             'html', 'searchtest-hash-1', 'https://example.com',
             'ca-la-tentatives-civil', NOW(), '2026-04-10', 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  docId1 = d1[0].id;

  // Document 2
  const { rows: d2 } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/la/search-test/doc2.html', 'judgemind-document-archive-dev',
             'html', 'searchtest-hash-2', 'https://example.com',
             'ca-la-tentatives-civil', NOW(), '2026-04-11', 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  docId2 = d2[0].id;

  // Ruling 1 — motion for summary judgment, granted
  const { rows: r1 } = await pool.query<{ id: string }>(
    `INSERT INTO rulings
       (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
        is_tentative, department, ruling_text)
     VALUES ($1, $2, $3, $4, '2026-04-10', 'granted', 'msj', true, 'Dept. 5',
             'TENTATIVE RULING: Defendant motion for summary judgment is GRANTED. The court finds no triable issue of material fact.')
     RETURNING id`,
    [docId1, caseId, judgeId, courtId],
  );
  rulingId1 = r1[0].id;

  // Ruling 2 — demurrer, overruled
  const { rows: r2 } = await pool.query<{ id: string }>(
    `INSERT INTO rulings
       (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
        is_tentative, department, ruling_text)
     VALUES ($1, $2, $3, $4, '2026-04-11', 'denied', 'demurrer', true, 'Dept. 5',
             'TENTATIVE RULING: Demurrer to the complaint is OVERRULED. Plaintiff has sufficiently alleged fraud with specificity.')
     RETURNING id`,
    [docId2, caseId, judgeId, courtId],
  );
  rulingId2 = r2[0].id;
}

async function seedOpenSearch(): Promise<void> {
  // Create test index with the same mapping as production
  const indexBody = {
    settings: {
      number_of_shards: 1,
      number_of_replicas: 0,
      analysis: {
        analyzer: {
          ruling_text_analyzer: {
            type: 'custom',
            tokenizer: 'standard',
            filter: ['lowercase', 'stop', 'snowball'],
          },
        },
      },
    },
    mappings: {
      properties: {
        case_number: { type: 'keyword' },
        court: { type: 'keyword' },
        county: { type: 'keyword' },
        state: { type: 'keyword' },
        judge_name: { type: 'keyword' },
        hearing_date: { type: 'date' },
        ruling_text: { type: 'text', analyzer: 'ruling_text_analyzer' },
        document_id: { type: 'keyword' },
        s3_key: { type: 'keyword', index: false },
        content_hash: { type: 'keyword', index: false },
        indexed_at: { type: 'date' },
      },
    },
    aliases: { tentative_rulings: {} },
  };

  // Delete test index if it exists
  try {
    await osClient.indices.delete({ index: TEST_INDEX });
  } catch {
    // Index doesn't exist — fine
  }

  await osClient.indices.create({ index: TEST_INDEX, body: indexBody });

  // Index test documents
  const docs = [
    {
      _id: docId1,
      case_number: '23STCV01234',
      court: 'Superior Court of California, County of Los Angeles',
      county: 'Los Angeles',
      state: 'CA',
      judge_name: 'Johnson, Robert M.',
      hearing_date: '2026-04-10',
      ruling_text:
        'TENTATIVE RULING: Defendant motion for summary judgment is GRANTED. The court finds no triable issue of material fact.',
      document_id: docId1,
      s3_key: 'ca/la/search-test/doc1.html',
      content_hash: 'searchtest-hash-1',
      indexed_at: new Date().toISOString(),
    },
    {
      _id: docId2,
      case_number: '23STCV01234',
      court: 'Superior Court of California, County of Los Angeles',
      county: 'Los Angeles',
      state: 'CA',
      judge_name: 'Johnson, Robert M.',
      hearing_date: '2026-04-11',
      ruling_text:
        'TENTATIVE RULING: Demurrer to the complaint is OVERRULED. Plaintiff has sufficiently alleged fraud with specificity.',
      document_id: docId2,
      s3_key: 'ca/la/search-test/doc2.html',
      content_hash: 'searchtest-hash-2',
      indexed_at: new Date().toISOString(),
    },
  ];

  for (const doc of docs) {
    const { _id, ...body } = doc;
    await osClient.index({ index: TEST_INDEX, id: _id, body, refresh: 'wait_for' });
  }
}

async function cleanupPgData(): Promise<void> {
  if (!courtId) return;
  await pool.query('DELETE FROM rulings WHERE court_id = $1', [courtId]);
  await pool.query('DELETE FROM documents WHERE court_id = $1', [courtId]);
  await pool.query('DELETE FROM case_judges WHERE case_id = $1', [caseId]);
  await pool.query('DELETE FROM cases WHERE court_id = $1', [courtId]);
  await pool.query('DELETE FROM judges WHERE court_id = $1', [courtId]);
  await pool.query('DELETE FROM courts WHERE id = $1', [courtId]);
}

async function cleanupOpenSearch(): Promise<void> {
  try {
    await osClient.indices.delete({ index: TEST_INDEX });
  } catch {
    // Index doesn't exist — fine
  }
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeAll(async () => {
  applyMigrations();
  await seedPgData();
  await seedOpenSearch();
  app = await buildApp(pool, osClient);
}, 60_000);

afterAll(async () => {
  await app?.close();
  await cleanupPgData();
  await cleanupOpenSearch();
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

describe('searchRulings — integration', () => {
  describe('full-text search', () => {
    it('returns results for a matching full-text query', async () => {
      const body = await gql(`{
        searchRulings(query: "summary judgment") {
          edges { node { rulingId caseNumber court excerpt score } cursor }
          pageInfo { hasNextPage endCursor }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{
        node: { rulingId: string; caseNumber: string; excerpt: string; score: number };
      }>;
      expect(edges.length).toBeGreaterThan(0);
      // Should match ruling 1 (summary judgment)
      expect(edges.some((e) => e.node.rulingId === rulingId1)).toBe(true);
      // Should have highlighted excerpt
      const hit = edges.find((e) => e.node.rulingId === rulingId1);
      expect(hit?.node.excerpt).toBeDefined();
      expect(hit?.node.score).toBeGreaterThan(0);
      expect(conn.totalHits).toBeGreaterThan(0);
    });

    it('returns results for a query matching demurrer', async () => {
      const body = await gql(`{
        searchRulings(query: "demurrer fraud") {
          edges { node { rulingId } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { rulingId: string } }>;
      expect(edges.some((e) => e.node.rulingId === rulingId2)).toBe(true);
    });

    it('returns no results for a non-matching query', async () => {
      const body = await gql(`{
        searchRulings(query: "xyznonexistent12345") {
          edges { node { rulingId } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      expect(conn.totalHits).toBe(0);
      expect(conn.edges).toHaveLength(0);
    });
  });

  describe('filter-only queries', () => {
    it('returns results filtered by county', async () => {
      const body = await gql(`{
        searchRulings(filters: { county: "Los Angeles" }) {
          edges { node { rulingId county hearingDate } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{
        node: { rulingId: string; county: string; hearingDate: string };
      }>;
      expect(edges.length).toBe(2);
      // Filter-only: sorted by hearing_date DESC
      expect(edges[0].node.hearingDate).toBe('2026-04-11');
      expect(edges[1].node.hearingDate).toBe('2026-04-10');
    });

    it('returns results filtered by state', async () => {
      const body = await gql(`{
        searchRulings(filters: { state: "CA" }) {
          edges { node { rulingId state } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      expect((conn.totalHits as number)).toBeGreaterThanOrEqual(2);
    });

    it('returns results filtered by judge name', async () => {
      const body = await gql(`{
        searchRulings(filters: { judgeName: "Johnson, Robert M." }) {
          edges { node { rulingId judgeName } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { judgeName: string } }>;
      expect(edges.length).toBeGreaterThanOrEqual(2);
      edges.forEach((e) => expect(e.node.judgeName).toBe('Johnson, Robert M.'));
    });

    it('returns results filtered by date range', async () => {
      const body = await gql(`{
        searchRulings(filters: { dateFrom: "2026-04-11", dateTo: "2026-04-11" }) {
          edges { node { rulingId hearingDate } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { rulingId: string; hearingDate: string } }>;
      expect(edges.length).toBe(1);
      expect(edges[0].node.rulingId).toBe(rulingId2);
      expect(edges[0].node.hearingDate).toBe('2026-04-11');
    });

    it('returns results filtered by case number prefix', async () => {
      const body = await gql(`{
        searchRulings(filters: { caseNumber: "23STCV" }) {
          edges { node { rulingId caseNumber } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { caseNumber: string } }>;
      expect(edges.length).toBe(2);
      edges.forEach((e) => expect(e.node.caseNumber).toMatch(/^23STCV/));
    });

    it('returns empty for a non-matching filter', async () => {
      const body = await gql(`{
        searchRulings(filters: { county: "Nonexistent County" }) {
          edges { node { rulingId } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      expect(conn.totalHits).toBe(0);
    });
  });

  describe('combined query + filters', () => {
    it('combines full-text query with county filter', async () => {
      const body = await gql(`{
        searchRulings(query: "summary judgment", filters: { county: "Los Angeles" }) {
          edges { node { rulingId county } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { rulingId: string } }>;
      expect(edges.some((e) => e.node.rulingId === rulingId1)).toBe(true);
    });
  });

  describe('pagination', () => {
    it('respects first parameter', async () => {
      const body = await gql(`{
        searchRulings(filters: { county: "Los Angeles" }, first: 1) {
          edges { node { rulingId } cursor }
          pageInfo { hasNextPage endCursor }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{ node: { rulingId: string }; cursor: string }>;
      expect(edges).toHaveLength(1);
      const pageInfo = conn.pageInfo as { hasNextPage: boolean; endCursor: string };
      expect(pageInfo.hasNextPage).toBe(true);
      expect(pageInfo.endCursor).toBeDefined();
    });

    it('cursor-based pagination: first page then next page', async () => {
      // Page 1
      const page1 = await gql(`{
        searchRulings(filters: { county: "Los Angeles" }, first: 1) {
          edges { node { rulingId } cursor }
          pageInfo { hasNextPage endCursor }
        }
      }`);
      expect(page1.errors).toBeUndefined();
      const p1 = page1.data?.searchRulings as Record<string, unknown>;
      const p1Edges = p1.edges as Array<{ node: { rulingId: string } }>;
      const endCursor = (p1.pageInfo as { endCursor: string }).endCursor;

      // Page 2
      const page2 = await gql(
        `query($after: String) {
          searchRulings(filters: { county: "Los Angeles" }, first: 1, after: $after) {
            edges { node { rulingId } }
            pageInfo { hasNextPage }
          }
        }`,
        { after: endCursor },
      );
      expect(page2.errors).toBeUndefined();
      const p2 = page2.data?.searchRulings as Record<string, unknown>;
      const p2Edges = p2.edges as Array<{ node: { rulingId: string } }>;
      expect(p2Edges).toHaveLength(1);
      // Ensure no overlap between pages
      expect(p2Edges[0].node.rulingId).not.toBe(p1Edges[0].node.rulingId);
    });
  });

  describe('result structure', () => {
    it('returns all expected fields in RulingSearchHit', async () => {
      const body = await gql(`{
        searchRulings(filters: { county: "Los Angeles" }) {
          edges {
            node {
              rulingId caseNumber court county state
              judgeName hearingDate excerpt score
            }
            cursor
          }
          pageInfo { hasNextPage endCursor }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      const edges = conn.edges as Array<{
        node: Record<string, unknown>;
        cursor: string;
      }>;
      expect(edges.length).toBeGreaterThan(0);

      const node = edges[0].node;
      expect(node.rulingId).toBeDefined();
      expect(node.caseNumber).toBe('23STCV01234');
      expect(node.county).toBe('Los Angeles');
      expect(node.state).toBe('CA');
      expect(node.judgeName).toBe('Johnson, Robert M.');
      expect(node.hearingDate).toBeDefined();
      expect(typeof edges[0].cursor).toBe('string');
      expect(typeof (conn.pageInfo as Record<string, unknown>).hasNextPage).toBe('boolean');
      expect(typeof conn.totalHits).toBe('number');
    });

    it('matches all results when no query or filters provided', async () => {
      const body = await gql(`{
        searchRulings {
          edges { node { rulingId } }
          totalHits
        }
      }`);
      expect(body.errors).toBeUndefined();
      const conn = body.data?.searchRulings as Record<string, unknown>;
      // Should return at least our 2 seeded documents
      expect((conn.totalHits as number)).toBeGreaterThanOrEqual(2);
    });
  });
});
