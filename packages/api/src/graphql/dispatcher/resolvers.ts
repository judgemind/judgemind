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
import { requireDispatcherAdmin } from './auth';
import { extractPriority, parseBlockedBy, priorityRank } from './parse-labels';

/**
 * Shape of a single entry in the enrichment JSON the daemon writes
 * into ``dispatcher.queue_snapshots.issues_json`` /
 * ``dispatcher.blocked_snapshots.issues_json`` (issue #2820). Must
 * stay wire-compatible with ``_normalize_issue_enrichment`` in
 * ``scripts/dispatcher/daemon.py``.
 */
interface SnapshotIssueRecord {
  number: number;
  title: string;
  labels: string[];
  createdAt: string;
  /** Only populated for blocked snapshots (daemon omits on queue scan). */
  body?: string | null;
}

// Minimal Context subset the dispatcher resolvers read.
interface DispatcherContext {
  pool: Pool;
  user: AuthUser | null;
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
    // Issue title captured at claim time (#2820). Pre-migration-28 rows
    // have NULL which the UI renders as a fallback to `#<number>`.
    issueTitle:
      typeof row.issue_title === 'string' && row.issue_title.length > 0
        ? row.issue_title
        : null,
    // Priority was added in migration 33 (#2899). Pre-migration rows
    // return null from pg, which the UI renders as an em-dash.
    priority: typeof row.priority === 'string' ? row.priority : null,
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

/**
 * Read ``dispatcher.config.cap_flipped_by`` — the diagnostic trail for
 * the last `concurrency_cap` flip. Returns the raw string (e.g.
 * `"circuit_breaker"`) or null when the row is unset or contains JSON
 * `null`. Used by the admin cockpit to render the circuit breaker's
 * open banner (#2860).
 */
async function queryCapFlippedBy(pool: Pool): Promise<string | null> {
  const { rows } = await pool.query<{ value: unknown }>(
    `SELECT value FROM dispatcher.config WHERE key = 'cap_flipped_by'`,
  );
  if (rows.length === 0) return null;
  const raw = rows[0].value;
  if (raw === null || raw === undefined) return null;
  // pg's jsonb parser unwraps scalars: JSON `"circuit_breaker"` → the
  // JS string `'circuit_breaker'`; JSON `null` → JS `null`.
  if (typeof raw === 'string') return raw;
  return null;
}

async function queryQueueDepth(pool: Pool): Promise<number> {
  // Phase 2: return the most recent row's `queue_depth` from
  // `dispatcher.queue_snapshots` (written by the daemon on each 30s
  // scheduler tick — see `scripts/dispatcher/daemon.py` and migration
  // `24_dispatcher-queue-snapshots.sql`).
  //
  // Fall back to 0 when:
  //   - The daemon has never booted (no snapshots yet).
  //   - The table exists but is empty (initial deploy before the first
  //     successful scan).
  //   - The daemon's GitHub API calls have failed for the full recent
  //     history (transient — the next successful scan writes a row).
  //
  // Returning 0 keeps the GraphQL contract (`queueDepth: Int!`) honored.
  const { rows } = await pool.query<{ queue_depth: number | string }>(
    `SELECT queue_depth
       FROM dispatcher.queue_snapshots
       ORDER BY observed_at DESC
       LIMIT 1`,
  );
  if (rows.length === 0) return 0;
  const raw = rows[0].queue_depth;
  return typeof raw === 'number' ? raw : Number(raw);
}

async function queryLatestQueueSnapshotIssuesJson(
  pool: Pool,
): Promise<SnapshotIssueRecord[]> {
  const { rows } = await pool.query<{ issues_json: unknown }>(
    `SELECT issues_json
       FROM dispatcher.queue_snapshots
       ORDER BY observed_at DESC
       LIMIT 1`,
  );
  if (rows.length === 0) return [];
  return normalizeSnapshotJson(rows[0].issues_json);
}

async function queryLatestBlockedSnapshotIssuesJson(
  pool: Pool,
): Promise<SnapshotIssueRecord[]> {
  const { rows } = await pool.query<{ issues_json: unknown }>(
    `SELECT issues_json
       FROM dispatcher.blocked_snapshots
       ORDER BY observed_at DESC
       LIMIT 1`,
  );
  if (rows.length === 0) return [];
  return normalizeSnapshotJson(rows[0].issues_json);
}

/**
 * Accept raw jsonb from psycopg's node equivalent — node-postgres
 * returns parsed objects for jsonb columns by default, but be
 * defensive in case a future pool config serializes as a string.
 */
function normalizeSnapshotJson(raw: unknown): SnapshotIssueRecord[] {
  let value: unknown = raw;
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(value)) return [];
  const result: SnapshotIssueRecord[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object') continue;
    const record = entry as Record<string, unknown>;
    const number = record.number;
    if (typeof number !== 'number') continue;
    const title = typeof record.title === 'string' ? record.title : '';
    const createdAt =
      typeof record.createdAt === 'string' ? record.createdAt : '';
    const labelsRaw = Array.isArray(record.labels) ? record.labels : [];
    const labels = labelsRaw.filter((l): l is string => typeof l === 'string');
    const body =
      typeof record.body === 'string'
        ? record.body
        : record.body === null
          ? null
          : undefined;
    const out: SnapshotIssueRecord = { number, title, labels, createdAt };
    if (body !== undefined) out.body = body;
    result.push(out);
  }
  return result;
}

