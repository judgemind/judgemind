'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { notFound } from 'next/navigation';
import { useMutation, useQuery } from '@apollo/client';
import { useAuth } from '@/providers/AuthProvider';
import { ErrorBanner } from '@/components/ErrorBanner';
import { useViewTransitionUpdate } from '@/hooks/useViewTransition';
import { PAGE_TITLE } from '@/lib/typography';
import {
  DISPATCHER_CONTROL_MUTATION,
  DISPATCHER_SET_CONFIG_MUTATION,
  DISPATCHER_STATE_QUERY,
  type DispatcherCommand,
  type DispatcherControlData,
  type DispatcherSetConfigData,
  type DispatcherStateData,
} from '@/lib/dispatcher-queries';
import { DispatcherHeader } from './DispatcherHeader';
import { DispatcherControls } from './DispatcherControls';
import { ActiveAgentsTable } from './ActiveAgentsTable';
import { QueueBlockedPanel, QueueReadyPanel } from './QueuePanel';
import { RecentCompletionsPanel } from './RecentCompletionsPanel';
import { RecentFailuresPanel } from './RecentFailuresPanel';
import { ConfigPanel } from './ConfigPanel';
import { ConfirmDialog, type ConfirmableCommand } from './ConfirmDialog';

/**
 * Polling interval for `dispatcherState`. Per spec §11, the daemon runs on
 * 30s/2min cadences, so a 2s polling delay is invisible.
 */
const POLL_INTERVAL_MS = 2000;

/**
 * Sentinel value passed to the `ConfirmDialog` when the operator is
 * lowering the concurrency cap — the dialog shows tailored copy for the
 * side-effect warning described in #2805 §1.6 (spec §17 Risk 6).
 */
const LOWER_CAP_CONFIRM = 'lower_cap' as const;

function SkeletonShell() {
  return (
    <div className="space-y-4">
      <div className="h-6 w-64 animate-pulse rounded bg-muted motion-reduce:animate-none" />
      <div className="grid grid-cols-1 gap-x-6 gap-y-4 lg:grid-cols-2">
        <div className="h-48 animate-pulse rounded bg-muted motion-reduce:animate-none" />
        <div className="h-48 animate-pulse rounded bg-muted motion-reduce:animate-none" />
        <div className="h-48 animate-pulse rounded bg-muted motion-reduce:animate-none" />
        <div className="h-48 animate-pulse rounded bg-muted motion-reduce:animate-none" />
      </div>
      <div className="h-10 animate-pulse rounded bg-muted motion-reduce:animate-none" />
    </div>
  );
}

