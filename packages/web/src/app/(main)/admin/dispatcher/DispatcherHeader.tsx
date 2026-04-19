'use client';

import type { DispatcherRun } from '@/lib/dispatcher-queries';
import { formatUptime } from './format-helpers';

type DaemonStatus = 'running' | 'paused' | 'stopped' | 'unhealthy';

// Per #2805 §2.3 the pill is informational status, not a CTA — kept
// compact (text-xs) with a subtle colored dot + neutral surround rather
// than the previous large filled badge. Borderless.
const STATUS_DOT: Record<DaemonStatus, string> = {
  running: 'bg-green-500',
  paused: 'bg-yellow-500',
  stopped: 'bg-stone-400',
  unhealthy: 'bg-red-500',
};

const STATUS_TEXT: Record<DaemonStatus, string> = {
  running: 'text-green-700 dark:text-green-300',
  paused: 'text-yellow-700 dark:text-yellow-300',
  stopped: 'text-muted-foreground',
  unhealthy: 'text-red-700 dark:text-red-300',
};

/**
 * Derive daemon status from the latest `dispatcher.runs` row.
 *
 * - `null` run → never booted → "stopped"
 * - `stoppedAt` populated → clean shutdown → "stopped"
 * - heartbeat older than 3 minutes (well outside the 2-min supervisor
 *   tick) → "unhealthy"
 * - otherwise → "running". The daemon pauses via `dispatcher.commands`
 *   without writing a new runs row, so Phase 1 does not distinguish
 *   paused-vs-running from the `runs` table alone. A follow-up will
 *   surface pause state via a `dispatcherState.paused` field.
 */
export function deriveDaemonStatus(
  run: DispatcherRun | null,
  nowMs: number = Date.now(),
): DaemonStatus {
  if (!run) return 'stopped';
  if (run.stoppedAt) return 'stopped';
  const heartbeat = new Date(run.heartbeatTs).getTime();
  if (!Number.isFinite(heartbeat)) return 'unhealthy';
  const ageMs = nowMs - heartbeat;
  if (ageMs > 3 * 60 * 1000) return 'unhealthy';
  return 'running';
}

interface DispatcherHeaderProps {
  currentRun: DispatcherRun | null;
  /** Override for deterministic tests. Defaults to `Date.now()`. */
  nowMs?: number;
}

/**
 * Compact left-cluster status block for the one-line header row
 * (#2805 §1.1). Renders: status pill · uptime · version sha · host,
 * all inline, borderless, muted/monospace metadata.
 *
 * The outer polite live region announces status-pill transitions
 * without stepping on focused elements (#2805 §3.5).
 */
export function DispatcherHeader({ currentRun, nowMs }: DispatcherHeaderProps) {
  const status = deriveDaemonStatus(currentRun, nowMs);
  const uptime = currentRun && !currentRun.stoppedAt
    ? formatUptime(currentRun.startedAt, nowMs)
    : null;
  const versionSha = currentRun?.versionSha ?? null;

  return (
    <div
      aria-live="polite"
      className="flex flex-wrap items-center gap-x-2 font-mono text-xs text-muted-foreground"
    >
      <span
        data-testid="daemon-status-pill"
        className="inline-flex items-center gap-1.5"
      >
        <span
          aria-hidden="true"
          className={`inline-block h-2 w-2 rounded-full ${STATUS_DOT[status]}`}
        />
        <span className={`font-medium capitalize ${STATUS_TEXT[status]}`}>
          {status}
        </span>
      </span>
      <span aria-hidden="true">&middot;</span>
      <span>
        uptime <span className="text-foreground">{uptime ?? '—'}</span>
      </span>
      <span aria-hidden="true">&middot;</span>
      <span>
        sha <span className="text-foreground">{versionSha ? versionSha.slice(0, 8) : '—'}</span>
      </span>
      {currentRun?.host && (
        <>
          <span aria-hidden="true">&middot;</span>
          <span>
            host <span className="text-foreground">{currentRun.host}</span>
          </span>
        </>
      )}
    </div>
  );
}
