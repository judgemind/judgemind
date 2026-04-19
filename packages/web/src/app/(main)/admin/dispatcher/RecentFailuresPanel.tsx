'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherFailure } from '@/lib/dispatcher-queries';
import { formatRelativeTime, groupFailuresByCategory } from './format-helpers';
import { IssueLink } from './ui-primitives';

interface RecentFailuresPanelProps {
  failures: readonly DispatcherFailure[];
  /** Override for deterministic tests. */
  nowMs?: number;
}

/**
 * Recent failures panel (#2805 §1.7) — same data, borderless. Category
 * renders as a muted pill; most-recent timestamp is a relative string
 * with the raw ISO on hover.
 */
export function RecentFailuresPanel({ failures, nowMs }: RecentFailuresPanelProps) {
  const groups = groupFailuresByCategory(failures);

  return (
    <section aria-labelledby="recent-failures-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="recent-failures-heading" className={SECTION_HEADING}>
          Recent failures (last 24h)
        </h2>
      </div>
      {groups.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          No failures in the last 24 hours.
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="py-2 font-medium">Category</th>
              <th scope="col" className="py-2 font-medium">Count</th>
              <th scope="col" className="py-2 font-medium">Most recent</th>
              <th scope="col" className="py-2 font-medium">Issue</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr
                key={group.category}
                className="border-t border-border hover:bg-muted/50"
              >
                <td className="py-2">
                  <span className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                    {group.category}
                  </span>
                </td>
                <td className="py-2 font-mono text-foreground">{group.count}</td>
                <td
                  className="py-2 text-xs text-muted-foreground"
                  title={group.mostRecent.ts}
                >
                  {formatRelativeTime(group.mostRecent.ts, nowMs)}
                </td>
                <td className="py-2">
                  {group.mostRecent.issueNumber !== null ? (
                    <IssueLink number={group.mostRecent.issueNumber} />
                  ) : (
                    <span className="text-muted-foreground">&mdash;</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
