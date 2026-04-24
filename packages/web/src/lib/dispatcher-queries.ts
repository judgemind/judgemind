/**
 * GraphQL queries, mutations, and result types for the /admin/dispatcher page.
 *
 * Backed by the dispatcher admin GraphQL surface landed in #2730
 * (`packages/api/src/graphql/dispatcher/`). Non-admins receive a generic
 * "not found" error from the resolver (see `dispatcher/auth.ts`) — the page
 * handles that gracefully without leaking the route's existence.
 *
 * Apollo `keyFields` for these types live in `apollo-client.ts`
 * (`DispatcherRun`, `DispatcherFailure`, `PhaseTransition`,
 * `DispatcherCommandResult`, and `keyFields: false` for `DispatcherState`).
 */
import { gql } from '@apollo/client';

export const DISPATCHER_STATE_QUERY = gql`
  query DispatcherState {
    dispatcherState {
      currentRun {
        runId
        startedAt
        stoppedAt
        heartbeatTs
        versionSha
        host
        pid
      }
      activeAgents {
        id
        issueNumber
        issueTitle
        priority
        worktreePath
        phase
        status
        startedAt
        endedAt
        exitCode
        prNumber
        retriesUsed
      }
      recentFailures(sinceHours: 24) {
        failureId
        agentId
        category
        displayCategory
        detectedBy
        details
        ts
        issueNumber
      }
      queueDepth
      blockedDepth
      queueReady {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy
        cooldownSecondsRemaining
      }
      queueBlocked {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy
      }
      recentCompletions {
        agentId
        issueNumber
        issueTitle
        priority
        status
        endedAt
        prNumber
        totalTokens
        totalCostUsd
        failureSummary
        mergedAt
        verifiedAt
        verifySkipReason
        retroedAt
      }
      config {
        key
        value
        updatedAt
        updatedBy
      }
      spawnFrozenUntil
      circuitBreakerOpen
      capFlippedBy
    }
  }
`;

/**
 * Full-list payload for the cockpit's expand-count dialogs (issue #3159).
 * Fired only on dialog open — NOT in the 2s `dispatcherState` poll path.
 * The server resolves this from the same snapshot tables as the capped
 * `DispatcherState` panels (`dispatcher.queue_snapshots` /
 * `dispatcher.blocked_snapshots` / `dispatcher.agents`), so opening the
 * dialog adds no GitHub API traffic.
 *
 * `kind` arg is required; the response echoes it back so Apollo can
 * normalize the cache by bucket — see `apollo-client.ts` for the
 * `keyFields: ['kind']` entry.
 */
export const DISPATCHER_QUEUE_FULL_QUERY = gql`
  query DispatcherQueueFull($kind: DispatcherQueueKind!) {
    dispatcherQueueFull(kind: $kind) {
      kind
      queueItems {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy
        cooldownSecondsRemaining
      }
      completions {
        agentId
        issueNumber
        issueTitle
        priority
        status
        endedAt
        prNumber
        totalTokens
        totalCostUsd
        failureSummary
        mergedAt
        verifiedAt
        verifySkipReason
        retroedAt
      }
    }
  }
`;

export const DISPATCHER_CONTROL_MUTATION = gql`
  mutation DispatcherControl($command: DispatcherCommand!, $payload: JSON) {
    dispatcherControl(command: $command, payload: $payload) {
      commandId
      command
      issuedBy
      issuedAt
      consumedAt
      payload
      created
    }
  }
`;

export const DISPATCHER_SET_CONFIG_MUTATION = gql`
  mutation DispatcherSetConfig($key: String!, $value: String!) {
    dispatcherSetConfig(key: $key, value: $value) {
      key
      value
      updatedAt
      updatedBy
    }
  }
`;

// ---------------------------------------------------------------------------
// Result types — shapes mirror the dispatcher GraphQL schema in
// packages/api/src/graphql/dispatcher/schema.ts.
// ---------------------------------------------------------------------------

export interface DispatcherRun {
  runId: string;
  startedAt: string;
  stoppedAt: string | null;
  heartbeatTs: string;
  versionSha: string;
  host: string;
  pid: number;
}

