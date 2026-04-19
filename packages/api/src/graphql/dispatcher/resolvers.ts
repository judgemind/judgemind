/**
 * Resolvers for the dispatcher v2 admin GraphQL surface. All resolvers
 * read directly from `dispatcher.*` and `public.users` — no caching in
 * Phase 1 (the admin page polls every 2s per §11, and the tables are
 * small enough that a cache would hide more than it helps).
 *
 * Gated on `user.role === 'admin'` via `requireDispatcherAdmin`. Non-admins
 * receive a generic "not found" error (see `./auth.ts`).
 *
 * `dispatcherControl` is idempotent at the command-row level — identical
 * commands issued within a 10-second window return the existing row
 * instead of inserting a duplicate.
 */

import type { Pool } from 'pg';
import { GraphQLError, GraphQLScalarType, Kind } from 'graphql';
import type { AuthUser } from '../../auth';
import { DESTRUCTIVE_COMMANDS, requireDispatcherAdmin } from './auth';

// Minimal Context subset the dispatcher resolvers read.
interface DispatcherContext {
  pool: Pool;
  user: AuthUser | null;
  mfaToken: string | null;
}

type Row = Record<string, unknown>;

// ---------------------------------------------------------------------------
// Column → GraphQL field shape converters
// ---------------------------------------------------------------------------

/** Convert a `dispatcher.agents` row into the GraphQL `DispatcherAgent` shape. */
function agentRowToGraphQL(row: Row): Record<string, unknown> {
  return {
    id: row.agent_id,
    kind: row.kind,
    issueNumber: row.issue_number,
    worktreePath: row.worktree_path,
    phase: row.phase,
    status: row.status,
    startedAt: row.started_at,
    endedAt: row.ended_at,
    exitCode: row.exit_code,
    prNumber: row.pr_number,
    retriesUsed: row.retries_used,
    // phaseTransitions / failures are resolved lazily by their field resolvers
    __agent_id: row.agent_id,
  };
}

/** Convert a `dispatcher.runs` row into the GraphQL `DispatcherRun` shape. */
function runRowToGraphQL(row: Row): Record<string, unknown> {
  return {
    runId: row.run_id,
    startedAt: row.started_at,
    stoppedAt: row.stopped_at,
    heartbeatTs: row.heartbeat_ts,
    versionSha: row.version_sha,
    host: row.host,
    pid: row.pid,
  };
}

/** Convert a `dispatcher.failures` row into the GraphQL `DispatcherFailure` shape. */
function failureRowToGraphQL(row: Row): Record<string, unknown> {
  return {
    failureId: row.failure_id,
    agentId: row.agent_id,
    category: row.category,
    detectedBy: row.detected_by,
    details: row.details ?? {},
    ts: row.ts,
    issueNumber: row.issue_number ?? null,
  };
}

/** Convert a `dispatcher.phase_transitions` row into the GraphQL `PhaseTransition` shape. */
function transitionRowToGraphQL(row: Row): Record<string, unknown> {
  return {
    transitionId: row.transition_id,
    phase: row.phase,
    ts: row.ts,
    autocompactCount: row.autocompact_count,
  };
}

/** Convert a `dispatcher.commands` row into the GraphQL `DispatcherCommandResult` shape. */
function commandRowToGraphQL(row: Row, created: boolean): Record<string, unknown> {
  return {
    commandId: row.command_id,
    command: row.command,
    issuedBy: row.issued_by,
    issuedAt: row.issued_at,
    consumedAt: row.consumed_at,
    payload: row.payload ?? {},
    created,
  };
}

// ---------------------------------------------------------------------------
// Data access — dispatcherState
// ---------------------------------------------------------------------------

interface DispatcherStateArgs {
  // No top-level args; nested fields have their own args.
}

async function queryCurrentRun(pool: Pool): Promise<Row | null> {
  const { rows } = await pool.query<Row>(
    `SELECT * FROM dispatcher.runs ORDER BY started_at DESC LIMIT 1`,
  );
  return rows[0] ?? null;
}

async function queryActiveAgents(pool: Pool): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `SELECT * FROM dispatcher.agents WHERE status = 'running' ORDER BY started_at ASC`,
  );
  return rows;
}

async function queryRecentFailures(pool: Pool, sinceHours: number): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `SELECT f.*, a.issue_number
       FROM dispatcher.failures f
       LEFT JOIN dispatcher.agents a ON a.agent_id = f.agent_id
      WHERE f.ts >= NOW() - ($1 || ' hours')::interval
      ORDER BY f.ts DESC`,
    [String(sinceHours)],
  );
  return rows;
}

