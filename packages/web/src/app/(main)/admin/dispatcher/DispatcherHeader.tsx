'use client';

import type { DispatcherRun } from '@/lib/dispatcher-queries';
import { formatUptime } from './format-helpers';

type DaemonStatus = 'running' | 'paused' | 'stopped' | 'unhealthy';

const STATUS_STYLES: Record<DaemonStatus, string> = {
  running: 'bg-green-100 border-green-300 text-green-800 dark:bg-green-900/30 dark:border-green-700 dark:text-green-300',
  paused: 'bg-yellow-100 border-yellow-300 text-yellow-800 dark:bg-yellow-900/30 dark:border-yellow-700 dark:text-yellow-300',
  stopped: 'bg-stone-100 border-stone-300 text-stone-800 dark:bg-stone-800 dark:border-stone-600 dark:text-stone-300',
  unhealthy: 'bg-red-100 border-red-300 text-red-800 dark:bg-red-900/30 dark:border-red-700 dark:text-red-300',
};

const STATUS_DOT: Record<DaemonStatus, string> = {
  running: 'bg-green-500',
  paused: 'bg-yellow-500',
  stopped: 'bg-stone-400',
  unhealthy: 'bg-red-500',
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
  // Malformed heartbeatTs → cannot verify liveness → treat as unhealthy.
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

export function DispatcherHeader({ currentRun, nowMs }: DispatcherHeaderProps) {
  const status = deriveDaemonStatus(currentRun, nowMs);
  const uptime = currentRun && !currentRun.stoppedAt
    ? formatUptime(currentRun.startedAt, nowMs)
    : null;
  const versionSha = currentRun?.versionSha ?? null;

  return (
    <section
      aria-labelledby="dispatcher-header-heading"
      className="flex flex-wrap items-center gap-x-6 gap-y-3 rounded-lg border border-border bg-card p-4"
    >
      <h2 id="dispatcher-header-heading" className="sr-only">
        Daemon status
      </h2>

      <span
        data-testid="daemon-status-pill"
        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium ${STATUS_STYLES[status]}`}
      >
        <span
          aria-hidden="true"
          className={`inline-block h-2.5 w-2.5 rounded-full ${STATUS_DOT[status]}`}
        />
        <span className="capitalize">{status}</span>
      </span>

      <div className="text-sm">
        <span className="text-muted-foreground">Uptime: </span>
        <span className="font-medium text-foreground">{uptime ?? '—'}</span>
      </div>

      <div className="text-sm">
        <span className="text-muted-foreground">Version: </span>
        <span className="font-mono text-xs text-foreground">
          {versionSha ? versionSha.slice(0, 8) : '—'}
        </span>
      </div>

      {currentRun?.host && (
        <div className="text-sm">
          <span className="text-muted-foreground">Host: </span>
          <span className="font-mono text-xs text-foreground">{currentRun.host}</span>
        </div>
      )}
    </section>
  );
}