export interface DispatcherAgent {
  id: string;
  issueNumber: number;
  /** Issue title captured at claim time from the queue-snapshot
   * enrichment (#2820). Null for pre-migration-28 rows or when the
   * issue was not in the snapshot at claim time — the UI renders
   * `#<number>` alone in that case. Exposed here so the active-agents
   * table can render a shared `(issue, priority, title)` prefix
   * matching the queue and recent-completions panels (#2899). */
  issueTitle: string | null;
  /** Priority label captured at claim time — `p0` | `p1` | `p2` | `p3`
   * | null. Null for pre-migration-33 rows or issues with no
   * `priority/pN` label at claim time; the UI renders an em-dash
   * placeholder. Reflects "priority when spawned", not "priority now".
   * Issue #2899. */
  priority: string | null;
  worktreePath: string;
  phase: string;
  status: string;
  startedAt: string;
  endedAt: string | null;
  exitCode: number | null;
  prNumber: number | null;
  retriesUsed: number;
}

export interface DispatcherFailure {
  failureId: string;
  agentId: string | null;
  /** Stored machine-readable category token (e.g.
   * `subprocess_turn_limit`, `daemon_restart_abandoned`). Used by
   * CloudWatch queries, SQL filters, and retry classification. The
   * admin cockpit surfaces this via the table cell's `title` tooltip
   * so operators can still copy-paste the raw token for debugging
   * even when the cell renders `displayCategory`. */
  category: string;
  /** Operator-friendly rephrasing of `category` computed server-side
   * from the display-name map in the API resolver, which mirrors
   * `_CATEGORY_DISPLAY_NAMES` in `scripts/dispatcher/daemon.py`.
   * Categories not in the map fall through to the raw token. Issue
   * #2948 — keeps Recent Failures table labels consistent with the
   * Recently Completed tooltips introduced in #2935. */
  displayCategory: string;
  detectedBy: string;
  details: Record<string, unknown>;
  ts: string;
  issueNumber: number | null;
}

export interface QueueItem {
  issueNumber: number;
  title: string;
  priority: string | null;
  labels: string[];
  createdAt: string;
  blockedBy: number[];
  /** Seconds left in the post-failure cooldown window. Null when no prior
   * attempt exists (never attempted) or when cooldown has elapsed.
   * Positive when the issue is still cooling down after a recent failure.
   * Issue #3001. */
  cooldownSecondsRemaining: number | null;
}

export interface RecentCompletion {
  agentId: string;
  issueNumber: number;
  issueTitle: string | null;
  /** Priority label captured at claim time — `p0` | `p1` | `p2` | `p3`
   * | null. Same semantics as `DispatcherAgent.priority`; pre-migration-33
   * rows return null (em-dash in the UI). Issue #2899. */
  priority: string | null;
  /** One of `succeeded | failed | crashed`. */
  status: string;
  endedAt: string;
  prNumber: number | null;
  /** Sum of input+output tokens across every phase the agent ran
   * (#2869). Null when no phase row has recorded usage — rendered as
   * "no cost data" rather than a misleading 0. */
  totalTokens: number | null;
  /** Sum of `cost_usd` across every phase the agent ran (#2869). Null
   * when no phase row has recorded usage.
   *
   * WARNING: list-price estimate, NOT Max plan-adjusted. */
  totalCostUsd: number | null;
  /** One-line "what happened" string for failure terminals (`failed`,
   * `crashed`, `plan_blocked`). Populated at terminal-time by the
   * daemon from `failures.category` + `phase` + stderr tail, then
   * optionally upgraded by `/diagnose-failure`. Capped at 240 chars.
   *
   * Null for `succeeded` / `needs_review` rows and for historical rows
   * that pre-date migration 33. Rendered as a tooltip on the outcome
   * glyph in the `Recently completed` panel — no always-visible second
   * line. Issue #2900. */
  failureSummary: string | null;
  /** Timestamp the PR squash-merge was observed by the daemon. Paired
   * with the `status='succeeded'` flip at merge time — `mergedAt != null`
   * is the canonical "shipped" signal. Null on rows that never merged
   * (push failed, CI red after retries, etc.) and on pre-migration-35
   * historical rows. Issue #2953. */
  mergedAt: string | null;
  /** Timestamp the verify phase completed with verdict=VERIFIED. Null
   * when verify was intentionally skipped (see `verifySkipReason`),
   * when verify crashed mid-phase, or for pre-migration-35 rows.
   * Issue #2953. */
  verifiedAt: string | null;
  /** Non-null when verify was intentionally skipped. Today the only
   * written value is `"self_deploy"` (dispatcher-self-PR touches
   * `scripts/dispatcher/`). A merged row with a non-null skip reason
   * counts as fully-shipped (green ✓) — skipping is not a regression.
   * Issue #2953. */
  verifySkipReason: string | null;
  /** Timestamp the retro phase completed (reached PHASE_RETRO_DONE).
   * Null when retro crashed, when the worktree was already gone at
   * retro time, or for pre-migration-35 rows. Issue #2953. */
  retroedAt: string | null;
}

