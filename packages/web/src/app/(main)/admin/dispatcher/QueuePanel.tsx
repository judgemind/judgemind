'use client';

import type { CSSProperties } from 'react';
import { SECTION_HEADING } from '@/lib/typography';
import type { BlockerRef, QueueItem } from '@/lib/dispatcher-queries';
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

/**
 * Render the count label as either a static `<span>` (back-compat) or a
 * clickable `<button>` when `onClick` is provided. The button-mode keeps
 * the same font-mono micro-typography as the original span — operators
 * scanning the panel header should not have to relearn the visual
 * shape of the count. We only add an underline-on-hover affordance and
 * a pointer cursor so the click target reads as actionable. Issue #3159.
 */
function CountLabel({
  shown,
  total,
  testId,
  onClick,
  ariaLabel,
}: {
  shown: number;
  total: number | undefined;
  testId: string;
  onClick?: () => void;
  ariaLabel?: string;
}) {
  const text = formatCountLabel(shown, total);
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        data-testid={testId}
        aria-label={ariaLabel ?? `${text} — open full list`}
        className="font-mono text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 rounded-sm cursor-pointer"
      >
        {text}
      </button>
    );
  }
  return (
    <span
      className="font-mono text-xs text-muted-foreground"
      data-testid={testId}
    >
      {text}
    </span>
  );
}

