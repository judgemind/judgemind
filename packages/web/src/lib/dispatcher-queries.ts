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
 *
 * Polled-query complexity contract (issue #4062 — followup to the #4003
 * GraphQL DoS hardening that capped per-query cost at 1000):
 * `DISPATCHER_STATE_QUERY` is fired every 2s by the cockpit, so it must
 * stay well below the 1000-cost cap. The biggest amplifier in this query
 * shape is list-of-objects-containing-a-list — the polled `blockedBy`
 * selections inside `queueReady` and `queueBlocked` therefore request
 * ONLY `number`. Blocker titles are populated only when the operator
 * opens the expand-on-click full-list dialog, which uses the separate
 * `DISPATCHER_QUEUE_FULL_QUERY` below (fires once per dialog open, not
 * polled). The render layer's `formatBlockerTooltip` already falls back
 * to `#N` alone when `BlockerRef.title` is null/undefined, so the polled
 * tooltip degrades gracefully — full titles still appear in the dialog.
 *
 * Polled-query slim — `config` (issue #4063): the dispatcher config rows
 * change rarely (manual operator edits via `dispatcherSetConfig`) and
 * don't need to ride the 2s real-time refresh. They were contributing
 * ~40 cost units to a query that was already over the 1000 cap (#4062).
 * Config now lives in the separate `DISPATCHER_CONFIG_QUERY` below,
 * which fires once on dashboard mount and is refetched explicitly by
 * the `dispatcherSetConfig` mutation's `refetchQueries` so the displayed
 * value stays in sync without a manual page reload.
 *
 * Polled-query slim — `recentCompletions` (issue #4064): the polled
 * `recentCompletions` block originally selected 14 fields; under the
 * 10× list-of-objects multiplier this contributed the third-largest
 * chunk of the 1355-unit cost. Trimmed to the eight fields
 * `RecentCompletionRow` actually reads to render the visible row layout
 * — `agentId` (key), the unified `(issue, priority, [PR,] title)`
 * prefix (`issueNumber` / `issueTitle` / `priority` / `prNumber`),
 * the OutcomePill `status` driver, the relative-time cell's `endedAt`,
 * and `failureSummary` (which re-skins the pill to gray ↺ for
 * infra-preempted rows). Detail-only fields (`startedAt` for the
 * Start/Duration tooltip, `totalTokens` / `totalCostUsd` for the cost
 * footnote, and the four milestone columns `mergedAt` / `verifiedAt` /
 * `verifySkipReason` / `retroedAt` that drive the green-vs-amber-✓
 * "shipped but bookkeeping incomplete" pill nuance) ride exclusively
 * on `DISPATCHER_QUEUE_FULL_QUERY` — fired on dialog open, not in the
 * 2s poll path. The polled row degrades gracefully: succeeded rows
 * render plain green ✓ instead of the amber-✓ incomplete variant; the
 * cost footnote and start/duration tooltip are absent. The full
 * milestone breakdown still renders inside the expand-on-click dialog.
 *
 * Polled-query slim — `activeAgents` (issue #4100): the polled
 * `activeAgents` block originally selected 12 fields. After #4062 +
 * #4063 + #4064 landed, the polled query was still 35 units over the
 * #4003 1000-cap (cost: 1035); CloudWatch on `/ecs/judgemind-api-dev`
 * showed every cockpit poll returning HTTP 400 with `Query exceeded
 * complexity`. Per-field breakdown (computed offline from the cost-
 * rule algorithm; the `costBreakdownPlugin` in
 * `packages/api/src/graphql/cost-breakdown.ts` will print this in
 * CloudWatch on each near-cap operation going forward) showed the
 * biggest remaining contributor was `activeAgents`'s 12-field scalar
 * list. Trimmed to the eight fields `ActiveAgentRow` actually reads
 * to render the row — `id` (key + native title for full UUID),
 * `worktreePath` (Logs link href + path-tail fallback), `phase`
 * (chip + tooltip via `dispatcher-phase-flow`), the unified
 * `(issue, priority, title)` prefix (`issueNumber` / `issueTitle` /
 * `priority`), `startedAt` (elapsed-time cell), and `retriesUsed`
 * (final-attempt Opus marker via `dispatcher-ralph-model`). The four
 * detail-only fields (`status`, `endedAt`, `exitCode`, `prNumber`)
 * are not consumed by `ActiveAgentRow` — `status` is implicit
 * ("running" by definition for active agents), `endedAt` and
 * `exitCode` are terminal-only signals (always null on a row that
 * appears in `activeAgents`), and `prNumber` is rare on running
 * agents and the row has no UI affordance for it anyway. Trim saves
 * 4 × 10 = 40 units; combined with the other slims the polled cost
 * is ≤ 995 — back under the 1000-cap with headroom.
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
        startedAt
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
        blockedBy {
          number
        }
        cooldownSecondsRemaining
      }
      queueBlocked {
        issueNumber
        title
        priority
        labels
        createdAt
        blockedBy {
          number
        }
      }
      recentCompletions {
        agentId
        issueNumber
        issueTitle
        priority
        status
        endedAt
        prNumber
        failureSummary
      }
      recentCompletionsCount
      spawnFrozenUntil
      circuitBreakerOpen
      capFlippedBy
    }
  }
`;

/**
 * Dispatcher config rows (issue #4063). Fires once on dashboard mount
 * (NOT in the 2s `dispatcherState` poll path), and is re-fetched by
 * `DISPATCHER_SET_CONFIG_MUTATION` via `refetchQueries` so the displayed
 * value updates without a page reload after an operator edit.
 *
 * Resolves the same `dispatcher.config` snapshot the polled query used
 * to read — no API/schema change. Splitting the selection out of the
 * 2s poll saved ~40 cost units against the #4003 1000-cap.
 */
export const DISPATCHER_CONFIG_QUERY = gql`
  query DispatcherConfig {
    dispatcherState {
      config {
        key
        value
        updatedAt
        updatedBy
      }
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
        blockedBy {
          number
          title
        }
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
  /** Issue number the agent is working on. Nullable for
   * scheduled-skill agents (`/audit`, `/spotcheck`,
   * `/dispatcher-daily-report`) which have no closing GitHub issue —
   * see migration 49 (issue #3381) and bug #3425. The cockpit renders
   * the null case via `IssueLink`'s null branch (em-dash placeholder). */
  issueNumber: number | null;
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
  /** Always `"running"` on rows fetched via `DISPATCHER_STATE_QUERY`
   * (issue #4100): the polled query no longer selects `status` because
   * `activeAgents` is — by SQL definition — only the running agents.
   * Always populated on rows fetched via `DISPATCHER_QUEUE_FULL_QUERY`
   * (active agents are not included in that bucket today, but the
   * field stays for type compatibility with terminal rows). */
  status?: string;
  startedAt: string;
  /** Always null on rows fetched via `DISPATCHER_STATE_QUERY` (issue
   * #4100): an active row by definition has not ended yet; the field
   * carries no information for the polled view, so it was dropped from
   * the polled selection. Populated when fetched via the dialog query. */
  endedAt?: string | null;
  /** Always null on rows fetched via `DISPATCHER_STATE_QUERY` (issue
   * #4100): paired with `endedAt` above — no exit code exists yet on
   * a still-running agent. */
  exitCode?: number | null;
  /** May be `undefined` on rows fetched via `DISPATCHER_STATE_QUERY`
   * (issue #4100): trimmed from the polled selection. PR is rare on
   * running agents (typically only present after summary phase has
   * opened the PR) and `ActiveAgentRow` has no UI affordance for it,
   * so the polled row carries no PR link. The dialog still surfaces
   * it via `DISPATCHER_QUEUE_FULL_QUERY`. */
  prNumber?: number | null;
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

/**
 * A blocker reference — the issue number and optional title of an issue
 * that is blocking a `status/blocked` queue item. Title is populated by the
 * daemon's `_fetch_issue_titles_for_blockers` helper (issue #2989) and may
 * be null when the title fetch failed (404, rate-limit, etc.).
 *
 * Apollo `keyFields: ['number']` is registered in `apollo-client.ts` so
 * Apollo normalises distinct BlockerRef entries correctly.
 *
 * Title selection (issue #4062): only `DISPATCHER_QUEUE_FULL_QUERY`
 * (the expand-on-click dialog) selects `title`. The polled
 * `DISPATCHER_STATE_QUERY` selects only `number` to keep query cost
 * under the 1000-cap from #4003. Render code that consumes
 * `BlockerRef.title` MUST tolerate `undefined` as well as `null` — see
 * `formatBlockerTooltip` in `QueuePanel.tsx` for the canonical fallback
 * (renders `#N` alone when the title is missing).
 */
export interface BlockerRef {
  number: number;
  /** May be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (which selects only `number`). Always either
   * a string or `null` on rows fetched via `DISPATCHER_QUEUE_FULL_QUERY`. */
  title?: string | null;
}

export interface QueueItem {
  issueNumber: number;
  title: string;
  priority: string | null;
  labels: string[];
  createdAt: string | null;
  /** Blocker references for this issue. Issue #2989: now `BlockerRef[]`
   * instead of `number[]`. Each entry carries the blocker issue number
   * and optional title (null when the daemon's title fetch failed).
   * Empty for queueReady items. */
  blockedBy: BlockerRef[];
  /** Seconds left in the post-failure cooldown window. Null when no prior
   * attempt exists (never attempted) or when cooldown has elapsed.
   * Positive when the issue is still cooling down after a recent failure.
   * Issue #3001. */
  cooldownSecondsRemaining: number | null;
}

export interface RecentCompletion {
  agentId: string;
  /** Issue the agent was working on. Nullable for scheduled-skill
   * agents (`/audit`, `/spotcheck`, `/dispatcher-daily-report`) which
   * have no closing GitHub issue — see migration 49 (issue #3381) and
   * bug #3425. */
  issueNumber: number | null;
  issueTitle: string | null;
  /** Priority label captured at claim time — `p0` | `p1` | `p2` | `p3`
   * | null. Same semantics as `DispatcherAgent.priority`; pre-migration-33
   * rows return null (em-dash in the UI). Issue #2899. */
  priority: string | null;
  /** One of `succeeded | failed | crashed`. */
  status: string;
  /** Timestamp the agent claimed the issue (`dispatcher.agents.started_at`).
   * Non-nullable on the API side. Issue #3024 — paired with `endedAt` for
   * the Start/Duration/End hover tooltip in the Recently completed panel.
   *
   * Issue #4064: may be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (which no longer selects `startedAt`).
   * The render code in `RecentCompletionRow` already handles missing
   * `startedAt` defensively — `Date.parse(undefined)` returns NaN,
   * `Number.isFinite(NaN)` is false, the tooltip falls back to the
   * single-line end-time string. Always populated on rows fetched via
   * `DISPATCHER_QUEUE_FULL_QUERY`. */
  startedAt?: string;
  endedAt: string;
  prNumber: number | null;
  /** Sum of input+output tokens across every phase the agent ran
   * (#2869). Null when no phase row has recorded usage — rendered as
   * "no cost data" rather than a misleading 0.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (which no longer selects `totalTokens`);
   * the cost footnote is absent on the polled row and reappears in the
   * expand-on-click dialog. `formatCostFootnote` treats undefined and
   * null identically. */
  totalTokens?: number | null;
  /** Sum of `cost_usd` across every phase the agent ran (#2869). Null
   * when no phase row has recorded usage.
   *
   * WARNING: list-price estimate, NOT Max plan-adjusted.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (same rationale as `totalTokens`). */
  totalCostUsd?: number | null;
  /** One-line "what happened" string for failure terminals (`failed`,
   * `crashed`, `plan_blocked`). Populated at terminal-time by the
   * daemon from `failures.category` + `phase` + stderr tail, then
   * optionally upgraded by `/diagnose-failure`. Capped at 240 chars.
   *
   * Null for `succeeded` / `needs_review` rows and for historical rows
   * that pre-date migration 33. Rendered as a tooltip on the outcome
   * glyph in the `Recently completed` panel — no always-visible second
   * line. Issue #2900.
   *
   * Issue #4064: kept on the polled `DISPATCHER_STATE_QUERY` selection
   * because the `OutcomePill` infra-preempted re-skin (gray ↺) keys off
   * a `failureSummary` exact-match against `INFRA_PREEMPTED_SUMMARIES`. */
  failureSummary: string | null;
  /** Timestamp the PR squash-merge was observed by the daemon. Paired
   * with the `status='succeeded'` flip at merge time — `mergedAt != null`
   * is the canonical "shipped" signal. Null on rows that never merged
   * (push failed, CI red after retries, etc.) and on pre-migration-35
   * historical rows. Issue #2953.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (the polled row falls back to the
   * status-only OutcomePill path; the milestone-completeness path runs
   * only for rows fetched via `DISPATCHER_QUEUE_FULL_QUERY`). */
  mergedAt?: string | null;
  /** Timestamp the verify phase completed with verdict=VERIFIED. Null
   * when verify was intentionally skipped (see `verifySkipReason`),
   * when verify crashed mid-phase, or for pre-migration-35 rows.
   * Issue #2953.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (same rationale as `mergedAt`). */
  verifiedAt?: string | null;
  /** Non-null when verify was intentionally skipped. Today the only
   * written value is `"self_deploy"` (dispatcher-self-PR touches
   * `scripts/dispatcher/`). A merged row with a non-null skip reason
   * counts as fully-shipped (green ✓) — skipping is not a regression.
   * Issue #2953.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (same rationale as `mergedAt`). */
  verifySkipReason?: string | null;
  /** Timestamp the retro phase completed (reached PHASE_RETRO_DONE).
   * Null when retro crashed, when the worktree was already gone at
   * retro time, or for pre-migration-35 rows. Issue #2953.
   *
   * Issue #4064: may also be `undefined` on rows fetched via the polled
   * `DISPATCHER_STATE_QUERY` (same rationale as `mergedAt`). */
  retroedAt?: string | null;
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
  /** Total count of terminal-status rows in `dispatcher.agents` (#3172).
   * Paired with `recentCompletions` (capped at 10 server-side) so the
   * admin-cockpit panel header can render `{shown} / {total}`, matching
   * the Ready/Blocked panels. Returns 0 when the table is empty. */
  recentCompletionsCount: number;
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

/**
 * Result type for `DISPATCHER_CONFIG_QUERY` (issue #4063). The query
 * selects only `dispatcherState.config`, so the result shape is a
 * minimal `dispatcherState` object carrying just that field.
 */
export interface DispatcherConfigData {
  dispatcherState: {
    config: DispatcherConfigEntry[];
  };
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

export const WEEKLY_DIAGNOSER_REPORT_QUERY = gql`
  query WeeklyDiagnoserReport {
    weeklyDiagnoserReport {
      recommendedAction
      observedOutcome
      count
      day
    }
  }
`;

/** One bucket in the diagnoser effectiveness rollup (issue #2800). */
export interface DiagnoserEffectivenessRow {
  recommendedAction: string;
  observedOutcome: string;
  count: number;
  /** ISO-8601 calendar day string (UTC, day precision). */
  day: string;
}

export interface WeeklyDiagnoserReportData {
  weeklyDiagnoserReport: DiagnoserEffectivenessRow[];
}
