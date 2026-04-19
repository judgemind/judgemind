/**
 * Pure formatting helpers used across the dispatcher admin page. Kept
 * separate so they can be unit tested without mounting React.
 */

/**
 * Format a duration (milliseconds) into a compact human string.
 * Examples: "12s", "4m 12s", "2h 15m", "1d 4h".
 *
 * - < 60s: seconds
 * - < 60m: minutes + seconds
 * - < 24h: hours + minutes
 * - >= 24h: days + hours
 */
export function formatDurationMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  const remSeconds = totalSeconds % 60;
  if (totalMinutes < 60) return `${totalMinutes}m ${remSeconds}s`;
  const totalHours = Math.floor(totalMinutes / 60);
  const remMinutes = totalMinutes % 60;
  if (totalHours < 24) return `${totalHours}h ${remMinutes}m`;
  const totalDays = Math.floor(totalHours / 24);
  const remHours = totalHours % 24;
  return `${totalDays}d ${remHours}h`;
}

/**
 * Compute elapsed time from an ISO-8601 `startedAt` to `nowMs`, returning
 * a formatted string. Returns `—` for invalid or future timestamps.
 */
export function formatUptime(startedAt: string, nowMs: number = Date.now()): string {
  const started = new Date(startedAt).getTime();
  if (!Number.isFinite(started)) return '—';
  const ageMs = nowMs - started;
  if (ageMs < 0) return '—';
  return formatDurationMs(ageMs);
}

/** Truncate an agent id for display: first 8 chars + ellipsis if longer. */
export function shortAgentId(agentId: string): string {
  if (agentId.length <= 8) return agentId;
  return `${agentId.slice(0, 8)}…`;
}

/**
 * Build a CloudWatch Logs console URL for a given agent's worktree path
 * when the worktree name encodes the agent id (e.g.
 * `.claude/worktrees/agent-aca547d3`). Dispatcher daemon agents log to
 * `/ecs/judgemind-dispatcher-dev` with stream prefix derived from the
 * agent id. Returns `null` when we cannot derive a stream from the path
 * (e.g. local-dev runs with a non-standard worktree layout).
 *
 * Phase 1 only uses the dev log group. Phase 2 will thread the
 * environment through via the GraphQL payload.
 */
export function worktreeLogsUrl(worktreePath: string): string | null {
  const match = worktreePath.match(/(agent-[a-zA-Z0-9]+)$/);
  if (!match) return null;
  const stream = match[1];
  const region = 'us-west-2';
  // CloudWatch console URLs use a bespoke encoding where `/` is `$252F`
  // (double-URL-encoded `/`). Use literal replacement rather than relying
  // on encodeURIComponent → substitution, which would also encode safe
  // characters like `-` inconsistently.
  const encodedGroup = '$252Fecs$252Fjudgemind-dispatcher-dev';
  return (
    `https://${region}.console.aws.amazon.com/cloudwatch/home` +
    `?region=${region}#logsV2:log-groups/log-group/${encodedGroup}` +
    `/log-events/${stream}`
  );
}

/**
 * Epoch-origin sentinel threshold. Any timestamp at or before this date is
 * treated as "unset / unknown" rather than a real past time. This catches
 * the failure mode where a backend fallback uses `new Date(0).toISOString()`
 * (or `0` as a ms epoch) to satisfy a non-nullable `DateTime!` GraphQL field
 * — without this guard, the helper cheerfully formats it as "20562 d ago"
 * (#2818).
 *
 * Threshold is 2000-01-01 UTC: the project has never emitted a legitimate
 * timestamp older than this, and real-world calendar drift shouldn't push
 * a genuine recent timestamp below it.
 */
const EPOCH_SANITY_THRESHOLD_MS = Date.UTC(2000, 0, 1);

/**
 * Format a past timestamp as a short relative string like "3 min ago",
 * "2 h ago", "5 d ago". Returns `'—'` for invalid, future, null, zero,
 * or pre-2000 (epoch-sentinel) timestamps.
 *
 * Buckets:
 *   - < 60 s     → "Ns ago"
 *   - < 60 min   → "N min ago"
 *   - < 24 h     → "N h ago"
 *   - otherwise  → "N d ago"
 *
 * The null/zero/pre-2000 handling is defensive against backend fallbacks
 * that substitute `new Date(0).toISOString()` for a missing upstream
 * timestamp (#2818). Without it, callers see "20562 d ago" (≈56 years)
 * for every such row, which is worse than showing nothing.
 */
export function formatRelativeTime(iso: string | null, nowMs: number = Date.now()): string {
  if (!iso) return '—';
  const ts = new Date(iso).getTime();
  if (!Number.isFinite(ts)) return '—';
  // Treat zero / pre-2000 timestamps as unset. See EPOCH_SANITY_THRESHOLD_MS
  // above for the rationale.
  if (ts <= EPOCH_SANITY_THRESHOLD_MS) return '—';
  const deltaMs = nowMs - ts;
  if (deltaMs < 0) return '—';
  const seconds = Math.floor(deltaMs / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `${days} d ago`;
}

/**
 * Group failures by the `category` field and return an ordered list of
 * `{ category, count, mostRecent }` entries, sorted by count desc. Used
 * by the Recent failures panel.
 */
export interface FailureGroup<F> {
  category: string;
  count: number;
  mostRecent: F;
}

export function groupFailuresByCategory<F extends { category: string; ts: string }>(
  failures: readonly F[],
): FailureGroup<F>[] {
  if (failures.length === 0) return [];
  const groups = new Map<string, FailureGroup<F>>();
  for (const f of failures) {
    const existing = groups.get(f.category);
    if (!existing) {
      groups.set(f.category, { category: f.category, count: 1, mostRecent: f });
      continue;
    }
    existing.count += 1;
    // Keep whichever has the newer ts.
    if (f.ts > existing.mostRecent.ts) {
      existing.mostRecent = f;
    }
  }
  return Array.from(groups.values()).sort((a, b) => b.count - a.count);
}
