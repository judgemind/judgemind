'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { RecentCompletion } from '@/lib/dispatcher-queries';
import { formatRelativeTime } from './format-helpers';
import { IssueLink, OutcomePill, PRLink } from './ui-primitives';

interface RecentCompletionsPanelProps {
  completions: readonly RecentCompletion[];
  /** Override for deterministic tests. */
  nowMs?: number;
}

/**
 * The "Recently completed" panel (#2805 §1.5). Newest terminal-state
 * agents (succeeded / failed / crashed) in the operator's recent history.
 * Each row links to the issue and, when available, the PR.
 *
 * Borderless. Lives in the right column below Active agents.
 */
export function RecentCompletionsPanel({
  completions,
  nowMs,
}: RecentCompletionsPanelProps) {
  return (
    <section aria-labelledby="recent-completions-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="recent-completions-heading" className={SECTION_HEADING}>
          Recently completed ({completions.length})
        </h2>
      </div>
      {completions.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          No completed agents yet.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {completions.map((completion) => (
            <RecentCompletionRow
              key={completion.agentId}
              completion={completion}
              nowMs={nowMs}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function RecentCompletionRow({
  completion,
  nowMs,
}: {
  completion: RecentCompletion;
  nowMs?: number;
}) {
  const relative = formatRelativeTime(completion.endedAt, nowMs);
  return (
    <li className="flex items-center gap-3 py-2 text-sm hover:bg-muted/50">
      <span className="flex-shrink-0">
        <OutcomePill status={completion.status} />
      </span>
      <span className="flex-shrink-0">
        <IssueLink number={completion.issueNumber} />
      </span>
      <span
        className="min-w-0 flex-1 truncate text-foreground"
        title={completion.issueTitle ?? ''}
      >
        {completion.issueTitle ?? (
          <span className="italic text-muted-foreground">(title unavailable)</span>
        )}
      </span>
      <span className="w-20 flex-shrink-0 text-right text-xs">
        {completion.prNumber !== null ? (
          <span className="inline-flex items-center gap-0.5">
            <PRLink number={completion.prNumber} />
            <span aria-hidden="true" className="text-muted-foreground">&nearr;</span>
          </span>
        ) : (
          <span className="italic text-muted-foreground">(no PR)</span>
        )}
      </span>
      <span
        className="w-20 flex-shrink-0 text-right text-xs text-muted-foreground"
        title={completion.endedAt}
      >
        {relative}
      </span>
    </li>
  );
}