export function QueueReadyPanel({
  items,
  total,
  onCountClick,
  dialogOpen = false,
}: {
  items: readonly QueueItem[];
  total?: number;
  /**
   * When provided, the count label becomes a clickable button that
   * triggers this callback. The cockpit dashboard wires this to open
   * the full-list dialog (issue #3159). When omitted, the count
   * renders as a static span — preserves back-compat for tests and any
   * caller that hasn't wired up the dialog.
   */
  onCountClick?: () => void;
  /**
   * When true, suppress the Magic Move ``view-transition-name`` on each
   * row so the cockpit's poll-driven view-transitions (#2967) don't
   * paint browser-managed ``::view-transition-*`` pseudo-elements
   * directly over an open full-list dialog (#3164/#3178). View-transition
   * pseudos render on a compositor layer that sits above the regular
   * z-index stack, so Radix Dialog's ``z-50`` content can't out-stack
   * them — the only fix is to suppress the transition trigger while a
   * dialog is open. The cockpit dashboard threads this from
   * ``fullDialogKind !== null``. Issue #3206.
   */
  dialogOpen?: boolean;
}) {
  return (
    <section aria-labelledby="queue-ready-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-ready-heading" className={SECTION_HEADING}>
          Queue: Agent-ready
        </h2>
        <CountLabel
          shown={items.length}
          total={total}
          testId="queue-ready-count"
          onClick={onCountClick}
          ariaLabel={
            onCountClick
              ? `Show full agent-ready queue (${formatCountLabel(items.length, total)})`
              : undefined
          }
        />
      </div>
      {items.length === 0 ? (
        <p className="py-2 text-sm text-muted-foreground">
          Queue empty — file an issue with <code className="font-mono">agent/ready</code> to seed.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {items.map((item) => (
            <QueueRow
              key={item.issueNumber}
              item={item}
              animated={!dialogOpen}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

export function QueueBlockedPanel({
  items,
  total,
  onCountClick,
}: {
  items: readonly QueueItem[];
  total?: number;
  /** See `QueueReadyPanel.onCountClick`. Issue #3159. */
  onCountClick?: () => void;
}) {
  return (
    <section aria-labelledby="queue-blocked-heading">
      <div className="flex items-center justify-between border-b border-border pb-2 mb-2">
        <h2 id="queue-blocked-heading" className={SECTION_HEADING}>
          Queue: Blocked
        </h2>
        <CountLabel
          shown={items.length}
          total={total}
          testId="queue-blocked-count"
          onClick={onCountClick}
          ariaLabel={
            onCountClick
              ? `Show full blocked queue (${formatCountLabel(items.length, total)})`
              : undefined
          }
        />
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

/**
 * Format a cooldown duration into a short human-readable string for the
 * operator. Rough buckets — second-precision is unnecessary on the admin
 * panel; the operator needs to know whether to wait seconds, minutes, etc.
 *
 * Examples: formatCooldown(3) → '3s', formatCooldown(42) → '42s',
 *           formatCooldown(300) → '5m', formatCooldown(3540) → '59m'
 */
export function formatCooldown(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m`;
}

/**
 * Build the native `title` tooltip string for the `#NNNN` IssueLink on a
 * blocked queue row. Issue #2989.
 *
 * Format:
 * ```
 * Blocked by:
 *   #42 The blocker title
 *   #100 Another blocker
 *   (+M more)
 * ```
 *
 * Rules:
 * - Header line is always `Blocked by:`.
 * - Each blocker gets its own line: `#N title` when title is known, or
 *   `#N` alone when `title` is null.
 * - Individual titles are capped at 80 characters with a `…` ellipsis.
 * - At most 6 entries are shown; when truncated, a `(+M more)` line is
 *   appended.
 * - Entries are separated by `\n` — modern browsers render newlines in
 *   native `title` attributes.
 *
 * Pure function — safe to call during render.
 */
export function formatBlockerTooltip(refs: BlockerRef[]): string {
  const MAX_ENTRIES = 6;
  const MAX_TITLE_CHARS = 80;

  const lines: string[] = ['Blocked by:'];
  const visible = refs.slice(0, MAX_ENTRIES);
  for (const ref of visible) {
    if (ref.title !== null && ref.title !== undefined) {
      const truncated =
        ref.title.length > MAX_TITLE_CHARS
          ? `${ref.title.slice(0, MAX_TITLE_CHARS)}\u2026`
          : ref.title;
      lines.push(`  #${ref.number} ${truncated}`);
    } else {
      lines.push(`  #${ref.number}`);
    }
  }
  const remaining = refs.length - MAX_ENTRIES;
  if (remaining > 0) {
    lines.push(`  (+${remaining} more)`);
  }
  return lines.join('\n');
}

/**
 * One row in the queue panel. Exported so the full-list dialog
 * (`QueueFullDialog`, issue #3159) can render the same row layout
 * without duplicating the markup.
 */
export function QueueRow({
  item,
  animated = false,
}: {
  item: QueueItem;
  /**
   * When true, apply a stable ``view-transition-name`` keyed on the
   * issue number so the row can Magic Move between the Agent-ready
   * queue and the Active agents panel (#2967). Only the ready panel
   * passes `animated`; the Blocked panel is a stable sidebar that is
   * not part of the Queue → Active → Completed flow.
   */
  animated?: boolean;
}) {
  // CSS's `view-transition-name` property isn't in the standard React
  // `CSSProperties` type yet (the project pins `@types/react` 18), so
  // we splice it in via a narrow cast rather than widening the global
  // `CSSProperties` declaration. The browser and jsdom both accept the
  // kebab-case key on the `style` attribute; React happily camelCases
  // it at runtime.
  const rowStyle = animated
    ? ({ viewTransitionName: `issue-${item.issueNumber}` } as CSSProperties)
    : undefined;
  const cooldown =
    typeof item.cooldownSecondsRemaining === 'number' &&
    item.cooldownSecondsRemaining > 0
      ? item.cooldownSecondsRemaining
      : null;
  return (
    <li
      className="flex items-start gap-3 py-2 text-sm hover:bg-muted/50"
      style={rowStyle}
      data-testid={animated ? `queue-row-${item.issueNumber}` : undefined}
    >
      <div className="flex-shrink-0 pt-0.5">
        <IssueLink
          number={item.issueNumber}
          title={
            item.blockedBy.length > 0
              ? formatBlockerTooltip(item.blockedBy)
              : undefined
          }
        />
      </div>
      <div className="flex-shrink-0 pt-0.5">
        <PriorityBadge priority={item.priority} />
      </div>
      {cooldown !== null && (
        <span
          className="flex-shrink-0 pt-0.5 font-mono text-xs text-muted-foreground"
          data-testid={`cooldown-badge-${item.issueNumber}`}
          title={`${cooldown}s cooldown remaining`}
        >
          {formatCooldown(cooldown)} cooldown
        </span>
      )}
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