export interface DispatcherConfigEntry {
  key: string;
  /** JSON-encoded string — e.g. `"1"`, `"\"on\""`, `"[60,300]"`. */
  value: string;
  updatedAt: string;
  updatedBy: string;
}

export interface DispatcherState {
  currentRun: DispatcherRun | null;
  activeAgents: DispatcherAgent[];
  recentFailures: DispatcherFailure[];
  queueDepth: number;
  /** Total count of open `status/blocked` issues on the most recent
   * daemon blocked-scan tick. Paired with `queueBlocked` (capped at 10
   * server-side) so the admin-cockpit panel header can render
   * `{shown} / {total}` without losing the tail (issue #2886). Returns
   * 0 before any blocked scan has landed — same fall-back-to-0 contract
   * as `queueDepth`. */
  blockedDepth: number;
  queueReady: QueueItem[];
  queueBlocked: QueueItem[];
  recentCompletions: RecentCompletion[];
  config: DispatcherConfigEntry[];
  spawnFrozenUntil: string | null;
  /** True when the circuit breaker is open (#2860). */
  circuitBreakerOpen: boolean;
  /** Diagnostic trail for the last `concurrency_cap` flip (#2860). */
  capFlippedBy: string | null;
}

export interface DispatcherSetConfigData {
  dispatcherSetConfig: DispatcherConfigEntry;
}

export interface DispatcherStateData {
  dispatcherState: DispatcherState;
}

/** Control commands accepted by `dispatcherControl` (#2884 simplified).
 *
 * Mirrors the `DispatcherCommand` enum in the API schema. Three global
 * commands (`start` / `stop` / `force_stop`) plus one per-agent command
 * (`retry`). The former `pause`, `resume`, `drain`, and `force_kill`
 * commands were removed — `stop` now carries the former `drain`
 * semantic (graceful) and `force_stop` replaces both the former `stop`
 * (immediate) and the per-agent `force_kill` (when `payload.agentId`
 * is supplied).
 *
 * `force_stop` is the only command the admin page confirms via modal
 * (immediate abort is destructive). `stop` is graceful — in-flight
 * agents finish their current phase — so no confirmation is needed.
 */
export type DispatcherCommand = 'start' | 'stop' | 'force_stop' | 'retry';

export interface DispatcherCommandResult {
  commandId: string;
  command: DispatcherCommand;
  issuedBy: string;
  issuedAt: string;
  consumedAt: string | null;
  payload: Record<string, unknown>;
  created: boolean;
}

export interface DispatcherControlData {
  dispatcherControl: DispatcherCommandResult;
}

/**
 * Bucket selector for `dispatcherQueueFull` (issue #3159). Mirrors the
 * `DispatcherQueueKind` enum in the API schema.
 */
export type DispatcherQueueKind = 'READY' | 'BLOCKED' | 'COMPLETED';

/**
 * Result type for `dispatcherQueueFull` (issue #3159). Exactly one of
 * `queueItems` (READY / BLOCKED) or `completions` (COMPLETED) is
 * populated per request — the other is empty.
 */
export interface DispatcherQueueFull {
  kind: DispatcherQueueKind;
  queueItems: QueueItem[];
  completions: RecentCompletion[];
}

export interface DispatcherQueueFullData {
  dispatcherQueueFull: DispatcherQueueFull;
}