async function querySpawnFrozenUntil(pool: Pool): Promise<string | null> {
  const { rows } = await pool.query<{ value: unknown }>(
    `SELECT value FROM dispatcher.config WHERE key = 'spawn_frozen_until'`,
  );
  if (rows.length === 0) return null;
  const raw = rows[0].value;
  // jsonb comes back as the native JSON value; wrap string vs null carefully.
  if (raw === null || raw === undefined) return null;
  // Expect either a string (ISO-8601) or a JSON string literal
  if (typeof raw === 'string') return raw;
  return null;
}

async function queryQueueDepth(pool: Pool): Promise<number> {
  // Phase 1: no persisted queue table — always 0. When the daemon queue
  // scan lands (sub-task C follow-up), swap this for a COUNT(*) against
  // the real source. Returning 0 keeps the contract (Int!) honored.
  void pool; // retain signature symmetry
  return 0;
}

// ---------------------------------------------------------------------------
// Data access — dispatcherAgent
// ---------------------------------------------------------------------------

async function queryAgent(pool: Pool, agentId: string): Promise<Row | null> {
  const { rows } = await pool.query<Row>(
    `SELECT * FROM dispatcher.agents WHERE agent_id = $1`,
    [agentId],
  );
  return rows[0] ?? null;
}

async function queryPhaseTransitions(pool: Pool, agentId: string): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `SELECT * FROM dispatcher.phase_transitions WHERE agent_id = $1 ORDER BY ts ASC`,
    [agentId],
  );
  return rows;
}

async function queryFailuresForAgent(pool: Pool, agentId: string): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `SELECT f.*, a.issue_number
       FROM dispatcher.failures f
       LEFT JOIN dispatcher.agents a ON a.agent_id = f.agent_id
      WHERE f.agent_id = $1
      ORDER BY f.ts DESC`,
    [agentId],
  );
  return rows;
}

// ---------------------------------------------------------------------------
// Data access — dispatcherControl mutation (idempotent insert)
// ---------------------------------------------------------------------------

/**
 * Idempotency window: identical rows (same command + issuedBy + payload)
 * written in the last 10 seconds that are still unconsumed are returned
 * instead of inserting a duplicate. This protects the admin page from
 * spurious duplicate inserts caused by e.g. double-clicking a button or
 * a retry after a transient network hiccup.
 */
const IDEMPOTENCY_WINDOW_SECONDS = 10;

async function insertCommandIdempotent(
  pool: Pool,
  command: string,
  issuedBy: string,
  payload: Record<string, unknown>,
): Promise<{ row: Row; created: boolean }> {
  // Serialize payload for the INSERT. For equality comparison we use jsonb's
  // native `=` operator — jsonb normalizes key order and whitespace, so
  // semantically-identical payloads match even if the surface JSON differs
  // (e.g. `{"a":1,"b":2}` vs `{"b":2,"a":1}`).
  const payloadJson = JSON.stringify(payload);

  // First look for an existing unconsumed row in the window.
  const existing = await pool.query<Row>(
    `SELECT * FROM dispatcher.commands
      WHERE command = $1
        AND issued_by = $2
        AND payload = $3::jsonb
        AND consumed_at IS NULL
        AND issued_at >= NOW() - ($4 || ' seconds')::interval
      ORDER BY issued_at DESC
      LIMIT 1`,
    [command, issuedBy, payloadJson, String(IDEMPOTENCY_WINDOW_SECONDS)],
  );
  if (existing.rows.length > 0) {
    return { row: existing.rows[0], created: false };
  }

  // Insert a new row.
  const insert = await pool.query<Row>(
    `INSERT INTO dispatcher.commands (command, issued_by, payload)
     VALUES ($1, $2, $3::jsonb)
     RETURNING *`,
    [command, issuedBy, payloadJson],
  );
  return { row: insert.rows[0], created: true };
}

// ---------------------------------------------------------------------------
// Resolvers
// ---------------------------------------------------------------------------

