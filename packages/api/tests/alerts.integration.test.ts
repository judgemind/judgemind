/**
 * Integration tests for the alert subscription system — GraphQL CRUD
 * operations, evaluateAlerts() matching engine, and sendAlertDigests()
 * email digest pipeline.
 *
 * Runs against a real PostgreSQL database. Each test run inserts its
 * own seed rows and deletes them in afterAll.
 *
 * DATA ISOLATION: This file uses county "Test Alert County" with court_code "ca-alert-test".
 * Do NOT reuse this court_code in other integration test files.
 * See tests/test-counties.ts for the full registry.
 */

import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest';
import { Pool, types } from 'pg';
import type { FastifyInstance } from 'fastify';
import { buildApp } from '../src/app';
import { signAccessToken } from '../src/auth/tokens';
import { evaluateAlerts } from '../src/alerts/evaluate';
import { sendAlertDigests } from '../src/alerts/digest';
import { applyMigrations } from './setup-db';
import { TEST_COUNTY_REGISTRY } from './test-counties';

// Extract county/court_code from registry for compile-time enforcement
const [ALERT_COUNTY] = TEST_COUNTY_REGISTRY.alerts.counties;
const [ALERT_COURT_CODE] = TEST_COUNTY_REGISTRY.alerts.courtCodes;

// Match the type parsers registered in src/data-access/db.ts so DATE columns
// come back as 'YYYY-MM-DD' strings rather than millisecond timestamps.
types.setTypeParser(1082, (val: string) => val);
types.setTypeParser(1114, (val: string) => val);
types.setTypeParser(1184, (val: string) => val);

// Mock the SES email sender so we don't send real emails during tests
vi.mock('../src/email/send', () => ({
  sendEmail: vi.fn().mockResolvedValue(undefined),
}));

const pool = new Pool({
  connectionString:
    process.env.TEST_DATABASE_URL ??
    'postgresql://judgemind:localdev@localhost:5432/judgemind_test',
});

let app: FastifyInstance;
let userId: string;
let accessToken: string;
let courtId: string;
let judgeId: string;
let caseId: string;
let rulingId: string;
let partyId: string;
let docId: string;

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

async function seedData(): Promise<void> {
  // Create a test user directly in the DB
  const { rows: uRows } = await pool.query<{ id: string }>(
    `INSERT INTO users (email, password_hash, email_verified, display_name, role)
     VALUES ('alert-test@example.com', 'not-a-real-hash', true, 'Alert Tester', 'user')
     RETURNING id`,
  );
  userId = uRows[0].id;

  // Sign a JWT for this user
  accessToken = signAccessToken({ sub: userId, email: 'alert-test@example.com', role: 'user' });

  // Create court
  const { rows: cRows } = await pool.query<{ id: string }>(
    `INSERT INTO courts (state, county, court_name, court_code, timezone)
     VALUES ('CA', '${ALERT_COUNTY}', 'Test Alert Court', '${ALERT_COURT_CODE}', 'America/Los_Angeles')
     RETURNING id`,
  );
  courtId = cRows[0].id;

  // Create judge
  const { rows: jRows } = await pool.query<{ id: string }>(
    `INSERT INTO judges (canonical_name, court_id, department, is_active)
     VALUES ('Alert Test Judge', $1, 'Dept. A', true)
     RETURNING id`,
    [courtId],
  );
  judgeId = jRows[0].id;

  // Create case
  const { rows: csRows } = await pool.query<{ id: string }>(
    `INSERT INTO cases (case_number, case_number_normalized, court_id, case_type, case_status, case_title, filed_at)
     VALUES ('ALERT-001', 'alert-001', $1, 'civil', 'active', 'Alert v. Test', '2024-01-01')
     RETURNING id`,
    [courtId],
  );
  caseId = csRows[0].id;

  // Create party
  const { rows: pRows } = await pool.query<{ id: string }>(
    `INSERT INTO parties (canonical_name, party_type) VALUES ('Alert Party', 'individual')
     RETURNING id`,
  );
  partyId = pRows[0].id;

  await pool.query(
    `INSERT INTO case_parties (case_id, party_id, role) VALUES ($1, $2, 'plaintiff')`,
    [caseId, partyId],
  );

  // Create document
  const { rows: dRows } = await pool.query<{ id: string }>(
    `INSERT INTO documents
       (case_id, court_id, document_type, s3_key, s3_bucket, format, content_hash,
        source_url, scraper_id, captured_at, hearing_date, status)
     VALUES ($1, $2, 'ruling', 'ca/alert-test/doc.html', 'judgemind-document-archive-dev',
             'html', 'alert-test-hash-001', 'https://example.com',
             'alert-test-scraper', NOW(), '2026-03-15', 'active')
     RETURNING id`,
    [caseId, courtId],
  );
  docId = dRows[0].id;

  // Create ruling
  const { rows: rRows } = await pool.query<{ id: string }>(
    `INSERT INTO rulings
       (document_id, case_id, judge_id, court_id, hearing_date, outcome, motion_type,
        is_tentative, department, ruling_text)
     VALUES ($1, $2, $3, $4, '2026-03-15', 'granted', 'msj', true, 'Dept. A',
             'The court grants the motion for summary judgment filed by plaintiff.')
     RETURNING id`,
    [docId, caseId, judgeId, courtId],
  );
  rulingId = rRows[0].id;
}

