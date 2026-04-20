'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { DispatcherFailure } from '@/lib/dispatcher-queries';
import { formatRelativeTime, groupFailuresByCategory } from './format-helpers';
import {
  INFRA_PREEMPTED_CATEGORIES,
  INFRA_PREEMPTED_CHIP_CLASSES,
  INFRA_PREEMPTED_GLYPH,
  IssueLink,
} from './ui-primitives';

interface RecentFailuresPanelProps {
  failures: readonly DispatcherFailure[];
  /** Override for deterministic tests. */
  nowMs?: number;
}

// #2947: red-✗ chip for non-infra-preempted failure categories. Shares
// the visual grammar with `OutcomePill`'s `failed` chip so the two
// panels read as a single outcome vocabulary. Infra-preempted rows
// (``daemon_restart_abandoned`` / ``paused_by_killswitch``) render the
// amber ↺ glyph instead (see ``INFRA_PREEMPTED_*`` imports above) —
// per operator direction, these rows DO still appear in the Recent
// failures roll-up (useful signal about dispatcher churn) but the
// different glyph+color makes it obvious at a glance that they aren't
// real operator action items.
const FAILURE_GLYPH = '\u2717';
const FAILURE_CHIP_CLASSES =
  'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300';

/**
 * Recent failures panel (#2805 §1.7) — same data, borderless. Category
 * renders as a muted pill; most-recent timestamp is a relative string
 * with the raw ISO on hover.
 *
 * #2947: adds a leading glyph column that keys off `category`. Infra-
 * preempted categories (``daemon_restart_abandoned`` /
 * ``paused_by_killswitch``) render ↺ amber; every other failure
 * category renders ✗ red. Matches the outcome-pill vocabulary in
 * ``RecentCompletionsPanel`` so the two panels read together.
 *
 * #2948: the Category column renders the operator-friendly
 * `displayCategory` string (e.g. "turn limit reached") instead of the
 * raw token (`subprocess_turn_limit`), matching the Recently Completed
 * tooltip rephrasing from #2935/#2939. The raw token is preserved on
 * the cell's `title` attribute and as a `data-category` attribute so
 * operators can still grab the exact string they need for CloudWatch
 * queries / SQL filters. Unknown categories (not in the server-side
 * display map) fall through to the raw token — the backend returns
 * `category` verbatim as `displayCategory` in that case, so the UI
 * doesn't need a fallback.
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
              <th scope="col" className="py-2 font-medium">
                {/* Glyph column — visually narrow, no header text. */}
                <span className="sr-only">Severity</span>
              </th>
              <th scope="col" className="py-2 font-medium">Category</th>
              <th scope="col" className="py-2 font-medium">Count</th>
              <th scope="col" className="py-2 font-medium">Most recent</th>
              <th scope="col" className="py-2 font-medium">Issue</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => {
              const infra = INFRA_PREEMPTED_CATEGORIES.has(group.category);
              const glyph = infra ? INFRA_PREEMPTED_GLYPH : FAILURE_GLYPH;
              const chipClasses = infra
                ? INFRA_PREEMPTED_CHIP_CLASSES
                : FAILURE_CHIP_CLASSES;
              const label = infra ? 'infra preempted' : 'failed';
              const testId = infra
                ? 'failure-glyph-infra_preempted'
                : 'failure-glyph-failed';
              return (
                <tr
                  key={group.category}
                  className="border-t border-border hover:bg-muted/50"
                >
                  <td className="py-2 pr-2">
                    <span
                      aria-label={label}
                      title={label}
                      className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs ${chipClasses}`}
                      data-testid={testId}
                    >
                      {glyph}
                    </span>
                  </td>
                  <td className="py-2">
                    <span
                      // #2948: title + data-category preserve the raw
                      // machine-readable token for operator debugging
                      // (CloudWatch queries, SQL filters, copy-paste)
                      // while the visible pill text shows the
                      // operator-friendly `displayCategory` string.
                      className="inline-flex items-center rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                      title={group.category}
                      data-category={group.category}
                      data-testid={`failure-category-pill-${group.category}`}
                    >
                      {group.mostRecent.displayCategory}
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
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