export const dispatcherResolvers = {
  Query: {
    dispatcherState: async (
      _: unknown,
      __: DispatcherStateArgs,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const [currentRunRow, spawnFrozenUntil] = await Promise.all([
        queryCurrentRun(pool),
        querySpawnFrozenUntil(pool),
      ]);
      return {
        // Nested fields defer to DispatcherState field resolvers.
        __currentRun: currentRunRow,
        __spawnFrozenUntil: spawnFrozenUntil,
      };
    },

    dispatcherAgent: async (
      _: unknown,
      { agentId }: { agentId: string },
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const row = await queryAgent(pool, agentId);
      return row ? agentRowToGraphQL(row) : null;
    },
  },

  Mutation: {
    dispatcherControl: async (
      _: unknown,
      {
        command,
        payload,
      }: { command: string; payload?: Record<string, unknown> | null },
      { pool, user, mfaToken }: DispatcherContext,
    ) => {
      const admin = requireDispatcherAdmin(user);

      // Destructive commands require a fresh re-auth token per §17 Risk 6.
      // TODO(#2730 follow-up): Phase 1 placeholder — accept any non-empty
      // X-MFA-Token header. Sub-task E or a follow-up wires the real MFA
      // challenge flow (short-lived token issued by a dedicated
      // challenge-verify mutation).
      if (DESTRUCTIVE_COMMANDS.has(command)) {
        if (!mfaToken || mfaToken.trim().length === 0) {
          throw new GraphQLError('MFA re-auth required for destructive commands', {
            extensions: { code: 'MFA_REQUIRED' },
          });
        }
      }

      const effectivePayload = payload ?? {};
      const { row, created } = await insertCommandIdempotent(
        pool,
        command,
        admin.email,
        effectivePayload,
      );
      return commandRowToGraphQL(row, created);
    },
  },

  DispatcherState: {
    currentRun: (parent: Record<string, unknown>) => {
      const raw = parent.__currentRun as Row | null | undefined;
      return raw ? runRowToGraphQL(raw) : null;
    },

    activeAgents: async (_: unknown, __: unknown, { pool, user }: DispatcherContext) => {
      // Parent resolver already admin-gated; re-check for defence-in-depth.
      requireDispatcherAdmin(user);
      const rows = await queryActiveAgents(pool);
      return rows.map(agentRowToGraphQL);
    },

    recentFailures: async (
      _: unknown,
      { sinceHours }: { sinceHours?: number | null },
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const hours = sinceHours ?? 24;
      const rows = await queryRecentFailures(pool, hours);
      return rows.map(failureRowToGraphQL);
    },

    queueDepth: async (_: unknown, __: unknown, { pool, user }: DispatcherContext) => {
      requireDispatcherAdmin(user);
      return queryQueueDepth(pool);
    },

    spawnFrozenUntil: (parent: Record<string, unknown>) => {
      return (parent.__spawnFrozenUntil as string | null) ?? null;
    },
  },

  DispatcherAgent: {
    phaseTransitions: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const agentId = parent.__agent_id ?? parent.id;
      const rows = await queryPhaseTransitions(pool, String(agentId));
      return rows.map(transitionRowToGraphQL);
    },

    failures: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const agentId = parent.__agent_id ?? parent.id;
      const rows = await queryFailuresForAgent(pool, String(agentId));
      return rows.map(failureRowToGraphQL);
    },
  },
};

// ---------------------------------------------------------------------------
// Scalar shims — DateTime and JSON are declared but otherwise pass-through.
// These live here rather than a shared scalars file because the dispatcher
// module is the only current consumer. If other modules need them, lift
// into `src/graphql/scalars.ts`.
// ---------------------------------------------------------------------------

function serializeDateTime(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

/** DateTime is represented as an ISO-8601 string on the wire. */
const DateTimeScalar = new GraphQLScalarType({
  name: 'DateTime',
  description: 'ISO-8601 timestamp string (e.g. "2026-04-18T19:00:00Z").',
  serialize: serializeDateTime,
  parseValue(value: unknown): string | null {
    return value === null || value === undefined ? null : String(value);
  },
  parseLiteral(ast): string | null {
    if (ast.kind === Kind.STRING) return ast.value;
    if (ast.kind === Kind.NULL) return null;
    return null;
  },
});

/** Opaque JSON scalar — pass-through in both directions. */
function recursiveParseLiteral(ast: unknown): unknown {
  const node = ast as { kind?: string; value?: unknown; values?: unknown[]; fields?: unknown[] };
  switch (node.kind) {
    case Kind.STRING:
    case Kind.BOOLEAN:
      return node.value;
    case Kind.INT:
    case Kind.FLOAT:
      return Number(node.value);
    case Kind.NULL:
      return null;
    case Kind.LIST:
      return (node.values ?? []).map((v) => recursiveParseLiteral(v));
    case Kind.OBJECT: {
      const out: Record<string, unknown> = {};
      for (const field of node.fields ?? []) {
        const f = field as { name: { value: string }; value: unknown };
        out[f.name.value] = recursiveParseLiteral(f.value);
      }
      return out;
    }
    default:
      return null;
  }
}

const JSONScalar = new GraphQLScalarType({
  name: 'JSON',
  description: 'Opaque JSON payload — object shape varies by field.',
  serialize: (value: unknown) => value,
  parseValue: (value: unknown) => value,
  parseLiteral(ast): unknown {
    return recursiveParseLiteral(ast);
  },
});

export const dispatcherScalarResolvers = {
  DateTime: DateTimeScalar,
  JSON: JSONScalar,
};

// Internal exports for unit testing.
export {
  agentRowToGraphQL,
  commandRowToGraphQL,
  failureRowToGraphQL,
  insertCommandIdempotent,
  runRowToGraphQL,
  transitionRowToGraphQL,
};
