'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { QueueItem } from '@/lib/dispatcher-queries';
import { IssueLink, PriorityBadge } from './ui-primitives';

interface QueuePanelProps {
  /** Agent-ready issues from the latest `dispatcher.queue_snapshots`. */
  queueReady: readonly QueueItem[];
  /** Open issues labelled `status/blocked`. */
  queueBlocked: readonly QueueItem[];
  /**
   * Total count of agent-ready issues observed by the daemon's most
   * recent queue scan (from `DispatcherState.queueDepth`). Rendered in
   * the Queue: Agent-ready panel header as `{items.length} / {total}`
   * so the operator can see queue depth even when the displayed list
   * is capped server-side at 10 (issue #2886). Optional so existing
   * test fixtures keep working — omit to hide the denominator.
   */
  queueReadyTotal?: number;
  /**
   * Total count of open `status/blocked` issues observed by the daemon's
   * most recent blocked scan (from `DispatcherState.blockedDepth`).
   * Same semantics as `queueReadyTotal`. Issue #2886.
   */
  queueBlockedTotal?: number;
  /**
   * Override for deterministic tests. Currently unused after #2818 stripped
   * the "filed N d ago" column — kept in the prop signature so existing
   * callers (`DispatcherDashboard`, the test suite) don't need to refactor
   * if we re-introduce a time-based cell later.
   */
  nowMs?: number;
}

/**
 * Two sibling queue panels (#2805 §1.3) — agent-ready and blocked. Each
 * panel is borderless with a subtle heading divider, and each row is a
 * `Link`-able hot link via `IssueLink`.
 *
 * Capped server-side at 10 entries per panel; more is dispatcher-internal
 * scheduling state, not useful on the overview page.
 *
 * Row density (#2818 — operator density pass): each row shows only
 *   `issue link + priority badge + title`.
 * The previous design also rendered `#<slot>` (rank) on the ready side and
 * `[N]` (blocked-by count) on the blocked side plus a `<N> d ago` filed-time
 * column — stripped because (a) row position IS the rank, (b) the blocked-by
 * tooltip belongs on the issue page, and (c) filed-time is never actionable
 * from this page. Titles get the freed horizontal space; the title cell
 * wraps onto a second line rather than ellipsis-truncating so the operator
 * can read the full text without hovering.
 *
 * Layout note (#2823 — state-flow two-column layout): the two sub-panels
 * are exposed as independent named exports (`QueueReadyPanel`,
 * `QueueBlockedPanel`) so the dashboard can place them in different
 * columns of the state-flow layout — ready sits bottom-left (below Active)
 * and blocked sits bottom-right (below Recently completed). The wrapping
 * `QueuePanel` component is retained for backwards compatibility with the
 * existing test suite but is no longer used by the dashboard itself.
 */
export function QueuePanel({
  queueReady,
  queueBlocked,
  queueReadyTotal,
  queueBlockedTotal,
}: QueuePanelProps) {
  return (
    <div
      className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2"
      data-testid="queue-panels"
    >
      <QueueReadyPanel items={queueReady} total={queueReadyTotal} />
      <QueueBlockedPanel items={queueBlocked} total={queueBlockedTotal} />
    </div>
  );
}

/**
 * Format the panel-header count badge. When `total` is provided we
 * render `{shown} / {total}` so the operator can see queue depth even
 * when the list is capped server-side at 10 (#2886). When it's omitted
 * we fall back to the pre-#2886 `{shown} shown` string so existing
 * tests and any caller that hasn't wired up `DispatcherState.queueDepth`
 * / `blockedDepth` still work.
 *
 * Edge case: when `total` is a number but `shown === 0`, render `0 / N`
 * (never "0 shown") — empty-panel UX stays obvious while still
 * communicating the denominator. This is the AC3 case in issue #2886
 * (blocked panel with tail truncation but all 10 visible items
 * cleared).
 */
function formatCountLabel(shown: number, total: number | undefined): string {
  if (typeof total === 'number') return `${shown} / ${total}`;
  return `${shown} shown`;
}

export function QueueReadyPanel({
  items,
  total,
}: {
  items: readonly QueueItem[];
  total?: number;
}) {
  return (
    <section aria-labelledby="queue-ready-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-ready-heading" className={SECTION_HEADING}>
          Queue: Agent-ready
        </h2>
        <span
          className="font-mono text-xs text-muted-foreground"
          data-testid="queue-ready-count"
        >
          {formatCountLabel(items.length, total)}
        </span>
      </div>
      {items.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          Queue empty — file an issue with <code className="font-mono">agent/ready</code> to seed.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((item) => (
            <QueueRow key={item.issueNumber} item={item} />
          ))}
        </ul>
      )}
    </section>
  );
}

export function QueueBlockedPanel({
  items,
  total,
}: {
  items: readonly QueueItem[];
  total?: number;
}) {
  return (
    <section aria-labelledby="queue-blocked-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-blocked-heading" className={SECTION_HEADING}>
          Queue: Blocked
        </h2>
        <span
          className="font-mono text-xs text-muted-foreground"
          data-testid="queue-blocked-count"
        >
          {formatCountLabel(items.length, total)}
        </span>
      </div>
      {items.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          No blocked issues.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((item) => (
            <QueueRow key={item.issueNumber} item={item} />
          ))}
        </ul>
      )}
    </section>
  );
}

function QueueRow({ item }: { item: QueueItem }) {
  return (
    <li className="flex items-start gap-3 py-2 text-sm hover:bg-muted/50">
      <div className="flex-shrink-0 pt-0.5">
        <IssueLink number={item.issueNumber} />
      </div>
      <div className="flex-shrink-0 pt-0.5">
        <PriorityBadge priority={item.priority} />
      </div>
      <div className="min-w-0 flex-1">
        <span
          className="block break-words text-foreground"
          data-testid="queue-row-title"
          title={item.title}
        >
          {item.title}
        </span>
      </div>
    </li>
  );
}
