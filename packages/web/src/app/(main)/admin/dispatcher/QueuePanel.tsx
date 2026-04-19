'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { QueueItem } from '@/lib/dispatcher-queries';
import { formatRelativeTime } from './format-helpers';
import { IssueLink, PriorityBadge } from './ui-primitives';

interface QueuePanelProps {
  /** Agent-ready issues from the latest `dispatcher.queue_snapshots`. */
  queueReady: readonly QueueItem[];
  /** Open issues labelled `status/blocked`. */
  queueBlocked: readonly QueueItem[];
  /** Override for deterministic tests. */
  nowMs?: number;
}

/**
 * Two sibling queue panels (#2805 §1.3) — agent-ready on the left,
 * blocked on the right. Each panel is borderless with a subtle heading
 * divider, and each row is a `Link`-able hot link via `IssueLink`.
 *
 * Capped server-side at 10 entries per panel; more is dispatcher-internal
 * scheduling state, not useful on the overview page.
 */
export function QueuePanel({ queueReady, queueBlocked, nowMs }: QueuePanelProps) {
  return (
    <div
      className="grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2"
      data-testid="queue-panels"
    >
      <QueueReadyPanel items={queueReady} nowMs={nowMs} />
      <QueueBlockedPanel items={queueBlocked} nowMs={nowMs} />
    </div>
  );
}

function QueueReadyPanel({
  items,
  nowMs,
}: {
  items: readonly QueueItem[];
  nowMs?: number;
}) {
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
          {items.map((item, index) => (
            <QueueRow
              key={item.issueNumber}
              item={item}
              slot={index + 1}
              nowMs={nowMs}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function QueueBlockedPanel({
  items,
  nowMs,
}: {
  items: readonly QueueItem[];
  nowMs?: number;
}) {
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
            <QueueRow
              key={item.issueNumber}
              item={item}
              nowMs={nowMs}
              blocked
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function QueueRow({
  item,
  slot,
  blocked,
  nowMs,
}: {
  item: QueueItem;
  slot?: number;
  blocked?: boolean;
  nowMs?: number;
}) {
  const timestamp = formatRelativeTime(item.createdAt, nowMs);
  return (
    <li className="flex items-center gap-3 py-2 text-sm hover:bg-muted/50">
      <div className="flex-shrink-0" data-testid="queue-row-number">
        <IssueLink number={item.issueNumber} />
      </div>
      <div className="min-w-0 flex-1">
        <span className="block truncate text-foreground" title={item.title}>
          {item.title}
        </span>
      </div>
      <div className="flex-shrink-0">
        <PriorityBadge priority={item.priority} />
      </div>
      <div
        className="w-10 flex-shrink-0 text-right font-mono text-xs text-muted-foreground"
        data-testid="queue-row-slot"
      >
        {blocked ? (
          item.blockedBy.length > 0 ? (
            <span
              className="cursor-help"
              title={item.blockedBy.map((n) => `#${n}`).join(', ')}
            >
              [{item.blockedBy.length}]
            </span>
          ) : (
            <span>—</span>
          )
        ) : slot !== undefined ? (
          <span>#{slot}</span>
        ) : (
          <span>—</span>
        )}
      </div>
      <div
        className="w-20 flex-shrink-0 text-right text-xs text-muted-foreground"
        title={item.createdAt}
      >
        {timestamp}
      </div>
    </li>
  );
}