/**
 * Refreshed, info-dense cockpit for /admin/dispatcher (#2805 Phase 1;
 * layout restructured in #2823).
 *
 * Layout is a two-column state-flow deck below the header strip. The
 * columns encode the natural motion of work through the daemon:
 *
 *   Queue: Agent-ready  →   Active agents   →   Recently completed
 *        (bottom-left)      (top-left)           (top-right)
 *
 * Claiming an issue moves it upward in the left column (queue → active).
 * Completing an issue moves it rightward to the top of the right column
 * (active → recently completed). Blocked issues live bottom-right —
 * adjacent to the flow but not part of the active cycle. The two columns
 * are independent vertical stacks, not a 2×2 grid — panels have their
 * natural heights and do not line up horizontally across columns.
 *
 *   ┌─ h1 Dispatcher · status pill · uptime · sha · host · [controls] ─┐
 *   ├─ Active agents ──────────────────┬─ Recently completed ──────────┤
 *   │   (0-N rows)                     │   (top 10)                    │
 *   ├─ Queue: Agent-ready ─────────────┼─ Queue: Blocked ──────────────┤
 *   │   (top 10)                       │   (top 10)                    │
 *   ├─ Config strip ──────────────────────────────────────────────────┤
 *   │   cap [N] · backoff [Ns] · spawn frozen: —                      │
 *   ├─ Recent failures (last 24h) ────────────────────────────────────┤
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * On mobile (`< lg`) the grid collapses to a single column in DOM order:
 *   Active → Queue-ready → Recently-completed → Queue-blocked.
 */
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
  const [pendingCommand, setPendingCommand] = useState<ConfirmableCommand | null>(null);
  const [pendingCap, setPendingCap] = useState<{ current: number; next: number } | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [busyConfigKey, setBusyConfigKey] = useState<string | null>(null);

  const { data, loading, error, refetch } = useQuery<DispatcherStateData>(
    DISPATCHER_STATE_QUERY,
    {
      skip: !authReady,
      pollInterval: authReady ? POLL_INTERVAL_MS : 0,
      fetchPolicy: 'cache-and-network',
    },
  );

  // Magic Move wiring (#2967). Apollo's `useQuery` pushes poll results
  // straight into `data`, but Magic Move needs the DOM mutation to happen
  // *inside* a `document.startViewTransition` callback so the browser
  // can snapshot the old frame, run the React render, then animate each
  // `view-transition-name` from its old position to its new one. We
  // mirror `data` into a local `renderedData` state and flip it through
  // the view-transition wrapper; the panel components render from the
  // mirror, so every poll update (and every imperative `refetch()`)
  // flows through the animation path without changing Apollo's cache
  // behaviour. On first paint we initialise synchronously — the very
  // first frame would have no "old" snapshot to animate from anyway.
  const [renderedData, setRenderedData] = useState<DispatcherStateData | undefined>(
    data,
  );
  const startViewTransitionUpdate = useViewTransitionUpdate();
  const lastAppliedDataRef = useRef<DispatcherStateData | undefined>(data);
  useEffect(() => {
    if (data === undefined) return;
    if (data === lastAppliedDataRef.current) return;
    const hasPrior = lastAppliedDataRef.current !== undefined;
    lastAppliedDataRef.current = data;
    if (!hasPrior) {
      // First successful fetch — no prior frame to transition from.
      setRenderedData(data);
      return;
    }
    startViewTransitionUpdate(() => setRenderedData(data));
  }, [data, startViewTransitionUpdate]);

  const [dispatcherControl, { loading: controlLoading }] =
    useMutation<DispatcherControlData>(DISPATCHER_CONTROL_MUTATION);

  const [dispatcherSetConfig] = useMutation<DispatcherSetConfigData>(
    DISPATCHER_SET_CONFIG_MUTATION,
  );

  const runControlCommand = useCallback(
    async (command: DispatcherCommand, payload: Record<string, unknown> = {}) => {
      setCommandError(null);
      try {
        // #2884: the X-MFA-Token placeholder was removed — admin
        // session auth is the only gate. No per-command context
        // headers needed.
        await dispatcherControl({
          variables: { command, payload },
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
      // #2884: `force_stop` is the only control command that confirms
      // via modal — it aborts in-flight work. `stop` is graceful
      // (in-flight agent finishes its current phase) and `start` is
      // safe, so both fire without a confirmation prompt. The whole
      // point of the simplification is that stopping dev work should
      // NOT be friction-heavy for the operator.
      if (command === 'force_stop') {
        setPendingCommand(command);
        return;
      }
      void runControlCommand(command);
    },
    [runControlCommand],
  );

  /**
   * Internal helper that actually calls `dispatcherSetConfig` with a
   * JSON-encoded value. Not exposed directly to `ConfigPanel` — see
   * `handleConfigEdit` below, which interposes the "lower cap"
   * confirm dialog. #2884: the MFA placeholder header was removed.
   */
  const commitConfigEdit = useCallback(
    async (key: string, value: string): Promise<void> => {
      setConfigError(null);
      setBusyConfigKey(key);
      try {
        await dispatcherSetConfig({
          variables: { key, value },
        });
        await refetch();
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Config update failed';
        setConfigError(message);
        throw err;
      } finally {
        setBusyConfigKey(null);
      }
    },
    [dispatcherSetConfig, refetch],
  );

  const handleConfirm = useCallback(() => {
    if (pendingCommand === null) return;
    if (pendingCommand === LOWER_CAP_CONFIRM) {
      const pc = pendingCap;
      setPendingCommand(null);
      setPendingCap(null);
      if (pc) {
        void commitConfigEdit('concurrency_cap', String(pc.next));
      }
      return;
    }
    const cmd = pendingCommand;
    setPendingCommand(null);
    void runControlCommand(cmd);
  }, [pendingCommand, pendingCap, runControlCommand, commitConfigEdit]);

  const handleCancel = useCallback(() => {
    setPendingCommand(null);
    setPendingCap(null);
  }, []);

  const handleAgentAction = useCallback(
    (command: 'retry' | 'force_stop', agentId: string) => {
      setCommandError(null);
      // Per-agent `force_stop` (formerly `force_kill`) still confirms —
      // it crashes a running agent without a clean exit. `retry` is
      // safe and fires immediately.
      if (command === 'force_stop') {
        const confirmed = window.confirm(
          `Force-stop agent ${agentId.slice(0, 8)}? This cannot be undone.`,
        );
        if (!confirmed) return;
      }
      void runControlCommand(command, { agentId });
    },
    [runControlCommand],
  );

  /**
   * Wrapped config-edit handler passed down to `ConfigPanel`. For
   * destructive transitions (concurrency_cap lowered) we pop the
   * ConfirmDialog first (spec §17 Risk 6).
   */
  const handleConfigEdit = useCallback(
    async (key: string, value: string): Promise<void> => {
      if (key === 'concurrency_cap') {
        const entries = renderedData?.dispatcherState?.config ?? [];
        const current = entries.find((e) => e.key === 'concurrency_cap');
        const currentValue = current ? parsePositiveInt(current.value) : null;
        const nextValue = parsePositiveInt(value);
        if (
          currentValue !== null &&
          nextValue !== null &&
          nextValue < currentValue
        ) {
          // Lowering the cap — pop the ConfirmDialog and let the commit
          // happen in `handleConfirm`. Return immediately so ConfigPanel's
          // local state doesn't stay in "editing".
          setPendingCap({ current: currentValue, next: nextValue });
          setPendingCommand(LOWER_CAP_CONFIRM);
          return;
        }
      }
      await commitConfigEdit(key, value);
    },
    [commitConfigEdit, renderedData],
  );

  // Loading states — show the skeleton while either auth or the first
  // dispatcherState fetch is still in flight.
  if (!authReady) {
    return <SkeletonShell />;
  }

  const state = renderedData?.dispatcherState;

  // GraphQL errors — hard-fail only when we have no cached state to show.
  // Apollo's polling contract is that `data` and `error` can both be
  // populated simultaneously: `data` holds the last successful result and
  // `error` holds the latest failure. If a transient polling refetch
  // fails after we already rendered the page, we keep showing the cached
  // state and surface a small "stale" indicator so the operator knows
  // the latest poll failed — tearing down the entire page on every
  // sub-2s blip is exactly the wrong behaviour during active agent
  // phases (#2842).
  if (error && !state) {
    return <ErrorBanner message="Failed to load dispatcher state." onRetry={() => void refetch()} />;
  }

  // `showInitialLoad` was historically `loading && !state`. After the
  // Magic Move mirror (#2967) it also needs to cover the one-render
  // gap between Apollo populating `data` and the useEffect flipping
  // `renderedData`: if we render before the effect commits, `state`
  // would be undefined even though data is ready. `data && !state`
  // catches that frame and keeps the skeleton up.
  const showInitialLoad = (loading && !state) || (data !== undefined && !state);

  return (
    <div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-border pb-3 mb-4">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className={PAGE_TITLE}>Dispatcher</h1>
          <DispatcherHeader currentRun={state?.currentRun ?? null} />
          {error && state && (
            <span
              data-testid="dispatcher-stale-indicator"
              role="status"
              aria-label="Last poll failed — showing cached state"
              title="Last poll failed — showing cached state"
              className="inline-flex items-center gap-1.5 font-mono text-xs text-yellow-700 dark:text-yellow-300"
            >
              <span
                aria-hidden="true"
                className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-500 motion-reduce:animate-none"
              />
              stale
            </span>
          )}
        </div>
        <DispatcherControls
          currentRun={state?.currentRun ?? null}
          disabled={controlLoading}
          onControlClick={handleControlClick}
        />
      </div>

      {state?.circuitBreakerOpen && (
        <div
          role="alert"
          data-testid="circuit-breaker-banner"
          className="mb-4 rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-800 dark:border-red-700 dark:bg-red-900/40 dark:text-red-200"
        >
          <div className="font-semibold">
            Circuit breaker open — dispatcher auto-paused (#2860).
          </div>
          <div className="mt-1">
            A streak of bad terminal outcomes tripped the circuit breaker.
            <code className="mx-1 font-mono">concurrency_cap</code>
            is held at 0. Review recent completions and failures below, then
            raise cap back to ≥1 in the config strip once the underlying
            pattern is triaged.
          </div>
        </div>
      )}

      {commandError && (
        <div
          role="alert"
          className="mb-4 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-900/30 dark:text-red-200"
        >
          Command failed: {commandError}
        </div>
      )}

      {showInitialLoad ? (
        <SkeletonShell />
      ) : (
        <>
          <div
            className="grid grid-cols-1 gap-x-6 gap-y-6 lg:grid-cols-2"
            data-testid="dispatcher-two-column-deck"
          >
            {/*
             * Left column: Active agents (top) → Queue: Agent-ready
             * (bottom). An issue moves upward within this column as it
             * transitions from "next" to "now". See file-level docstring
             * for the full state-flow rationale (#2823).
             */}
            <div className="flex flex-col gap-6" data-testid="dispatcher-column-left">
              <ActiveAgentsTable
                agents={state?.activeAgents ?? []}
                disabled={controlLoading}
                onAgentAction={handleAgentAction}
              />
              <QueueReadyPanel
                items={state?.queueReady ?? []}
                total={state?.queueDepth}
              />
            </div>

            {/*
             * Right column: Recently completed (top) → Queue: Blocked
             * (bottom). Work flows rightward from Active to Recently
             * completed. Blocked sits bottom-right — adjacent to the flow
             * but not part of the active cycle.
             */}
            <div className="flex flex-col gap-6" data-testid="dispatcher-column-right">
              <RecentCompletionsPanel
                completions={state?.recentCompletions ?? []}
              />
              <QueueBlockedPanel
                items={state?.queueBlocked ?? []}
                total={state?.blockedDepth}
              />
            </div>
          </div>

          <ConfigPanel
            entries={state?.config ?? []}
            spawnFrozenUntil={state?.spawnFrozenUntil ?? null}
            onCommitEdit={handleConfigEdit}
            errorMessage={configError}
            busyKey={busyConfigKey}
          />

          <div className="mt-6">
            <RecentFailuresPanel failures={state?.recentFailures ?? []} />
          </div>

          <p className="mt-4 text-xs text-muted-foreground">
            Daemon command handlers tracked in{' '}
            <a
              href="https://github.com/judgemind/judgemind/issues/2801"
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-accent dark:text-brand-accent-light underline-offset-2 hover:underline"
            >
              #2801
            </a>
            . Control buttons write to <code className="font-mono">dispatcher.commands</code>{' '}
            today; daemon-side handlers land with that issue.
          </p>
        </>
      )}

      <ConfirmDialog
        command={pendingCommand}
        capDetail={pendingCommand === LOWER_CAP_CONFIRM ? pendingCap : null}
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </div>
  );
}

function parsePositiveInt(jsonValue: string): number | null {
  try {
    const raw = JSON.parse(jsonValue);
    if (typeof raw === 'number' && Number.isInteger(raw) && raw >= 0) return raw;
    if (typeof raw === 'string') {
      const n = Number.parseInt(raw, 10);
      return Number.isFinite(n) && n >= 0 ? n : null;
    }
    return null;
  } catch {
    // jsonValue may already be a bare integer string like "3" from the
    // config editor — try a direct parse as a fallback.
    const n = Number.parseInt(jsonValue, 10);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }
}
