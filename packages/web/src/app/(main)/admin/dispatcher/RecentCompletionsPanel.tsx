'use client';

import { SECTION_HEADING } from '@/lib/typography';
import type { RecentCompletion } from '@/lib/dispatcher-queries';
import { IssueLink, OutcomePill, PriorityBadge, PRLink } from './ui-primitives';

interface RecentCompletionsPanelProps {
  completions: readonly RecentCompletion[];
  /**
   * Override for deterministic tests. Used by the relative-time cell
   * (#2932) to render "Nm ago" / "Nh ago" / "N days ago" against a
   * fixed `now` in tests. Production callers omit this prop and the
   * row falls back to `Date.now()` at render time.
   */
  nowMs?: number;
}

/**
 * The "Recently completed" panel (#2805 §1.5). Newest terminal-state
 * agents (succeeded / failed / crashed / plan_blocked / needs_review) in
 * the operator's recent history. Each row links to the issue and, when
 * available, the PR. The correct-outcome and infra-preempted terminals
 * render with their own chips to keep them visually separated from
 * genuine failures:
 *
 * - `plan_blocked` (#2857) — neutral muted chip (⊘). Operator-
 *   informational; no action expected unless the issue needs reshaping.
 * - `needs_review` (#2856) — yellow chip (◐). DOES need operator action:
 *   a draft PR is open for review, ralph produced SHIP code but
 *   summary flagged unmet AC. The yellow is distinct from amber
 *   (`crashed`) so the operator can scan the panel and separate
 *   "review my draft PR" from "something crashed, diagnose this".
 * - *infra-preempted* (#2947) — amber chip (↺). A `failed` row whose
 *   `failureSummary` matches one of the canonical infra-preemption
 *   strings (``"dispatcher restarted"`` / ``"manually stopped"``).
 *   The dispatcher itself interrupted the agent; it will auto-resume
 *   on the next tick. NOT an operator action item.
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
 *
 * #2932 re-adds a relative-time cell **after** the cost footnote, with
 * better formatting than the original ("5m ago" / "4h ago" / "11 days
 * ago" / "2 months ago"). Native `title` attribute exposes the full
 * datetime in Pacific time on hover. Operator gets at-a-glance "when
 * did this happen" without clicking into the agent detail page.
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
  const costFootnote = formatCostFootnote(
    completion.totalCostUsd,
    completion.totalTokens,
  );
  // Resolve `now` at render. Tests pass a fixed `nowMs` to make the
  // relative-time cell deterministic; production callers omit the
  // prop so we fall back to `Date.now()`.
  const effectiveNowMs = nowMs ?? Date.now();
  const endedMs = Date.parse(completion.endedAt);
  const relativeTime = Number.isFinite(endedMs)
    ? formatRelativeTime(endedMs, effectiveNowMs)
    : null;
  const pacificTitle = Number.isFinite(endedMs)
    ? formatPacificDatetime(endedMs)
    : '';
  return (
    <li className="flex items-start gap-3 py-2 text-sm hover:bg-muted/50">
      <span className="flex-shrink-0 pt-0.5">
        {/* #2900: pass failureSummary through as hover tooltip for
            failure terminals. Null on succeeded / needs_review /
            pre-#2900 rows, in which case the pill falls back to its
            built-in status-label tooltip (prior behaviour).
            #2953: pass the four milestone columns through so the pill
            can render green vs. amber vs. red+milestone-breakdown. */}
        <OutcomePill
          status={completion.status}
          failureSummary={completion.failureSummary}
          mergedAt={completion.mergedAt}
          verifiedAt={completion.verifiedAt}
          verifySkipReason={completion.verifySkipReason}
          retroedAt={completion.retroedAt}
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
      {/* #2932 — abbreviated relative-time cell ("5m ago" / "4h ago" /
          "11 days ago"). Native `title` attribute shows the full datetime
          in Pacific time on hover, matching the OutcomePill / IssueLink
          tooltip pattern. Narrow fixed-width cell — abbreviated values
          are short (≤ ~11 chars).
          Positioned AFTER the title+cost span so a tall/wrapping title
          does not push the timestamp off the row. */}
      {relativeTime !== null && (
        <span
          className="flex-shrink-0 pt-0.5 text-xs text-muted-foreground tabular-nums"
          data-testid="completion-row-relative-time"
          title={pacificTitle}
        >
          {relativeTime}
        </span>
      )}
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

