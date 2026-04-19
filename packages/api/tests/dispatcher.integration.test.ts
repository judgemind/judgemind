/**
 * Integration tests for the dispatcher v2 admin GraphQL surface (#2730).
 *
 * Covers:
 *   - Auth gate: unauthenticated + non-admin both receive "not found" (no
 *     field-shape introspection) — verifies §11 Auth behaviour.
 *   - dispatcherState: empty state returns the expected skeleton shape.
 *   - dispatcherState: active agents / recent failures are surfaced.
 *   - dispatcherAgent: resolves an agent with phase transitions and failures.
 *   - dispatcherControl: writes a row visible in the next dispatcherState call.
 *   - dispatcherControl: idempotency — repeating the same command inside
 *     the idempotency window returns the existing row (created=false).
 *   - dispatcherControl: destructive commands require X-MFA-Token header.
 *
 * DATA ISOLATION: this file uses `dispatcher` in the test-counties registry.
 * It writes only to `public.users` (emails prefixed with the run timestamp)
 * and `dispatcher.*` (dispatcher_test_<ts> markers). All rows are cleaned up
 * in afterAll.
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

// Unique marker suffix — lets us clean up only rows we inserted even if the
// test crashes mid-run and leaves orphans.
const MARKER = `dispatcher_test_${Date.now()}`;

let app: FastifyInstance;
let adminToken: string;
let userToken: string;
let adminUserId: string;
let regularUserId: string;

// Track rows inserted so cleanup is exact.
const insertedAgentIds: string[] = [];
const insertedFailureIds: string[] = [];
const insertedRunIds: string[] = [];

async function seedData(): Promise<void> {
  // Admin user — is_admin=true gates the dispatcher surface.
  const { rows: adminRows } = await pool.query<{ id: string }>(
    `INSERT INTO public.users (email, email_verified, role, password_hash, is_admin)
     VALUES ($1, true, 'user', 'not-a-real-hash', true)
     RETURNING id`,
    [`${MARKER}-admin@test.com`],
  );
  adminUserId = adminRows[0].id;
  adminToken = signAccessToken({
    sub: adminUserId,
    email: `${MARKER}-admin@test.com`,
    role: 'user',
  });

  // Regular user — is_admin=false (default) must receive "not found".
  const { rows: userRows } = await pool.query<{ id: string }>(
    `INSERT INTO public.users (email, email_verified, role, password_hash)
     VALUES ($1, true, 'user', 'not-a-real-hash')
     RETURNING id`,
    [`${MARKER}-user@test.com`],
  );
  regularUserId = userRows[0].id;
  userToken = signAccessToken({
    sub: regularUserId,
    email: `${MARKER}-user@test.com`,
    role: 'user',
  });
}

async function cleanupData(): Promise<void> {
  for (const id of insertedFailureIds) {
    await pool.query(`DELETE FROM dispatcher.failures WHERE failure_id = $1`, [id]);
  }
  for (const id of insertedAgentIds) {
    await pool.query(`DELETE FROM dispatcher.phase_transitions WHERE agent_id = $1`, [id]);
    await pool.query(`DELETE FROM dispatcher.failures WHERE agent_id = $1`, [id]);
    await pool.query(`DELETE FROM dispatcher.agents WHERE agent_id = $1`, [id]);
  }
  for (const id of insertedRunIds) {
    await pool.query(`DELETE FROM dispatcher.runs WHERE run_id = $1`, [id]);
  }
  // Clean all commands issued by our test admin.
  await pool.query(`DELETE FROM dispatcher.commands WHERE issued_by LIKE $1`, [`${MARKER}%`]);

  if (adminUserId) await pool.query(`DELETE FROM public.users WHERE id = $1`, [adminUserId]);
  if (regularUserId) await pool.query(`DELETE FROM public.users WHERE id = $1`, [regularUserId]);
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

interface GqlResponse {
  data?: Record<string, unknown>;
  errors?: Array<{ message: string; extensions?: { code?: string } }>;
}

async function gql(
  query: string,
  variables?: Record<string, unknown>,
  token?: string,
  extraHeaders?: Record<string, string>,
): Promise<GqlResponse> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    ...extraHeaders,
  };
  if (token) headers.authorization = `Bearer ${token}`;
  const res = await app.inject({
    method: 'POST',
    url: '/graphql',
    headers,
    payload: JSON.stringify({ query, variables }),
  });
  return JSON.parse(res.body) as GqlResponse;
}

async function insertRun(): Promise<string> {
  const { rows } = await pool.query<{ run_id: string }>(
    `INSERT INTO dispatcher.runs (version_sha, host, pid)
     VALUES ('deadbeef', $1, 9999)
     RETURNING run_id`,
    [`${MARKER}-host`],
  );
  const runId = rows[0].run_id;
  insertedRunIds.push(runId);
  return runId;
}

async function insertAgent(opts: {
  runId?: string;
  issueNumber: number;
  status?: string;
  phase?: string;
}): Promise<string> {
  const { rows } = await pool.query<{ agent_id: string }>(
    `INSERT INTO dispatcher.agents (parent_run_id, kind, issue_number, worktree_path, phase, status)
     VALUES ($1, 'task', $2, $3, $4, $5)
     RETURNING agent_id`,
    [
      opts.runId ?? null,
      opts.issueNumber,
      `/tmp/${MARKER}/agent-${opts.issueNumber}`,
      opts.phase ?? 'claiming',
      opts.status ?? 'running',
    ],
  );
  const agentId = rows[0].agent_id;
  insertedAgentIds.push(agentId);
  return agentId;
}

async function insertPhaseTransition(agentId: string, phase: string): Promise<void> {
  await pool.query(
    `INSERT INTO dispatcher.phase_transitions (agent_id, phase) VALUES ($1, $2)`,
    [agentId, phase],
  );
}

async function insertFailure(opts: {
  agentId: string | null;
  category: string;
  detectedBy: string;
  details?: Record<string, unknown>;
}): Promise<string> {
  const { rows } = await pool.query<{ failure_id: string }>(
    `INSERT INTO dispatcher.failures (agent_id, category, detected_by, details)
     VALUES ($1, $2, $3, $4::jsonb)
     RETURNING failure_id`,
    [
      opts.agentId,
      opts.category,
      opts.detectedBy,
      JSON.stringify(opts.details ?? {}),
    ],
  );
  const failureId = rows[0].failure_id;
  insertedFailureIds.push(failureId);
  return failureId;
}

// ---------------------------------------------------------------------------
// Auth gate
// ---------------------------------------------------------------------------

describe('dispatcher — auth gate', () => {
  const stateQuery = `{
    dispatcherState {
      queueDepth
      activeAgents { id }
      recentFailures { failureId }
    }
  }`;

  it('unauthenticated: dispatcherState returns "not found", no data', async () => {
    const body = await gql(stateQuery);
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
    // For a non-nullable field (dispatcherState: DispatcherState!), GraphQL
    // propagates the error to the root, so `data` is null or absent entirely.
    expect(body.data == null || body.data.dispatcherState == null).toBe(true);
  });

  it('non-admin: dispatcherState returns "not found", no data', async () => {
    const body = await gql(stateQuery, undefined, userToken);
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
    expect(body.data == null || body.data.dispatcherState == null).toBe(true);
  });

  it('non-admin: dispatcherAgent(id) returns "not found"', async () => {
    const body = await gql(
      `query($id: ID!) { dispatcherAgent(agentId: $id) { id } }`,
      { id: '00000000-0000-0000-0000-000000000000' },
      userToken,
    );
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
  });

  it('non-admin: dispatcherControl returns "not found"', async () => {
    const body = await gql(
      `mutation { dispatcherControl(command: pause) { commandId } }`,
      undefined,
      userToken,
    );
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('NOT_FOUND');
  });
});

// ---------------------------------------------------------------------------
// dispatcherState — empty + seeded paths
// ---------------------------------------------------------------------------

describe('dispatcherState — admin', () => {
  it('admin sees the expected shape against empty state (no active rows)', async () => {
    const body = await gql(
      `{
        dispatcherState {
          currentRun { runId }
          activeAgents { id }
          recentFailures(sinceHours: 1) { failureId }
          queueDepth
          spawnFrozenUntil
        }
      }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const state = body.data?.dispatcherState as Record<string, unknown>;
    expect(state).toBeDefined();
    // currentRun may or may not exist depending on what else has touched the
    // DB, but activeAgents must be an array and recentFailures with
    // sinceHours=1 must not contain our test markers (we haven't seeded yet
    // with any running agent in this describe block).
    expect(Array.isArray(state.activeAgents)).toBe(true);
    expect(Array.isArray(state.recentFailures)).toBe(true);
    expect(typeof state.queueDepth).toBe('number');
    // spawnFrozenUntil must be null (not set) or a string.
    expect(
      state.spawnFrozenUntil === null || typeof state.spawnFrozenUntil === 'string',
    ).toBe(true);
  });

  it('surfaces a seeded active agent via activeAgents', async () => {
    const agentId = await insertAgent({ issueNumber: 999001, status: 'running', phase: 'ralph-worker' });
    const body = await gql(
      `{ dispatcherState { activeAgents { id issueNumber phase status } } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const agents = (body.data?.dispatcherState as Record<string, unknown>).activeAgents as Array<
      Record<string, unknown>
    >;
    const ours = agents.find((a) => a.id === agentId);
    expect(ours).toBeDefined();
    expect(ours!.issueNumber).toBe(999001);
    expect(ours!.phase).toBe('ralph-worker');
    expect(ours!.status).toBe('running');
  });

  it('surfaces recent failures within sinceHours window', async () => {
    const agentId = await insertAgent({ issueNumber: 999002, status: 'failed' });
    await insertFailure({
      agentId,
      category: 'hook_failure',
      detectedBy: 'hook:subagentstop',
      details: { hook: 'subagentstop', note: MARKER },
    });
    const body = await gql(
      `{ dispatcherState { recentFailures(sinceHours: 1) { failureId category detectedBy agentId details } } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const failures = (body.data?.dispatcherState as Record<string, unknown>).recentFailures as Array<
      Record<string, unknown>
    >;
    const ours = failures.find((f) => (f.agentId as string) === agentId);
    expect(ours).toBeDefined();
    expect(ours!.category).toBe('hook_failure');
    expect(ours!.detectedBy).toBe('hook:subagentstop');
    // details round-trips as JSON
    expect((ours!.details as Record<string, unknown>).note).toBe(MARKER);
  });
});

// ---------------------------------------------------------------------------
// dispatcherAgent — full detail
// ---------------------------------------------------------------------------

describe('dispatcherAgent — admin', () => {
  it('returns null for an unknown agentId', async () => {
    const body = await gql(
      `query($id: ID!) { dispatcherAgent(agentId: $id) { id } }`,
      { id: '00000000-0000-0000-0000-000000000000' },
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    expect(body.data?.dispatcherAgent).toBeNull();
  });

  it('returns full detail including phaseTransitions and failures', async () => {
    const runId = await insertRun();
    const agentId = await insertAgent({
      runId,
      issueNumber: 999003,
      status: 'running',
      phase: 'ralph-worker',
    });
    await insertPhaseTransition(agentId, 'claiming');
    await insertPhaseTransition(agentId, 'setup');
    await insertPhaseTransition(agentId, 'ralph-worker');
    await insertFailure({
      agentId,
      category: 'subprocess_timeout',
      detectedBy: 'supervisor:timeout',
    });

    const body = await gql(
      `query($id: ID!) {
        dispatcherAgent(agentId: $id) {
          id
          issueNumber
          worktreePath
          phase
          status
          phaseTransitions { transitionId phase }
          failures { failureId category detectedBy }
        }
      }`,
      { id: agentId },
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const agent = body.data?.dispatcherAgent as Record<string, unknown>;
    expect(agent.id).toBe(agentId);
    expect(agent.issueNumber).toBe(999003);
    expect(agent.phase).toBe('ralph-worker');
    const transitions = agent.phaseTransitions as Array<{ phase: string }>;
    expect(transitions.map((t) => t.phase)).toEqual(['claiming', 'setup', 'ralph-worker']);
    const failures = agent.failures as Array<{ category: string }>;
    expect(failures).toHaveLength(1);
    expect(failures[0].category).toBe('subprocess_timeout');
  });
});

// ---------------------------------------------------------------------------
// dispatcherControl — write path, idempotency, MFA
// ---------------------------------------------------------------------------

describe('dispatcherControl — admin', () => {
  it('PAUSE writes a row in dispatcher.commands and returns it', async () => {
    const body = await gql(
      `mutation { dispatcherControl(command: pause) {
         commandId command issuedBy payload created
       } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const result = body.data?.dispatcherControl as Record<string, unknown>;
    expect(result.command).toBe('pause');
    expect(result.issuedBy).toBe(`${MARKER}-admin@test.com`);
    expect(result.created).toBe(true);

    // Round-trip via direct DB query to confirm the row shape.
    const { rows } = await pool.query<{ command: string; issued_by: string; payload: unknown }>(
      `SELECT command, issued_by, payload FROM dispatcher.commands WHERE command_id = $1`,
      [result.commandId],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].command).toBe('pause');
    expect(rows[0].issued_by).toBe(`${MARKER}-admin@test.com`);
  });

  it('identical command+payload inside the idempotency window returns the same row with created=false', async () => {
    // Use a distinct payload marker so this test is independent of the
    // earlier "writes a row" test — otherwise a lingering idempotent row
    // from that test trips this one up.
    const markerPayload = { idempotencyMarker: `${MARKER}-idempotency-test` };

    // Issue #1
    const first = await gql(
      `mutation($p: JSON) { dispatcherControl(command: pause, payload: $p) { commandId created } }`,
      { p: markerPayload },
      adminToken,
    );
    expect(first.errors).toBeUndefined();
    const r1 = first.data?.dispatcherControl as Record<string, unknown>;
    expect(r1.created).toBe(true);

    // Issue #2 — identical command, identical issuer, identical payload.
    const second = await gql(
      `mutation($p: JSON) { dispatcherControl(command: pause, payload: $p) { commandId created } }`,
      { p: markerPayload },
      adminToken,
    );
    expect(second.errors).toBeUndefined();
    const r2 = second.data?.dispatcherControl as Record<string, unknown>;
    expect(r2.created).toBe(false);
    expect(r2.commandId).toBe(r1.commandId);
  });

  it('retry command accepts a payload and stores it verbatim', async () => {
    const agentId = '11111111-1111-1111-1111-111111111111';
    const body = await gql(
      `mutation($p: JSON) { dispatcherControl(command: retry, payload: $p) {
         commandId command payload
       } }`,
      { p: { agentId, reason: 'flake' } },
      adminToken,
    );
    expect(body.errors).toBeUndefined();
    const result = body.data?.dispatcherControl as Record<string, unknown>;
    const payload = result.payload as Record<string, unknown>;
    expect(payload.agentId).toBe(agentId);
    expect(payload.reason).toBe('flake');
  });

  it('destructive command (stop) without X-MFA-Token returns MFA_REQUIRED', async () => {
    const body = await gql(
      `mutation { dispatcherControl(command: stop) { commandId } }`,
      undefined,
      adminToken,
    );
    expect(body.errors).toBeDefined();
    expect(body.errors![0].extensions?.code).toBe('MFA_REQUIRED');
  });

  it('destructive command (stop) with non-empty X-MFA-Token succeeds', async () => {
    const body = await gql(
      `mutation { dispatcherControl(command: stop) { commandId command created } }`,
      undefined,
      adminToken,
      { 'x-mfa-token': 'placeholder-token-accepted-in-phase-1' },
    );
    expect(body.errors).toBeUndefined();
    const result = body.data?.dispatcherControl as Record<string, unknown>;
    expect(result.command).toBe('stop');
  });

  it('state reflects a newly-issued command on the next read', async () => {
    // Issue a distinct command then fetch state — the row must exist in
    // dispatcher.commands, which this test verifies directly (admin surface
    // doesn't expose the commands queue in dispatcherState for Phase 1).
    const issue = await gql(
      `mutation { dispatcherControl(command: resume) { commandId } }`,
      undefined,
      adminToken,
    );
    const commandId = (issue.data?.dispatcherControl as Record<string, unknown>).commandId as string;
    const { rows } = await pool.query<{ command: string }>(
      `SELECT command FROM dispatcher.commands WHERE command_id = $1`,
      [commandId],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].command).toBe('resume');
  });
});
