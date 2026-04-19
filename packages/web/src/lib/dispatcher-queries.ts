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
      spawnFrozenUntil
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

export interface DispatcherState {
  currentRun: DispatcherRun | null;
  activeAgents: DispatcherAgent[];
  recentFailures: DispatcherFailure[];
  queueDepth: number;
  spawnFrozenUntil: string | null;
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