/** Render an abbreviated, age-scaled "N ago" string from two epoch-ms
 * values (#2932). Designed for the Recently Completed panel where the
 * operator wants at-a-glance freshness, not precise duration.
 *
 * Ranges (per the issue body):
 * - `< 60 seconds`: `Just now`
 *   (`0m ago` reads as "zero minutes", which is confusing — the spec
 *   allows either phrasing and "Just now" is the clearer one.)
 * - `< 60 minutes`: `Nm ago` (e.g. `5m ago`, `47m ago`)
 * - `1–23 hours`: `Nh ago`
 * - `1 day`: `1 day ago` (singular, no "s")
 * - `2–29 days`: `N days ago`
 * - `30–364 days`: `N months ago` (30-day months, singular for N=1)
 * - `≥ 365 days`: `N years ago` (365-day years, singular for N=1)
 *
 * Future timestamps (clock skew, test fixtures) collapse to `Just now`
 * rather than rendering a nonsensical negative-minutes string.
 */
function formatRelativeTime(timestampMs: number, nowMs: number): string {
  const diffMs = nowMs - timestampMs;
  // Future or < 1 second → "Just now" (graceful clock-skew handling).
  if (diffMs < 1000) return 'Just now';

  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return 'Just now';

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days === 1) return '1 day ago';
  if (days < 30) return `${days} days ago`;

  // ≥ 30 days — month/year bucketing using fixed 30-day months and
  // 365-day years. Operators use this for freshness scanning, not
  // calendar precision; the hover tooltip carries the exact datetime.
  if (days < 365) {
    const months = Math.floor(days / 30);
    return months === 1 ? '1 month ago' : `${months} months ago`;
  }
  const years = Math.floor(days / 365);
  return years === 1 ? '1 year ago' : `${years} years ago`;
}

/** Format an epoch-ms timestamp as a Pacific-time datetime string
 * (#2932) like `2026-04-20 15:36:39 PDT`. Used as the native `title`
 * attribute on the relative-time cell so operators get the exact time
 * on hover.
 *
 * Vanilla `Intl.DateTimeFormat` is used (no dayjs / moment / date-fns
 * — the web package has none of those and this component is the only
 * caller). `formatToParts` lets us assemble the YYYY-MM-DD HH:MM:SS
 * format the spec calls for without relying on a locale that happens
 * to produce the right shape.
 */
function formatPacificDatetime(timestampMs: number): string {
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  });
  const parts = formatter.formatToParts(new Date(timestampMs));
  const lookup: Record<string, string> = {};
  for (const part of parts) {
    if (part.type !== 'literal') lookup[part.type] = part.value;
  }
  // `hour12: false` can emit "24" for midnight on some engines —
  // normalize to "00" so the assembled string is unambiguous.
  const hour = lookup.hour === '24' ? '00' : (lookup.hour ?? '00');
  return (
    `${lookup.year}-${lookup.month}-${lookup.day} ` +
    `${hour}:${lookup.minute}:${lookup.second} ${lookup.timeZoneName}`
  );
}

// Exported for unit testing; the helpers stay co-located with the
// component because they are presentation-only and not reused elsewhere.
// See ``__tests__/RecentCompletionsPanel.test.tsx``.
export {
  formatCostFootnote,
  formatTokenCount,
  formatRelativeTime,
  formatPacificDatetime,
};