async function cleanupData(): Promise<void> {
  if (!userId) return;
  // Clean up alert data first (FK order)
  await pool.query(`DELETE FROM alert_events WHERE subscription_id IN (SELECT id FROM alert_subscriptions WHERE user_id = $1)`, [userId]);
  await pool.query(`DELETE FROM alert_subscriptions WHERE user_id = $1`, [userId]);
  // Clean up ruling/document/case/judge/court/party/user
  await pool.query(`DELETE FROM rulings WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM documents WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM case_parties WHERE case_id = $1`, [caseId]);
  await pool.query(`DELETE FROM cases WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM parties WHERE id = $1`, [partyId]);
  await pool.query(`DELETE FROM judges WHERE court_id = $1`, [courtId]);
  await pool.query(`DELETE FROM courts WHERE id = $1`, [courtId]);
  await pool.query(`DELETE FROM refresh_tokens WHERE user_id = $1`, [userId]);
  await pool.query(`DELETE FROM users WHERE id = $1`, [userId]);
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
// Helper
// ---------------------------------------------------------------------------

async function gql(query: string, variables?: Record<string, unknown>, headers?: Record<string, string>) {
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers: { 'content-type': 'application/json', ...headers },
    payload: JSON.stringify({ query, variables }),
  });
  return JSON.parse(res.body) as {
    data?: Record<string, unknown>;
    errors?: Array<{ message: string; extensions?: { code?: string } }>;
  };
}

function authHeaders(): Record<string, string> {
  return { authorization: `Bearer ${accessToken}` };
}

// ---------------------------------------------------------------------------
// Tests — GraphQL CRUD
// ---------------------------------------------------------------------------

