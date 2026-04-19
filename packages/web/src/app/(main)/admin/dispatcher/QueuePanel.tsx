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
   * Override for deterministic tests. Currently unused after #2818 stripped
   * the "filed N d ago" column — kept in the prop signature so existing
   * callers (`DispatcherDashboard`, the test suite) don't need to refactor
   * if we re-introduce a time-based cell later.
   */
  nowMs?: number;
}

/**
 * Two sibling queue panels (#2805 §1.3) — agent-ready on the left,
 * blocked on the right. Each panel is borderless with a subtle heading
 * divider, and each row is a `Link`-able hot link via `IssueLink`.
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
 */
export function QueuePanel({ queueReady, queueBlocked }: QueuePanelProps) {
  return (
    <div
      className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2"
      data-testid="queue-panels"
    >
      <QueueReadyPanel items={queueReady} />
      <QueueBlockedPanel items={queueBlocked} />
    </div>
  );
}

function QueueReadyPanel({ items }: { items: readonly QueueItem[] }) {
  return (
    <section aria-labelledby="queue-ready-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-ready-heading" className={SECTION_HEADING}>
          Queue: Agent-ready
        </h2>
        <span className="font-mono text-xs text-muted-foreground">
          {items.length} shown
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

function QueueBlockedPanel({ items }: { items: readonly QueueItem[] }) {
  return (
    <section aria-labelledby="queue-blocked-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-blocked-heading" className={SECTION_HEADING}>
          Queue: Blocked
        </h2>
        <span className="font-mono text-xs text-muted-foreground">
          {items.length} shown
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
