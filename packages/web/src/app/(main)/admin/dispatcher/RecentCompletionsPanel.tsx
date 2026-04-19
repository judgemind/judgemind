'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { RecentCompletion } from '@/lib/dispatcher-queries';
import { IssueLink, OutcomePill, PRLink } from './ui-primitives';

interface RecentCompletionsPanelProps {
  completions: readonly RecentCompletion[];
  /**
   * Override for deterministic tests. Currently unused after #2818 stripped
   * the "N min ago" column — kept in the prop signature so callers
   * (`DispatcherDashboard`) don't need a refactor if we re-introduce a
   * time-based cell later.
   */
  nowMs?: number;
}

/**
 * The "Recently completed" panel (#2805 §1.5). Newest terminal-state
 * agents (succeeded / failed / crashed / plan_blocked / needs_review) in
 * the operator's recent history. Each row links to the issue and, when
 * available, the PR. The two correct-outcome triage terminals render
 * with their own chips to keep them visually separated from genuine
 * failures:
 *
 * - `plan_blocked` (#2857) — neutral muted chip (⊘). Operator-
 *   informational; no action expected unless the issue needs reshaping.
 * - `needs_review` (#2856) — yellow chip (◐). DOES need operator action:
 *   a draft PR is open for review, ralph produced SHIP code but
 *   summary flagged unmet AC. The yellow is distinct from amber
 *   (`crashed`) so the operator can scan the panel and separate
 *   "review my draft PR" from "something crashed, diagnose this".
 *
 * Borderless. Lives in the right column below Active agents.
 *
 * Row density (#2818 — operator density pass): each row shows only
 *   `outcome glyph + issue link + optional PR link + title`.
 * Dropped the "N min/h/d ago" completion-time column (never actionable
 * from this page — the agent is already done) and the "(no PR)" filler
 * when `prNumber` is null — empty space is less noisy than italic copy.
 * The title cell wraps onto a second line rather than ellipsis-truncating
 * so the full text is always readable without hover.
 */
export function RecentCompletionsPanel({
  completions,
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
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function RecentCompletionRow({ completion }: { completion: RecentCompletion }) {
  return (
    <li className="flex items-start gap-3 py-2 text-sm hover:bg-muted/50">
      <span className="flex-shrink-0 pt-0.5">
        <OutcomePill status={completion.status} />
      </span>
      <span className="flex-shrink-0 pt-0.5">
        <IssueLink number={completion.issueNumber} />
      </span>
      {completion.prNumber !== null && (
        <span className="flex-shrink-0 pt-0.5">
          <PRLink number={completion.prNumber} />
        </span>
      )}
      <span
        className="min-w-0 flex-1 break-words text-foreground"
        data-testid="completion-row-title"
        title={completion.issueTitle ?? ''}
      >
        {completion.issueTitle ?? (
          <span className="italic text-muted-foreground">(title unavailable)</span>
        )}
      </span>
    </li>
  );
}
