/**
 * Integration tests for the rulings_block_non_active_document trigger (#3728).
 *
 * Verifies that the DB trigger prevents INSERT of rulings onto non-active
 * (e.g. superseded) documents by raising a check_violation error.
 *
 * DATA ISOLATION: This file uses county "Test Trigger County" with court_code
 * "ca-trigger-test". Do NOT reuse this court_code in other integration test
 * files. See tests/test-counties.ts for the full registry.
 */

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool } from 'pg';
import { applyMigrations } from './setup-db';

const pool = new Pool({
  connectionString:
    process.env.TEST_DATABASE_URL ??
    'postgresql://judgemind:localdev@localhost:5432/judgemind_test',
});

const TRIGGER_COUNTY = 'Test Trigger County';
const TRIGGER_COURT_CODE = 'ca-trigger-test';

let courtId: string;
let judgeId: string;
let caseId: string;
let activeDocId: string;
let supersededDocId: string;

async function seedData(): Promise<void> {
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', $1, 'Superior Court of California, County of Test Trigger', $2, 'America/Los_Angeles')
     RETURNING id`,
    [TRIGGER_COUNTY, TRIGGER_COURT_CODE],
  );
  courtId = cRows[0].id;

  const { rows: jRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Trigger, Test Judge', $1, 'Dept. T', true)
     RETURNING id`,
    [courtId],
  );
  judgeId = jRows[0].id;

  const { rows: csRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, case_number_normalized, court_id, case_type, case_status, case_title, filed_at)
     VALUES ('99TRIGGER01', '99trigger01', $1, 'civil', 'active', 'Trigger Test v. Trigger', '2024-01-01')
     RETURNING id`,
    [courtId],
  );
  caseId = csRows[0].id;

  // Active document — used to verify that INSERT INTO rulings still works normally.
  const { rows: aRows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/trigger/active-doc.html', 'judgemind-document-archive-dev',
             'html', 'triggerhash001', 'https://example.com',
             'ca-trigger-test', NOW(), '2026-04-01', 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  activeDocId = aRows[0].id;

  // Superseded document — INSERT INTO rulings referencing this should fail.
  const { rows: sRows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/trigger/superseded-doc.html', 'judgemind-document-archive-dev',
             'html', 'triggerhash002', 'https://example.com',
             'ca-trigger-test', NOW(), '2026-04-01', 'superseded')
     RETURNING id`,
    [caseId, courtId],
  );
  supersededDocId = sRows[0].id;
}

async function cleanupData(): Promise<void> {
  if (!courtId) return;
  await pool.query(`DELETE FROM rulings WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM documents WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM case_judges WHERE case_id = $1`, [caseId]);
  await pool.query(`DELETE FROM cases WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM judges WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM courts WHERE id = $1`, [courtId]);
}

beforeAll(async () => {
  applyMigrations();
  await seedData();
}, 30_000);

afterAll(async () => {
  await cleanupData();
  await pool.end();
}, 15_000);

describe('rulings_block_non_active_document trigger', () => {
  it('allows INSERT of a ruling onto an active document', async () => {
    const { rows } = await pool.query<{ id: string }>(
      `INSERT INTO rulings
         (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
          is_tentative, department, ruling_text)
       VALUES ($1, $2, $3, $4, '2026-04-01', 'granted', 'msj', true, 'Dept. T',
               'Active doc ruling text.')
       RETURNING id`,
      [activeDocId, caseId, judgeId, courtId],
    );
    expect(rows[0].id).toBeTruthy();

    // Clean up the inserted ruling
    await pool.query(`DELETE FROM rulings WHERE id = $1`, [rows[0].id]);
  });

  it('rejects INSERT of a ruling onto a superseded document', async () => {
    await expect(
      pool.query(
        `INSERT INTO rulings
           (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
            is_tentative, department, ruling_text)
         VALUES ($1, $2, $3, $4, '2026-04-01', 'denied', 'mtd', true, 'Dept. T',
                 'Superseded doc ruling text.')`,
        [supersededDocId, caseId, judgeId, courtId],
      ),
    ).rejects.toThrow(/cannot insert\/update ruling/);
  });

  it('rejects INSERT with the document status in the error message', async () => {
    let thrownError: Error | null = null;
    try {
      await pool.query(
        `INSERT INTO rulings
           (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
            is_tentative, department, ruling_text)
         VALUES ($1, $2, $3, $4, '2026-04-01', 'denied', 'mtd', true, 'Dept. T',
                 'Superseded doc ruling text 2.')`,
        [supersededDocId, caseId, judgeId, courtId],
      );
    } catch (err) {
      thrownError = err as Error;
    }
    expect(thrownError).not.toBeNull();
    expect(thrownError!.message).toMatch(/superseded/);
  });

  it('bypasses trigger via session_replication_role replica (seed pattern for integration tests)', async () => {
    // This pattern is used by integration test seeds that need to insert orphan
    // rulings for testing the resolver's status filter.
    await pool.query(`SET session_replication_role = 'replica'`);
    try {
      const { rows } = await pool.query<{ id: string }>(
        `INSERT INTO rulings
           (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
            is_tentative, department, ruling_text)
         VALUES ($1, $2, $3, $4, '2026-04-01', 'granted', 'msj', true, 'Dept. T',
                 'Orphan ruling inserted via replica role bypass.')
         RETURNING id`,
        [supersededDocId, caseId, judgeId, courtId],
      );
      expect(rows[0].id).toBeTruthy();
      // Clean up
      await pool.query(`DELETE FROM rulings WHERE id = $1`, [rows[0].id]);
    } finally {
      await pool.query(`SET session_replication_role = 'origin'`);
    }
  });
});
