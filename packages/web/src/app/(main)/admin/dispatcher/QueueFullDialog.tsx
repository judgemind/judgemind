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
  shown,
  total,
  onClose,
}: {
  /** Open when not null — also drives which kind to fetch. */
  kind: DispatcherQueueKind | null;
  /**
   * Number of rows the panel that opened this dialog was rendering at
   * click time (capped at 10 server-side). Surfaced in the title's
   * `{shown} / {total}` so the dialog title carries the same denominator
   * as the badge the operator just clicked (issue #3172). Optional —
   * when omitted the title falls back to bare `{label}` without a count
   * decoration so existing test fixtures keep working.
   */
  shown?: number;
  /**
   * Total count of rows in the bucket the panel knows about (sourced
   * from `DispatcherState.queueDepth` / `blockedDepth` /
   * `recentCompletionsCount`). Same role as `shown` — surfaces the
   * denominator. Optional for the same back-compat reason.
   */
  total?: number;
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

  const titleText = formatDialogTitle(kind, shown, total, loading);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent
        // #3172: bumped from `max-w-2xl` (42rem / 672px) to `max-w-4xl`
        // (56rem / 896px). The cockpit page is a wide 3-column layout
        // and the previous width forced full issue rows (issue link +
        // priority badge + PR link + title) to wrap awkwardly. `max-w-4xl`
        // gives ~30% more horizontal room without crowding the page on
        // common 1280px+ viewports.
        className="max-w-4xl"
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
 * Format the dialog title — the heading text mirrors the panel heading
 * the operator just clicked, and the count decoration carries the same
 * `{shown} / {total}` denominator the badge displayed.
 *
 * Issue #3172 made two changes vs. the original #3159 shape:
 *  1. Labels now match the panel headings exactly:
 *     - `READY`     → `Queue: Agent-ready`   (matches `QueuePanel.tsx`)
 *     - `BLOCKED`   → `Queue: Blocked`       (matches `QueuePanel.tsx`)
 *     - `COMPLETED` → `Recently completed`   (matches `RecentCompletionsPanel.tsx`)
 *     The original `Agent-ready queue (N)` / `Blocked queue (N)` strings
 *     inverted the noun/qualifier order vs. the panel headings, so the
 *     dialog visually disconnected from the badge that opened it.
 *  2. Count decoration is `{shown} / {total}` (em-dash separator), not
 *     `(N)`. When `shown` and `total` are both numbers, the title
 *     reads e.g. `Queue: Agent-ready — 10 / 50`. When either is
 *     undefined (back-compat / kind=null), the count decoration is
 *     dropped so the title stays clean.
 *
 * Pure helper so the test can exercise the formatting without rendering
 * the dialog.
 */
export function formatDialogTitle(
  kind: DispatcherQueueKind | null,
  shown: number | undefined,
  total: number | undefined,
  loading: boolean,
): string {
  if (kind === null) return '';
  const label =
    kind === 'READY'
      ? 'Queue: Agent-ready'
      : kind === 'BLOCKED'
        ? 'Queue: Blocked'
        : 'Recently completed';
  if (loading) return `${label} — loading…`;
  if (typeof shown === 'number' && typeof total === 'number') {
    return `${label} — ${shown} / ${total}`;
  }
  return label;
}
