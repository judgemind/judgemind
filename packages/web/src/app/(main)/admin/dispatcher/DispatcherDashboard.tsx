'use client';

import { useCallback, useState } from 'react';
import { notFound } from 'next/navigation';
import { useMutation, useQuery } from '@apollo/client';
import { useAuth } from '@/providers/AuthProvider';
import { ErrorBanner } from '@/components/ErrorBanner';
import { PAGE_TITLE } from '@/lib/typography';
import {
  DISPATCHER_CONTROL_MUTATION,
  DISPATCHER_STATE_QUERY,
  DESTRUCTIVE_COMMANDS,
  type DispatcherCommand,
  type DispatcherControlData,
  type DispatcherStateData,
} from '@/lib/dispatcher-queries';
import { DispatcherHeader } from './DispatcherHeader';
import { DispatcherControls } from './DispatcherControls';
import { ActiveAgentsTable } from './ActiveAgentsTable';
import { QueuePanel } from './QueuePanel';
import { RecentFailuresPanel } from './RecentFailuresPanel';
import { ConfigPanel } from './ConfigPanel';
import { ConfirmDialog } from './ConfirmDialog';

/**
 * Polling interval for `dispatcherState`. Per spec §11, the daemon runs on
 * 30s/2min cadences, so a 2s polling delay is invisible.
 */
const POLL_INTERVAL_MS = 2000;

/**
 * Static config shown in the read-only config panel (§11 says "editable"
 * but the issue body carves editing out of Phase 1). These labels describe
 * the keys that live in `dispatcher.config` — the values themselves are
 * not yet exposed via GraphQL, so Phase 1 shows placeholders with a note
 * pointing at the follow-up work.
 */
const CONFIG_KEYS = [
  { key: 'concurrency_cap', label: 'Concurrency cap' },
  { key: 'idle_mode', label: 'Idle mode' },
  { key: 'backoff_seconds', label: 'Backoff seconds' },
] as const;

function SkeletonShell() {
  return (
    <div className="space-y-6">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="h-24 animate-pulse rounded-lg bg-muted motion-reduce:animate-none" />
      ))}
    </div>
  );
}

export function DispatcherDashboard() {
  const { user, loading: authLoading } = useAuth();

  // Non-admin 404: only after auth has resolved. Until then render the
  // skeleton so we don't flash a 404 for an admin session that is still
  // hydrating. notFound() terminates rendering and hands off to Next's
  // not-found boundary.
  if (!authLoading && (!user || user.role !== 'admin')) {
    notFound();
  }

  return <DispatcherDashboardInner authReady={!authLoading} />;
}

/**
 * Inner dashboard — split out so the admin check in `DispatcherDashboard`
 * can early-return via `notFound()` without first mounting any of the
 * Apollo hooks (which would otherwise fire `dispatcherState` requests from
 * non-admin sessions, producing misleading NOT_FOUND errors in logs).
 */
function DispatcherDashboardInner({ authReady }: { authReady: boolean }) {
  const [pendingCommand, setPendingCommand] = useState<DispatcherCommand | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);

  const { data, loading, error, refetch } = useQuery<DispatcherStateData>(
    DISPATCHER_STATE_QUERY,
    {
      skip: !authReady,
      pollInterval: authReady ? POLL_INTERVAL_MS : 0,
      fetchPolicy: 'cache-and-network',
    },
  );

  const [dispatcherControl, { loading: controlLoading }] =
    useMutation<DispatcherControlData>(DISPATCHER_CONTROL_MUTATION);

  const runControlCommand = useCallback(
    async (command: DispatcherCommand, payload: Record<string, unknown> = {}) => {
      setCommandError(null);
      try {
        await dispatcherControl({
          variables: { command, payload },
          // Destructive commands require a non-empty X-MFA-Token header per
          // the Phase 1 placeholder in `dispatcher/auth.ts`. A follow-up
          // issue will wire a real MFA challenge flow; for now we always
          // send a non-empty placeholder so the destructive flow succeeds.
          context: DESTRUCTIVE_COMMANDS.has(command)
            ? { headers: { 'X-MFA-Token': 'phase1-placeholder' } }
            : undefined,
        });
        // Kick an immediate refetch so the page doesn't wait up to 2s for
        // the next poll to reflect the newly-written command row.
        await refetch();
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Command failed';
        setCommandError(message);
      }
    },
    [dispatcherControl, refetch],
  );

  const handleControlClick = useCallback(
    (command: DispatcherCommand) => {
      setCommandError(null);
      if (DESTRUCTIVE_COMMANDS.has(command)) {
        setPendingCommand(command);
        return;
      }
      void runControlCommand(command);
    },
    [runControlCommand],
  );

  const handleConfirm = useCallback(() => {
    if (pendingCommand === null) return;
    const cmd = pendingCommand;
    setPendingCommand(null);
    void runControlCommand(cmd);
  }, [pendingCommand, runControlCommand]);

  const handleCancel = useCallback(() => {
    setPendingCommand(null);
  }, []);

  const handleAgentAction = useCallback(
    (command: 'retry' | 'force_kill', agentId: string) => {
      setCommandError(null);
      if (DESTRUCTIVE_COMMANDS.has(command)) {
        // For per-agent destructive actions we could open the same modal;
        // to keep Phase 1 minimal we reuse the non-agent modal path by
        // encoding the agentId in a sentinel field. `force_kill` is the
        // only destructive per-agent command today.
        const confirmed = window.confirm(
          `Force-kill agent ${agentId.slice(0, 8)}? This cannot be undone.`,
        );
        if (!confirmed) return;
      }
      void runControlCommand(command, { agentId });
    },
    [runControlCommand],
  );

  // Loading states — show the skeleton while either auth or the first
  // dispatcherState fetch is still in flight.
  if (!authReady) {
    return <SkeletonShell />;
  }

  // GraphQL errors — the server returns NOT_FOUND for non-admins; that
  // path is already handled by the admin check above, so any error here
  // is an actual infrastructure problem.
  if (error) {
    return <ErrorBanner message="Failed to load dispatcher state." onRetry={() => void refetch()} />;
  }

  const state = data?.dispatcherState;
  const showInitialLoad = loading && !state;

  return (
    <div>
      <h1 className={PAGE_TITLE}>Dispatcher</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Monitor and control the dispatcher daemon. Polls every {POLL_INTERVAL_MS / 1000}s.
      </p>

      {showInitialLoad ? (
        <div className="mt-6">
          <SkeletonShell />
        </div>
      ) : (
        <div className="mt-6 space-y-8">
          <DispatcherHeader currentRun={state?.currentRun ?? null} />

          {commandError && (
            <div
              role="alert"
              className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-700 dark:bg-red-900/30 dark:text-red-200"
            >
              Command failed: {commandError}
            </div>
          )}

          <DispatcherControls
            currentRun={state?.currentRun ?? null}
            disabled={controlLoading}
            onControlClick={handleControlClick}
          />

          <ActiveAgentsTable
            agents={state?.activeAgents ?? []}
            disabled={controlLoading}
            onAgentAction={handleAgentAction}
          />

          <QueuePanel queueDepth={state?.queueDepth ?? 0} />

          <RecentFailuresPanel failures={state?.recentFailures ?? []} />

          <ConfigPanel configKeys={CONFIG_KEYS} spawnFrozenUntil={state?.spawnFrozenUntil ?? null} />
        </div>
      )}

      <ConfirmDialog
        command={pendingCommand}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </div>
  );
}
