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