async function queryRecentCompletions(pool: Pool, limit: number): Promise<Row[]> {
  // Terminal-status filter includes the correct-outcome triage states
  // `plan_blocked` (#2857) and `needs_review` (#2856) so the admin
  // cockpit's "Recently completed" panel surfaces them alongside the
  // genuine failures and successes. Additional correct-outcome states
  // slot into this IN list without a schema migration —
  // `dispatcher.agents.status` is plain text (migration 25, no CHECK
  // constraint).
  //
  // #2869: LEFT JOIN the per-agent token + cost rollup from
  // `dispatcher.phase_outputs` so the admin cockpit can render a cost
  // footnote on each row without an N+1 query. SUM across attempts and
  // phases — the row is one "finished agent", not one phase. Rows that
  // predate migration 31 have NULL metering columns, so SUM returns
  // NULL (not 0), which the UI renders as "no cost data" rather than a
  // misleading "$0.00".
  // #2900: select `a.failure_summary` so the admin cockpit can render
  // a one-line "what happened" tooltip on hover over the outcome glyph
  // for failure terminals. Always nullable — succeeded / needs_review
  // rows don't populate it, nor do historical rows from before
  // migration 33.
  const { rows } = await pool.query<Row>(
    `SELECT a.agent_id,
            a.issue_number,
            a.issue_title,
            a.priority,
            a.status,
            a.ended_at,
            a.pr_number,
            a.failure_summary,
            po.total_tokens,
            po.total_cost_usd
       FROM dispatcher.agents a
       LEFT JOIN (
         SELECT agent_id,
                SUM(COALESCE(tokens_input, 0) + COALESCE(tokens_output, 0)) AS total_tokens,
                SUM(cost_usd) AS total_cost_usd
           FROM dispatcher.phase_outputs
          GROUP BY agent_id
       ) po ON po.agent_id = a.agent_id
      WHERE a.status IN ('succeeded', 'failed', 'crashed', 'plan_blocked', 'needs_review')
        AND a.ended_at IS NOT NULL
      ORDER BY a.ended_at DESC
      LIMIT $1`,
    [limit],
  );
  return rows;
}

async function queryConfig(pool: Pool): Promise<Row[]> {
  const { rows } = await pool.query<Row>(
    `SELECT key, value, updated_at, updated_by
       FROM dispatcher.config
      ORDER BY key ASC`,
  );
  return rows;
}

function configRowToGraphQL(row: Row): Record<string, unknown> {
  // `value` is jsonb; serialize back to a JSON-encoded string so the
  // client always sees the same on-wire shape (e.g. `"1"` for a number,
  // `"\"on\""` for a string, `"[60,300]"` for an array).
  return {
    key: row.key,
    value: JSON.stringify(row.value ?? null),
    updatedAt: row.updated_at,
    updatedBy: row.updated_by,
  };
}

