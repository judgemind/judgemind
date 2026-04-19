'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherFailure } from '@/lib/dispatcher-queries';
import { groupFailuresByCategory } from './format-helpers';

interface RecentFailuresPanelProps {
  failures: readonly DispatcherFailure[];
}

export function RecentFailuresPanel({ failures }: RecentFailuresPanelProps) {
  const groups = groupFailuresByCategory(failures);

  return (
    <section
      aria-labelledby="recent-failures-heading"
      className="rounded-lg border border-border bg-card"
    >
      <div className="border-b border-border p-4">
        <h2 id="recent-failures-heading" className={SECTION_HEADING}>
          Recent failures (last 24h)
        </h2>
      </div>

      {groups.length === 0 ? (
        <p className="p-6 text-center text-sm text-muted-foreground">
          No failures in the last 24 hours.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
                <th scope="col" className="p-3">Category</th>
                <th scope="col" className="p-3">Count</th>
                <th scope="col" className="p-3">Most recent</th>
                <th scope="col" className="p-3">Issue</th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr
                  key={group.category}
                  className="border-b border-border last:border-b-0"
                >
                  <td className="p-3 font-mono text-xs text-foreground">
                    {group.category}
                  </td>
                  <td className="p-3 text-foreground">{group.count}</td>
                  <td className="p-3 text-xs text-muted-foreground">
                    {group.mostRecent.ts}
                  </td>
                  <td className="p-3 text-foreground">
                    {group.mostRecent.issueNumber !== null
                      ? `#${group.mostRecent.issueNumber}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
