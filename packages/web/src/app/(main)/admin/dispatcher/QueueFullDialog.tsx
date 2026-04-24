'use client';

import { useQuery } from '@apollo/client';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  DISPATCHER_QUEUE_FULL_QUERY,
  type DispatcherQueueFullData,
  type DispatcherQueueKind,
} from '@/lib/dispatcher-queries';
import { QueueRow } from './QueuePanel';
import { RecentCompletionRow } from './RecentCompletionsPanel';

/**
 * Full-list expand-count dialog for the dispatcher cockpit (issue #3159).
 * Triggered by clicking the count badge on any of the three bottom-row
 * panels (Ready / Blocked / Recently Completed). Renders every item the
 * daemon snapshot knows about — no 10-cap.
 *
 * Data path: a separate `dispatcherQueueFull(kind)` GraphQL query, fired
 * only when `kind !== null`. The 2s `dispatcherState` poll on the parent
 * dashboard is unaffected — opening this dialog adds no traffic to the
 * hot path. Apollo's cache normalizes per-`kind` (see
 * `apollo-client.ts`), and `fetchPolicy: 'network-only'` forces a fresh
 * fetch on each open so the dialog always reflects the latest snapshot.
 *
 * Reuses `QueueRow` (Ready/Blocked) and `RecentCompletionRow`
 * (Completed) so the dialog rows are visually identical to the panel
 * rows — operators don't have to relearn the row layout when they
 * expand.
 */
export function QueueFullDialog({
  kind,
  onClose,
}: {
  /** Open when not null — also drives which kind to fetch. */
  kind: DispatcherQueueKind | null;
  /** Called when the dialog closes (ESC, click outside, X button). */
  onClose: () => void;
}) {
  const open = kind !== null;
  const { data, loading, error } = useQuery<DispatcherQueueFullData>(
    DISPATCHER_QUEUE_FULL_QUERY,
    {
      variables: kind !== null ? { kind } : undefined,
      // Skip when the dialog is closed — Apollo will not fire the query
      // until the operator actually clicks a count badge.
      skip: kind === null,
      // Network-only on each open: the snapshot updates every 30s on
      // the daemon side, and the operator has just clicked because they
      // want to see the current tail. A cache hit could show stale rows.
      fetchPolicy: 'network-only',
    },
  );

  const payload = data?.dispatcherQueueFull;
  const queueItems = payload?.queueItems ?? [];
  const completions = payload?.completions ?? [];
  const totalCount =
    kind === 'COMPLETED' ? completions.length : queueItems.length;

  const titleText = formatDialogTitle(kind, totalCount, loading);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent
        className="max-w-2xl"
        data-testid="queue-full-dialog"
      >
        <DialogHeader>
          <DialogTitle data-testid="queue-full-dialog-title">
            {titleText}
          </DialogTitle>
          <DialogDescription className="sr-only">
            Full list of items the dispatcher snapshot knows about for the
            selected bucket. Press Escape or click outside to close.
          </DialogDescription>
        </DialogHeader>
        <div
          className="max-h-[60vh] overflow-y-auto"
          data-testid="queue-full-dialog-body"
        >
          {loading && (
            <p
              className="py-2 text-sm text-muted-foreground"
              data-testid="queue-full-dialog-loading"
            >
              Loading…
            </p>
          )}
          {error && !loading && (
            <p
              role="alert"
              className="py-2 text-sm text-red-600 dark:text-red-400"
              data-testid="queue-full-dialog-error"
            >
              Failed to load: {error.message}
            </p>
          )}
          {!loading && !error && payload && (
            <DialogList
              kind={payload.kind}
              queueItems={queueItems}
              completions={completions}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Inner list renderer — split out so the empty / non-empty branches
 * stay readable inside the dialog's loading/error scaffolding.
 */
function DialogList({
  kind,
  queueItems,
  completions,
}: {
  kind: DispatcherQueueKind;
  queueItems: DispatcherQueueFullData['dispatcherQueueFull']['queueItems'];
  completions: DispatcherQueueFullData['dispatcherQueueFull']['completions'];
}) {
  if (kind === 'READY') {
    if (queueItems.length === 0) {
      return (
        <p
          className="py-2 text-sm text-muted-foreground"
          data-testid="queue-full-dialog-empty"
        >
          Queue empty — file an issue with{' '}
          <code className="font-mono">agent/ready</code> to seed.
        </p>
      );
    }
    return (
      <ul
        className="divide-y divide-border"
        data-testid="queue-full-dialog-list"
      >
        {queueItems.map((item) => (
          // Ready rows do NOT carry the `animated` view-transition-name
          // here — the dialog list is a separate DOM tree from the
          // panel list, and giving rows the same `issue-<N>` name in
          // both would collide on the next view-transition tick (#2967).
          <QueueRow key={item.issueNumber} item={item} />
        ))}
      </ul>
    );
  }
  if (kind === 'BLOCKED') {
    if (queueItems.length === 0) {
      return (
        <p
          className="py-2 text-sm text-muted-foreground"
          data-testid="queue-full-dialog-empty"
        >
          No blocked issues.
        </p>
      );
    }
    return (
      <ul
        className="divide-y divide-border"
        data-testid="queue-full-dialog-list"
      >
        {queueItems.map((item) => (
          <QueueRow key={item.issueNumber} item={item} />
        ))}
      </ul>
    );
  }
  // COMPLETED
  if (completions.length === 0) {
    return (
      <p
        className="py-2 text-sm text-muted-foreground"
        data-testid="queue-full-dialog-empty"
      >
        No completed agents yet.
      </p>
    );
  }
  return (
    <ul
      className="divide-y divide-border"
      data-testid="queue-full-dialog-list"
    >
      {completions.map((completion) => (
        <RecentCompletionRow
          key={completion.agentId}
          completion={completion}
        />
      ))}
    </ul>
  );
}

/**
 * Format the dialog title — the heading text mirrors which bucket the
 * operator opened. Pure helper so the test can exercise the formatting
 * without rendering the dialog. Issue #3159.
 */
export function formatDialogTitle(
  kind: DispatcherQueueKind | null,
  count: number,
  loading: boolean,
): string {
  if (kind === null) return '';
  const label =
    kind === 'READY'
      ? 'Agent-ready queue'
      : kind === 'BLOCKED'
        ? 'Blocked queue'
        : 'Recently completed';
  if (loading) return `${label} (loading…)`;
  return `${label} (${count})`;
}