/**
 * Fetch up to `limit` queue-ready QueueItem rows. Reads directly from
 * ``dispatcher.queue_snapshots.issues_json`` — the daemon persists
 * title/labels/createdAt for every issue it observes on each 30s
 * scan (issue #2820). **No GitHub API calls from the API container**
 * — the operator constraint is that the API must not have the
 * ability to burn the shared PAT budget (see the deleted
 * ``github.ts`` and the removed ``GITHUB_TOKEN`` secret from the API
 * task-def).
 *
 * Graceful degradation: when the snapshot is empty (fresh deploy
 * before the first daemon scan, or the daemon is down), the admin
 * page renders an empty queue — which is the correct UX, because we
 * really don't know the queue state.
 */
async function queryQueueReady(
  pool: Pool,
  limit: number,
): Promise<Array<Record<string, unknown>>> {
  const issues = await queryLatestQueueSnapshotIssuesJson(pool);
  if (issues.length === 0) return [];
  return sortAndSliceQueueReady(issues, limit).map((issue) =>
    queueItemFromSnapshot(issue, /* includeBlockedBy */ false),
  );
}

/**
 * Sort the enriched `issues` snapshot by `(priorityRank, createdAt ASC)`
 * and return the first `limit` entries. Extracted so unit tests can
 * exercise the ordering directly without a DB fixture (issue #2843).
 *
 * The daemon's queue-scan stores issues in gh CREATED_AT DESC order,
 * which does NOT match the order the daemon actually picks them in
 * (post-#2835 the daemon sorts by priority). The admin page's "top 10"
 * must match the daemon's pick order so the operator sees what's
 * coming up next — otherwise a p0 sitting at position 23-by-createdAt
 * never appears, even though it's the next pick.
 *
 * Uses a copied array + stable-by-construction comparator — we do not
 * mutate the input. Within a priority bucket the secondary key is
 * `createdAt` ASC (older first), matching the Python daemon's
 * ``key=(_priority_rank(labels), createdAt or "")`` at
 * ``scripts/dispatcher/daemon.py:1708-1713``.
 */
function sortAndSliceQueueReady(
  issues: readonly SnapshotIssueRecord[],
  limit: number,
): SnapshotIssueRecord[] {
  const copy = issues.slice();
  copy.sort((a, b) => {
    const ra = priorityRank(a.labels);
    const rb = priorityRank(b.labels);
    if (ra !== rb) return ra - rb;
    // Secondary key: createdAt ASC. Empty/missing createdAt sorts last
    // within its priority bucket (same convention as the daemon).
    const ca = a.createdAt || '';
    const cb = b.createdAt || '';
    if (ca === cb) return 0;
    if (ca === '') return 1;
    if (cb === '') return -1;
    return ca < cb ? -1 : 1;
  });
  return copy.slice(0, limit);
}

/**
 * Fetch up to `limit` queueBlocked QueueItem rows from
 * ``dispatcher.blocked_snapshots.issues_json`` (daemon-populated, no
 * GitHub call; issue #2820). Blocked issues include ``body`` so
 * ``parseBlockedBy`` can populate the inline blocker list.
 */
async function queryQueueBlocked(
  pool: Pool,
  limit: number,
): Promise<Array<Record<string, unknown>>> {
  const issues = await queryLatestBlockedSnapshotIssuesJson(pool);
  const result = issues.map((issue) =>
    queueItemFromSnapshot(issue, /* includeBlockedBy */ true),
  );
  // Sort newest first per spec.
  result.sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
  return result.slice(0, limit);
}

function queueItemFromSnapshot(
  issue: SnapshotIssueRecord,
  includeBlockedBy: boolean,
): Record<string, unknown> {
  return {
    issueNumber: issue.number,
    title: issue.title,
    priority: extractPriority(issue.labels),
    labels: issue.labels,
    createdAt: issue.createdAt,
    blockedBy: includeBlockedBy ? parseBlockedBy(issue.body) : [],
  };
}

