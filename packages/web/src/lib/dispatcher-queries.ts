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
        kind
        issueNumber
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
        detectedBy
        details
        ts
        issueNumber
      }
      queueDepth
      queueReady {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy
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
        status
        endedAt
        prNumber
        totalTokens
        totalCostUsd
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
  kind: string;
  issueNumber: number;
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
  category: string;
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
}

export interface RecentCompletion {
  agentId: string;
  issueNumber: number;
  issueTitle: string | null;
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
  queueReady: QueueItem[];
  queueBlocked: QueueItem[];
  recentCompletions: RecentCompletion[];
  config: DispatcherConfigEntry[];
  spawnFrozenUntil: string | null;
  /** True when the overnight-safety circuit breaker is open (#2860). */
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

/** Control commands accepted by `dispatcherControl`.
 *
 * Mirrors the `DispatcherCommand` enum in the API schema. The subset marked
 * "destructive" in the server's `DESTRUCTIVE_COMMANDS` set triggers a
 * confirmation modal in the UI before invoking the mutation.
 */
export type DispatcherCommand =
  | 'start'
  | 'stop'
  | 'drain'
  | 'pause'
  | 'resume'
  | 'retry'
  | 'force_kill';

/** Commands that mutate in-flight agent state. Matches the server-side
 * `DESTRUCTIVE_COMMANDS` set in `packages/api/src/graphql/dispatcher/auth.ts`.
 * The admin page always raises a confirmation modal for these. */
export const DESTRUCTIVE_COMMANDS: ReadonlySet<DispatcherCommand> = new Set([
  'stop',
  'drain',
  'force_kill',
]);

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