describe('Alert subscriptions — integration', () => {
  let subscriptionId: string;

  describe('GraphQL mutations', () => {
    it('rejects createAlertSubscription when not authenticated', async () => {
      const body = await gql(
        `mutation { createAlertSubscription(alertType: "judge_ruling", filters: "{}") { id } }`,
      );
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });

    it('rejects invalid alert type', async () => {
      const body = await gql(
        `mutation { createAlertSubscription(alertType: "invalid_type", filters: "{}") { id } }`,
        undefined,
        authHeaders(),
      );
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('BAD_USER_INPUT');
      expect(body.errors![0].message).toContain('Invalid alert type');
    });

    it('rejects invalid JSON filters', async () => {
      const body = await gql(
        `mutation { createAlertSubscription(alertType: "judge_ruling", filters: "not json") { id } }`,
        undefined,
        authHeaders(),
      );
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('BAD_USER_INPUT');
      expect(body.errors![0].message).toContain('valid JSON');
    });

    it('creates a judge_ruling subscription', async () => {
      const filters = JSON.stringify({ judge_id: judgeId });
      const body = await gql(
        `mutation($type: String!, $filters: String!) {
          createAlertSubscription(alertType: $type, filters: $filters) {
            id alertType filters isActive createdAt
          }
        }`,
        { type: 'judge_ruling', filters },
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      const sub = body.data?.createAlertSubscription as Record<string, unknown>;
      expect(sub.alertType).toBe('judge_ruling');
      expect(sub.isActive).toBe(true);
      expect(sub.filters).toBe(filters);
      expect(sub.id).toBeDefined();
      subscriptionId = sub.id as string;
    });

    it('creates a keyword subscription', async () => {
      const filters = JSON.stringify({ keyword: 'summary judgment' });
      const body = await gql(
        `mutation($type: String!, $filters: String!) {
          createAlertSubscription(alertType: $type, filters: $filters) {
            id alertType filters
          }
        }`,
        { type: 'keyword', filters },
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      const sub = body.data?.createAlertSubscription as Record<string, unknown>;
      expect(sub.alertType).toBe('keyword');
    });
  });

  describe('GraphQL queries', () => {
    it('returns myAlerts for authenticated user', async () => {
      const body = await gql(
        `{ myAlerts { id alertType filters isActive createdAt } }`,
        undefined,
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      const alerts = body.data?.myAlerts as Array<Record<string, unknown>>;
      expect(alerts.length).toBeGreaterThanOrEqual(2);
      // Most recent first
      expect(alerts[0].alertType).toBe('keyword');
      expect(alerts[1].alertType).toBe('judge_ruling');
    });

    it('rejects myAlerts when not authenticated', async () => {
      const body = await gql(`{ myAlerts { id } }`);
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('UNAUTHENTICATED');
    });
  });

  describe('toggleAlertSubscription', () => {
    it('toggles a subscription off', async () => {
      const body = await gql(
        `mutation($id: ID!) { toggleAlertSubscription(id: $id, isActive: false) { id isActive } }`,
        { id: subscriptionId },
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      const sub = body.data?.toggleAlertSubscription as Record<string, unknown>;
      expect(sub.isActive).toBe(false);
    });

    it('toggles a subscription back on', async () => {
      const body = await gql(
        `mutation($id: ID!) { toggleAlertSubscription(id: $id, isActive: true) { id isActive } }`,
        { id: subscriptionId },
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      const sub = body.data?.toggleAlertSubscription as Record<string, unknown>;
      expect(sub.isActive).toBe(true);
    });

    it('rejects toggle for non-owned subscription', async () => {
      const body = await gql(
        `mutation { toggleAlertSubscription(id: "00000000-0000-0000-0000-000000000000", isActive: false) { id } }`,
        undefined,
        authHeaders(),
      );
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
    });
  });

  describe('evaluateAlerts', () => {
    it('creates alert events for matching subscriptions', async () => {
      const count = await evaluateAlerts(pool, rulingId);
      // The judge_ruling subscription matches (judge_id matches) and
      // the keyword subscription matches ("summary judgment" is in ruling_text).
      expect(count).toBe(2);

      // Verify events were created in the DB
      const { rows } = await pool.query(
        `SELECT * FROM alert_events WHERE subscription_id IN (
          SELECT id FROM alert_subscriptions WHERE user_id = $1
        )`,
        [userId],
      );
      expect(rows.length).toBe(2);
    });

    it('returns 0 for non-existent ruling', async () => {
      const count = await evaluateAlerts(pool, '00000000-0000-0000-0000-000000000000');
      expect(count).toBe(0);
    });
  });

  describe('sendAlertDigests', () => {
    it('sends digest emails for unsent alert events', async () => {
      const { sendEmail } = await import('../src/email/send');
      const mockSendEmail = sendEmail as ReturnType<typeof vi.fn>;
      mockSendEmail.mockClear();

      const emailsSent = await sendAlertDigests(pool);
      expect(emailsSent).toBe(1); // one user with pending events

      // Verify sendEmail was called with the right recipient
      expect(mockSendEmail).toHaveBeenCalledTimes(1);
      expect(mockSendEmail).toHaveBeenCalledWith(
        expect.objectContaining({
          to: 'alert-test@example.com',
          subject: expect.stringContaining('Alert Digest'),
        }),
      );

      // Verify events are now marked as sent
      const { rows } = await pool.query(
        `SELECT digest_sent FROM alert_events WHERE subscription_id IN (
          SELECT id FROM alert_subscriptions WHERE user_id = $1
        )`,
        [userId],
      );
      rows.forEach((row) => expect(row.digest_sent).toBe(true));
    });

    it('returns 0 when no unsent events exist', async () => {
      const emailsSent = await sendAlertDigests(pool);
      expect(emailsSent).toBe(0);
    });
  });

  describe('deleteAlertSubscription', () => {
    it('deletes an owned subscription', async () => {
      const body = await gql(
        `mutation($id: ID!) { deleteAlertSubscription(id: $id) }`,
        { id: subscriptionId },
        authHeaders(),
      );
      expect(body.errors).toBeUndefined();
      expect(body.data?.deleteAlertSubscription).toBe(true);
    });

    it('rejects deletion of non-owned subscription', async () => {
      const body = await gql(
        `mutation { deleteAlertSubscription(id: "00000000-0000-0000-0000-000000000000") }`,
        undefined,
        authHeaders(),
      );
      expect(body.errors).toBeDefined();
      expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
    });
  });
});