function recentCompletionsToGraphQL(
  rows: readonly Row[],
): Array<Record<string, unknown>> {
  return rows.map((row) => {
    const number =
      typeof row.issue_number === 'number' ? row.issue_number : null;
    const title =
      typeof row.issue_title === 'string' && row.issue_title.length > 0
        ? row.issue_title
        : null;
    // #2900: failure_summary is TEXT nullable — any non-string or empty
    // value collapses to null so the UI renders the outcome glyph
    // exactly as it did pre-#2900 (no tooltip layering over the
    // built-in status label). Trimming matches the daemon's write-time
    // convention and handles the edge case where a migration-era empty
    // string sneaks through.
    const failureSummary =
      typeof row.failure_summary === 'string' && row.failure_summary.trim()
        ? row.failure_summary.trim()
        : null;
    return {
      agentId: row.agent_id,
      issueNumber: number,
      // Priority pulled from ``dispatcher.agents.priority`` (populated
      // by the daemon at claim time from the queue-snapshot labels;
      // issue #2899). Pre-migration-33 rows return null, which the UI
      // renders as an em-dash placeholder.
      priority: typeof row.priority === 'string' ? row.priority : null,
      // Pulled from ``dispatcher.agents.issue_title`` (populated by the
      // daemon at claim time from the queue-snapshot enrichment; issue
      // #2820). Agents that claimed before migration 28 will have
      // ``issue_title = NULL`` and the admin-page UI falls back to
      // ``#<number>``.
      issueTitle: title,
      status: row.status,
      endedAt: row.ended_at,
      prNumber: row.pr_number ?? null,
      // #2869: metering fields. Coerced to Number because pg returns
      // numeric-type columns as strings on some platforms; null stays
      // null so the UI renders "no data" instead of "$0.00".
      totalTokens: coerceNullableNumber(row.total_tokens),
      totalCostUsd: coerceNullableNumber(row.total_cost_usd),
      // #2900: one-line "what happened" string for failure terminals,
      // rendered as a tooltip in the admin cockpit's Recently
      // completed panel. Null for succeeded / needs_review / historical
      // rows — the UI falls back to the default status-label tooltip.
      failureSummary,
    };
  });
}

/** Coerce a pg numeric/bigint column to a JS Number or null.
 *
 * pg's default mapping returns ``numeric`` as a string (to preserve
 * arbitrary precision) and ``bigint`` as a string on platforms where
 * Number would lose precision. The dispatcher metering columns are
 * always small enough for a Number (token counts fit in 53 bits of
 * precision well under any plausible phase usage), so we coerce.
 * Null / undefined / NaN pass through as null — the GraphQL schema
 * keeps every metering field nullable exactly for this "no signal"
 * case. Issue #2869.
 */
function coerceNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === 'string' || typeof value === 'bigint') {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
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
// Data access — dispatcherSetConfig mutation (live-edit config + audit)
// ---------------------------------------------------------------------------

/**
 * Server-side validation for `dispatcher.config` edits (#2805 §1.6). The
 * admin UI restricts the inputs it shows, but we also enforce the
 * constraints here so an unauthorised (but admin) bulk update can't
 * corrupt the daemon. Return value is the parsed (JSON-native) value
 * that will be written into the jsonb column. Throws a GraphQLError on
 * invalid input.
 */
export function validateConfigValue(key: string, raw: unknown): unknown {
  switch (key) {
    case 'concurrency_cap': {
      const n = typeof raw === 'number' ? raw : Number.NaN;
      if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0 || n > 5) {
        throw new GraphQLError(
          'concurrency_cap must be an integer in [0, 5]',
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }
      return n;
    }
    case 'subprocess_timeout_s':
    case 'idle_audit_every_n_prs': {
      const n = typeof raw === 'number' ? raw : Number.NaN;
      if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0) {
        throw new GraphQLError(
          `${key} must be a non-negative integer`,
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }
      return n;
    }
    case 'backoff_seconds': {
      if (
        !Array.isArray(raw) ||
        !raw.every((v) => typeof v === 'number' && Number.isInteger(v) && v >= 0)
      ) {
        throw new GraphQLError(
          'backoff_seconds must be an array of non-negative integers',
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }
      return raw;
    }
    case 'idle_spotcheck_cron': {
      if (typeof raw !== 'string' || raw.length === 0) {
        throw new GraphQLError(
          'idle_spotcheck_cron must be a non-empty string',
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }
      return raw;
    }
    case 'runner_by_phase':
    case 'model_by_phase':
    case 'runner_shadow': {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new GraphQLError(
          `${key} must be a JSON object`,
          { extensions: { code: 'BAD_USER_INPUT' } },
        );
      }
      return raw;
    }
    default:
      // Unknown keys are rejected — operators should not be able to
      // freehand new config entries through the admin page.
      throw new GraphQLError(`Unknown config key: ${key}`, {
        extensions: { code: 'BAD_USER_INPUT' },
      });
  }
}

async function updateConfigEntry(
  pool: Pool,
  key: string,
  value: unknown,
  updatedBy: string,
): Promise<Row | null> {
  const { rows } = await pool.query<Row>(
    `UPDATE dispatcher.config
        SET value = $2::jsonb,
            updated_at = NOW(),
            updated_by = $3
      WHERE key = $1
    RETURNING key, value, updated_at, updated_by`,
    [key, JSON.stringify(value), updatedBy],
  );
  return rows[0] ?? null;
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
      const [currentRunRow, spawnFrozenUntil, capFlippedBy] = await Promise.all([
        queryCurrentRun(pool),
        querySpawnFrozenUntil(pool),
        queryCapFlippedBy(pool),
      ]);
      return {
        // Nested fields defer to DispatcherState field resolvers.
        __currentRun: currentRunRow,
        __spawnFrozenUntil: spawnFrozenUntil,
        __capFlippedBy: capFlippedBy,
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
      { pool, user }: DispatcherContext,
    ) => {
      const admin = requireDispatcherAdmin(user);

      // #2884: the MFA re-auth placeholder was removed — admin session
      // auth is sufficient, and the "destructive vs not" gate added pure
      // friction with zero real safety (any non-empty X-MFA-Token
      // header was accepted). Audit trail lives in
      // `dispatcher.commands.issued_by`.

      const effectivePayload = payload ?? {};
      const { row, created } = await insertCommandIdempotent(
        pool,
        command,
        admin.email,
        effectivePayload,
      );
      return commandRowToGraphQL(row, created);
    },

    dispatcherSetConfig: async (
      _: unknown,
      { key, value }: { key: string; value: string },
      { pool, user }: DispatcherContext,
    ) => {
      const admin = requireDispatcherAdmin(user);

      // #2884: MFA re-auth placeholder removed (see dispatcherControl
      // above). Config edits still go through `requireDispatcherAdmin`
      // and `validateConfigValue`.

      let parsed: unknown;
      try {
        parsed = JSON.parse(value);
      } catch {
        throw new GraphQLError('value must be a JSON-encoded string', {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }

      const validated = validateConfigValue(key, parsed);

      const row = await updateConfigEntry(pool, key, validated, admin.email);
      if (!row) {
        throw new GraphQLError(`Unknown config key: ${key}`, {
          extensions: { code: 'BAD_USER_INPUT' },
        });
      }
      return configRowToGraphQL(row);
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

    queueReady: async (_: unknown, __: unknown, { pool, user }: DispatcherContext) => {
      requireDispatcherAdmin(user);
      return queryQueueReady(pool, 10);
    },

    queueBlocked: async (_: unknown, __: unknown, { pool, user }: DispatcherContext) => {
      requireDispatcherAdmin(user);
      return queryQueueBlocked(pool, 10);
    },

    recentCompletions: async (
      _: unknown,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const rows = await queryRecentCompletions(pool, 10);
      return recentCompletionsToGraphQL(rows);
    },

    config: async (_: unknown, __: unknown, { pool, user }: DispatcherContext) => {
      requireDispatcherAdmin(user);
      const rows = await queryConfig(pool);
      return rows.map(configRowToGraphQL);
    },

    spawnFrozenUntil: (parent: Record<string, unknown>) => {
      return (parent.__spawnFrozenUntil as string | null) ?? null;
    },

    capFlippedBy: (parent: Record<string, unknown>) => {
      return (parent.__capFlippedBy as string | null) ?? null;
    },

    /**
     * True when the circuit breaker is open (#2860). Derived from
     * `cap_flipped_by === 'circuit_breaker'` so the admin cockpit can
     * show a banner without parsing the raw config value client-side.
     */
    circuitBreakerOpen: (parent: Record<string, unknown>) => {
      return parent.__capFlippedBy === 'circuit_breaker';
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

    // #2869: per-agent token + cost aggregates. Four fields share a
    // single per-phase SELECT via DataLoader-style caching on the
    // parent object — the first field to resolve primes
    // `__phase_cost_rows` on the parent so subsequent sibling fields
    // reuse the same query result instead of issuing three more
    // SELECTs. dispatcherAgent() is a single-object query so this
    // optimization only matters for the one-field-per-render paths,
    // but it keeps the admin cockpit cheap when polling.
    totalTokensInput: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const rows = await _memoPhaseCostRows(pool, parent);
      return sumPhaseCost(rows, 'tokens_input');
    },

    totalTokensOutput: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const rows = await _memoPhaseCostRows(pool, parent);
      return sumPhaseCost(rows, 'tokens_output');
    },

    totalCostUsd: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const rows = await _memoPhaseCostRows(pool, parent);
      return sumPhaseCost(rows, 'cost_usd');
    },

    phaseCostBreakdown: async (
      parent: Record<string, unknown>,
      __: unknown,
      { pool, user }: DispatcherContext,
    ) => {
      requireDispatcherAdmin(user);
      const rows = await _memoPhaseCostRows(pool, parent);
      return phaseCostRowsToGraphQL(rows);
    },
  },
};

async function _memoPhaseCostRows(
  pool: Pool,
  parent: Record<string, unknown>,
): Promise<Row[]> {
  const cached = parent.__phase_cost_rows;
  if (Array.isArray(cached)) return cached as Row[];
  const agentId = parent.__agent_id ?? parent.id;
  const rows = await queryPhaseCostBreakdown(pool, String(agentId));
  parent.__phase_cost_rows = rows;
  return rows;
}

async function queryPhaseCostBreakdown(
  pool: Pool,
  agentId: string,
): Promise<Row[]> {
  // #2869: one row per phase, summed across retry attempts. The
  // "latest model_used per phase" picks the most recent attempt
  // deterministically via DISTINCT ON — retries that swap models
  // (model_override operator knob) report the model that actually
  // produced the final result.
  const { rows } = await pool.query<Row>(
    `SELECT po.phase,
            SUM(po.tokens_input)       AS tokens_input,
            SUM(po.tokens_output)      AS tokens_output,
            SUM(po.tokens_cache_read)  AS tokens_cache_read,
            SUM(po.tokens_cache_write) AS tokens_cache_write,
            SUM(po.cost_usd)           AS cost_usd,
            (SELECT model_used
               FROM dispatcher.phase_outputs po2
              WHERE po2.agent_id = po.agent_id
                AND po2.phase    = po.phase
                AND po2.model_used IS NOT NULL
              ORDER BY po2.ts DESC
              LIMIT 1) AS model_used
       FROM dispatcher.phase_outputs po
      WHERE po.agent_id = $1
      GROUP BY po.agent_id, po.phase
      ORDER BY MIN(po.ts) ASC`,
    [agentId],
  );
  return rows;
}

function phaseCostRowsToGraphQL(
  rows: readonly Row[],
): Array<Record<string, unknown>> {
  return rows.map((row) => ({
    phase: row.phase,
    tokensInput: coerceNullableNumber(row.tokens_input),
    tokensOutput: coerceNullableNumber(row.tokens_output),
    tokensCacheRead: coerceNullableNumber(row.tokens_cache_read),
    tokensCacheWrite: coerceNullableNumber(row.tokens_cache_write),
    costUsd: coerceNullableNumber(row.cost_usd),
    modelUsed: typeof row.model_used === 'string' ? row.model_used : null,
  }));
}

/** Sum a single nullable-numeric column across phase rows.
 *
 * Returns null when every row's value is null — the "no metering
 * signal" case for an agent whose phases all ran before migration 31
 * or all failed before producing a JSON envelope. Returns a Number
 * (possibly 0) when at least one row has a non-null value, which the
 * UI renders literally. Issue #2869.
 */
function sumPhaseCost(
  rows: readonly Row[],
  column: 'tokens_input' | 'tokens_output' | 'cost_usd',
): number | null {
  let total = 0;
  let anyPresent = false;
  for (const row of rows) {
    const v = coerceNullableNumber(row[column]);
    if (v === null) continue;
    total += v;
    anyPresent = true;
  }
  return anyPresent ? total : null;
}

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
  coerceNullableNumber,
  commandRowToGraphQL,
  configRowToGraphQL,
  failureRowToGraphQL,
  insertCommandIdempotent,
  normalizeSnapshotJson,
  phaseCostRowsToGraphQL,
  queueItemFromSnapshot,
  recentCompletionsToGraphQL,
  runRowToGraphQL,
  sortAndSliceQueueReady,
  sumPhaseCost,
  transitionRowToGraphQL,
  updateConfigEntry,
};

export type { SnapshotIssueRecord };
