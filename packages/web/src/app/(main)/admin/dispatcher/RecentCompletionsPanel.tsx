'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { RecentCompletion } from '@/lib/dispatcher-queries';
import { IssueLink, OutcomePill, PriorityBadge, PRLink } from './ui-primitives';

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
  const costFootnote = formatCostFootnote(
    completion.totalCostUsd,
    completion.totalTokens,
  );
  return (
    <li className="flex items-start gap-3 py-2 text-sm hover:bg-muted/50">
      <span className="flex-shrink-0 pt-0.5">
        {/* #2900: pass failureSummary through as hover tooltip for
            failure terminals. Null on succeeded / needs_review /
            pre-#2900 rows, in which case the pill falls back to its
            built-in status-label tooltip (prior behaviour). */}
        <OutcomePill
          status={completion.status}
          failureSummary={completion.failureSummary}
        />
      </span>
      {/* Unified (issue, priority, [pr,] title) prefix — matches
          ActiveAgentsTable and QueuePanel (#2899). */}
      <span className="flex-shrink-0 pt-0.5">
        <IssueLink number={completion.issueNumber} />
      </span>
      <span className="flex-shrink-0 pt-0.5">
        <PriorityBadge priority={completion.priority} />
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
        {costFootnote !== null && (
          <span
            className="ml-2 text-xs text-muted-foreground"
            data-testid="completion-row-cost"
            title="List-price token estimate; NOT Max-plan-adjusted actual spend."
          >
            {costFootnote}
          </span>
        )}
      </span>
    </li>
  );
}

/** Render a compact cost footnote ("~$0.42, 123k tok") from the parent
 * aggregate fields (#2869).
 *
 * Returns ``null`` when neither value is present — we render nothing at
 * all rather than "$0.00 / 0 tok" so operators can distinguish "cheap
 * agent" from "no metering signal on this row" (e.g. a pre-migration-31
 * agent, or every phase crashed before emitting a JSON envelope).
 *
 * Design choices:
 * - Cost is formatted with 2 decimals when ≥ $1, 4 decimals otherwise —
 *   a cheap haiku verify phase lands at ~$0.0008 and would round to
 *   "$0.00" under a blanket 2-decimal rule.
 * - Tokens are formatted "Nk" for brevity; shows "<1k" for the rare
 *   sub-1000-token agent so "0k" doesn't imply zero cost.
 * - The "~" on cost signals "estimate, not billed" per the caveat
 *   documented on `totalCostUsd`.
 */
function formatCostFootnote(
  costUsd: number | null,
  totalTokens: number | null,
): string | null {
  if (costUsd === null && totalTokens === null) return null;
  const parts: string[] = [];
  if (costUsd !== null) {
    const decimals = Math.abs(costUsd) >= 1 ? 2 : 4;
    parts.push(`~$${costUsd.toFixed(decimals)}`);
  }
  if (totalTokens !== null) {
    parts.push(formatTokenCount(totalTokens));
  }
  return `(${parts.join(', ')})`;
}

function formatTokenCount(tokens: number): string {
  if (tokens >= 1000) {
    const k = tokens / 1000;
    // >= 10k — drop the decimal; < 10k — keep one so "1.2k" reads better than "1k".
    return k >= 10 ? `${Math.round(k)}k tok` : `${k.toFixed(1)}k tok`;
  }
  return `${tokens} tok`;
}

// Exported for unit testing; the helpers stay co-located with the
// component because they are presentation-only and not reused elsewhere.
// See ``__tests__/RecentCompletionsPanel.test.tsx``.
export { formatCostFootnote, formatTokenCount };
