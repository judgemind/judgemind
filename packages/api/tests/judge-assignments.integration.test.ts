/**
 * Integration tests for judge department assignment history.
 *
 * Tests run against a real PostgreSQL database. Each test run inserts its own
 * seed rows and deletes them in afterAll, so they are safe to run against an
 * existing schema (local dev) or a fresh one (CI postgres service).
 *
 * DATA ISOLATION: This file uses county "Ventura" with court_code "ca-ventura-assignments-test".
 * Do NOT reuse this court_code in other integration test files.
 * See tests/test-counties.ts for the full registry.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool, types } from 'pg';
import { applyMigrations } from './setup-db';
import { TEST_COUNTY_REGISTRY } from './test-counties';
import { getJudgeAssignments, getJudgeAssignmentsBatch } from '../src/graphql/judge-assignments';

// Extract county/court_code from registry for compile-time enforcement
const [JA_COUNTY] = TEST_COUNTY_REGISTRY.judgeAssignments.counties;
const [JA_COURT_CODE] = TEST_COUNTY_REGISTRY.judgeAssignments.courtCodes;

// Match type parsers from src/data-access/db.ts
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

const pool = new Pool({
  connectionString:
    process.env.TEST_DATABASE_URL ??
    'postgresql://judgemind:localdev@localhost:5432/judgemind_test',
});

let courtId: string;
// Two judges: judgeA has data in two depts, judgeB has no non-null case_type
let judgeAId: string;
let judgeBId: string;
// Cases: different case_types seeded for majority-wins test
let caseCivilId: string;
let caseFamilyId: string;
let caseNullTypeId: string;

async function seedData(): Promise<void> {
  // Court
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', $1, 'Superior Court of California, County of Ventura',
             $2, 'America/Los_Angeles')
     RETURNING id`,
    [JA_COUNTY, JA_COURT_CODE],
  );
  courtId = cRows[0].id;

  // Judge A — has rulings in two departments, with mixed case_types
  const { rows: jARows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Assignment, Judge Alpha', $1, 'D1', true)
     RETURNING id`,
    [courtId],
  );
  judgeAId = jARows[0].id;

  // Judge B — has rulings but all cases have NULL case_type
  const { rows: jBRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Assignment, Judge Beta', $1, 'D3', true)
     RETURNING id`,
    [courtId],
  );
  judgeBId = jBRows[0].id;

  // Cases
  const { rows: csCivilRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, court_id, case_type, case_status)
     VALUES ('ASSIGN001', $1, 'civil', 'active')
     RETURNING id`,
    [courtId],
  );
  caseCivilId = csCivilRows[0].id;

  const { rows: csFamilyRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, court_id, case_type, case_status)
     VALUES ('ASSIGN002', $1, 'family', 'active')
     RETURNING id`,
    [courtId],
  );
  caseFamilyId = csFamilyRows[0].id;

  const { rows: csNullRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, court_id, case_type, case_status)
     VALUES ('ASSIGN003', $1, NULL, 'active')
     RETURNING id`,
    [courtId],
  );
  caseNullTypeId = csNullRows[0].id;

  // --- Judge A, Department D1 ---
  // 3 civil rulings (majority), 1 family ruling => case_type = 'civil'
  // Date range: 2024-01-01 to 2025-06-01
  const deptD1Rulings = [
    { date: '2024-01-01', caseId: caseCivilId, hash: 'assign-d1-c1' },
    { date: '2024-06-01', caseId: caseCivilId, hash: 'assign-d1-c2' },
    { date: '2025-01-01', caseId: caseCivilId, hash: 'assign-d1-c3' },
    { date: '2025-06-01', caseId: caseFamilyId, hash: 'assign-d1-f1' },
  ];

  for (let i = 0; i < deptD1Rulings.length; i++) {
    const r = deptD1Rulings[i];
    const { rows: docRows } = await pool.query<{ id: string }>(
      `INSERT INTO documents
         (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
          captured_at, status)
       VALUES ($1, $2, 'ruling', $3, 'judgemind-document-archive-dev', 'html', $4,
               NOW(), 'active')
       RETURNING id`,
      [r.caseId, courtId, `ca/ventura/assignments-test/d1-${i}.html`, r.hash],
    );
    await pool.query(
      `INSERT INTO rulings
         (document_id, case_id, judge_id, court_id, hearing_date, outcome,
          is_tentative, department)
       VALUES ($1, $2, $3, $4, $5, 'granted', true, 'D1')`,
      [docRows[0].id, r.caseId, judgeAId, courtId, r.date],
    );
  }

  // --- Judge A, Department D2 ---
  // 2 family rulings (majority), 1 civil ruling => case_type = 'family'
  // Date range: 2023-03-01 to 2023-12-01 (older, should appear after D1)
  const deptD2Rulings = [
    { date: '2023-03-01', caseId: caseFamilyId, hash: 'assign-d2-f1' },
    { date: '2023-07-01', caseId: caseFamilyId, hash: 'assign-d2-f2' },
    { date: '2023-12-01', caseId: caseCivilId, hash: 'assign-d2-c1' },
  ];

  for (let i = 0; i < deptD2Rulings.length; i++) {
    const r = deptD2Rulings[i];
    const { rows: docRows } = await pool.query<{ id: string }>(
      `INSERT INTO documents
         (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
          captured_at, status)
       VALUES ($1, $2, 'ruling', $3, 'judgemind-document-archive-dev', 'html', $4,
               NOW(), 'active')
       RETURNING id`,
      [r.caseId, courtId, `ca/ventura/assignments-test/d2-${i}.html`, r.hash],
    );
    await pool.query(
      `INSERT INTO rulings
         (document_id, case_id, judge_id, court_id, hearing_date, outcome,
          is_tentative, department)
       VALUES ($1, $2, $3, $4, $5, 'denied', true, 'D2')`,
      [docRows[0].id, r.caseId, judgeAId, courtId, r.date],
    );
  }

  // --- Judge B, Department D3 ---
  // All rulings have cases with NULL case_type => case_type = null
  const deptD3Rulings = [
    { date: '2025-02-01', caseId: caseNullTypeId, hash: 'assign-d3-n1' },
    { date: '2025-04-01', caseId: caseNullTypeId, hash: 'assign-d3-n2' },
  ];

  for (let i = 0; i < deptD3Rulings.length; i++) {
    const r = deptD3Rulings[i];
    const { rows: docRows } = await pool.query<{ id: string }>(
      `INSERT INTO documents
         (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
          captured_at, status)
       VALUES ($1, $2, 'ruling', $3, 'judgemind-document-archive-dev', 'html', $4,
               NOW(), 'active')
       RETURNING id`,
      [r.caseId, courtId, `ca/ventura/assignments-test/d3-${i}.html`, r.hash],
    );
    await pool.query(
      `INSERT INTO rulings
         (document_id, case_id, judge_id, court_id, hearing_date, outcome,
          is_tentative, department)
       VALUES ($1, $2, $3, $4, $5, 'granted', true, 'D3')`,
      [docRows[0].id, r.caseId, judgeBId, courtId, r.date],
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
}, 30_000);

afterAll(async () => {
  await cleanupData();
  await pool.end();
}, 15_000);

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('getJudgeAssignments — integration', () => {
  it('returns most-frequent case_type per department', async () => {
    const result = await getJudgeAssignments(pool, judgeAId, null);
    expect(result).toHaveLength(2);

    // D1: last_seen = 2025-06-01 (most recent, appears first)
    const d1 = result.find((a) => a.department === 'D1');
    expect(d1).toBeDefined();
    expect(d1!.caseType).toBe('civil'); // 3 civil vs 1 family

    // D2: last_seen = 2023-12-01 (older, appears second)
    const d2 = result.find((a) => a.department === 'D2');
    expect(d2).toBeDefined();
    expect(d2!.caseType).toBe('family'); // 2 family vs 1 civil
  });

  it('returns assignments ordered by last_seen DESC', async () => {
    const result = await getJudgeAssignments(pool, judgeAId, null);
    expect(result).toHaveLength(2);
    // D1 last_seen 2025-06-01 > D2 last_seen 2023-12-01
    expect(result[0].department).toBe('D1');
    expect(result[1].department).toBe('D2');
  });

  it('returns correct date range per department', async () => {
    const result = await getJudgeAssignments(pool, judgeAId, null);
    const d1 = result.find((a) => a.department === 'D1');
    expect(d1!.firstSeen).toBe('2024-01-01');
    expect(d1!.lastSeen).toBe('2025-06-01');

    const d2 = result.find((a) => a.department === 'D2');
    expect(d2!.firstSeen).toBe('2023-03-01');
    expect(d2!.lastSeen).toBe('2023-12-01');
  });

  it('returns null caseType when all cases have null case_type', async () => {
    const result = await getJudgeAssignments(pool, judgeBId, null);
    expect(result).toHaveLength(1);
    expect(result[0].department).toBe('D3');
    expect(result[0].caseType).toBeNull();
  });

  it('returns empty array for unknown judge', async () => {
    const result = await getJudgeAssignments(
      pool,
      '00000000-0000-0000-0000-000000000000',
      null,
    );
    expect(result).toEqual([]);
  });
});

describe('getJudgeAssignmentsBatch — integration', () => {
  it('returns most-frequent case_type per (judge, department)', async () => {
    const countyByJudge = new Map<string, string | null>([
      [judgeAId, null],
      [judgeBId, null],
    ]);
    const result = await getJudgeAssignmentsBatch(pool, [judgeAId, judgeBId], countyByJudge);
    expect(result).toHaveLength(2);

    const judgeAAssignments = result[0];
    expect(judgeAAssignments).toHaveLength(2);

    const d1 = judgeAAssignments.find((a) => a.department === 'D1');
    expect(d1!.caseType).toBe('civil');

    const d2 = judgeAAssignments.find((a) => a.department === 'D2');
    expect(d2!.caseType).toBe('family');

    const judgeBAssignments = result[1];
    expect(judgeBAssignments).toHaveLength(1);
    expect(judgeBAssignments[0].caseType).toBeNull();
  });

  it('orders each judge\'s assignments by last_seen DESC', async () => {
    const countyByJudge = new Map<string, string | null>([[judgeAId, null]]);
    const result = await getJudgeAssignmentsBatch(pool, [judgeAId], countyByJudge);
    const assignments = result[0];
    expect(assignments).toHaveLength(2);
    // D1 last_seen 2025-06-01 > D2 last_seen 2023-12-01
    expect(assignments[0].department).toBe('D1');
    expect(assignments[1].department).toBe('D2');
  });

  it('returns empty array for judge with no rulings', async () => {
    const unknownId = '00000000-0000-0000-0000-000000000000';
    const countyByJudge = new Map<string, string | null>([[unknownId, null]]);
    const result = await getJudgeAssignmentsBatch(pool, [unknownId], countyByJudge);
    expect(result).toEqual([[]]);
  });
});

describe('idx_rulings_judge_dept index — existence', () => {
  it('index exists after migrations', async () => {
    const { rows } = await pool.query<{ exists: number }>(
      `SELECT 1 AS exists FROM pg_indexes WHERE indexname = 'idx_rulings_judge_dept'`,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].exists).toBe(1);
  });
});
