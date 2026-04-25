import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { RenderOptions } from '@testing-library/react';
import type { ReactElement } from 'react';
import { QueueBlockedPanel, QueuePanel, QueueReadyPanel, QueueRow, formatBlockerTooltip } from '../QueuePanel';
import { formatCooldown } from '../QueuePanel';
import type { QueueItem } from '@/lib/dispatcher-queries';
import type { BlockerRef } from '@/lib/dispatcher-queries';
import { TooltipProvider } from '@/components/ui/tooltip';

const now = Date.parse('2026-04-18T12:00:00Z');

/**
 * Render wrapper that includes `TooltipProvider` — required because `QueueRow`
 * uses Radix `<Tooltip>` on blocked rows (#2812) and Radix requires the
 * provider in the tree.
 */
function renderWithTooltip(ui: ReactElement, options?: RenderOptions) {
  return render(<TooltipProvider>{ui}</TooltipProvider>, options);
}

function item(overrides: Partial<QueueItem>): QueueItem {
  return {
    issueNumber: 1,
    title: 'Example issue',
    priority: 'p1',
    labels: ['priority/p1', 'agent/ready'],
    createdAt: '2026-04-18T11:00:00Z',
    blockedBy: [],
    cooldownSecondsRemaining: null,
    ...overrides,
  };
}

/** Build a BlockerRef for tests. */
function blocker(number: number, title: string | null = null): BlockerRef {
  return { number, title };
}

describe('QueuePanel', () => {
  it('renders empty states for both panels when no items', () => {
    renderWithTooltip(<QueuePanel queueReady={[]} queueBlocked={[]} nowMs={now} />);
    expect(screen.getByText(/queue empty/i)).toBeInTheDocument();
    expect(screen.getByText(/no blocked issues/i)).toBeInTheDocument();
  });

  it('renders agent-ready rows with hot-linked issue numbers, priority badges, and full titles', () => {
    const items = [
      item({ issueNumber: 2801, title: 'wire dispatcherControl through to daemon' }),
      item({ issueNumber: 2802, title: 'recently completed panel', priority: 'p2' }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.getByTestId('issue-link-2801')).toHaveAttribute(
      'href',
      'https://github.com/judgemind/judgemind/issues/2801',
    );
    expect(screen.getByTestId('issue-link-2802')).toBeInTheDocument();
    // Priority badges present.
    expect(screen.getByTestId('priority-badge-p1')).toBeInTheDocument();
    expect(screen.getByTestId('priority-badge-p2')).toBeInTheDocument();
    // Titles render in full.
    expect(screen.getByText('wire dispatcherControl through to daemon')).toBeInTheDocument();
    expect(screen.getByText('recently completed panel')).toBeInTheDocument();
  });

  it('renders blocked rows with the issue link and title only', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [blocker(2500), blocker(2600)] }),
    ];
    renderWithTooltip(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    expect(screen.getByTestId('issue-link-2500')).toBeInTheDocument();
    expect(screen.getByText('blocked issue')).toBeInTheDocument();
  });

  // --- #2818 — density pass: rank / blocked-count / filed-time columns removed.
  it('#2818: strips the #<rank> column from the ready side', () => {
    const items = [
      item({ issueNumber: 2801, title: 'first' }),
      item({ issueNumber: 2802, title: 'second' }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByText('#1')).not.toBeInTheDocument();
    expect(screen.queryByText('#2')).not.toBeInTheDocument();
    expect(screen.queryAllByTestId('queue-row-slot')).toHaveLength(0);
  });

  it('#2818: strips the [N] blocked-by count column from the blocked side', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [blocker(2500), blocker(2600)] }),
    ];
    renderWithTooltip(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    expect(screen.queryByText('[2]')).not.toBeInTheDocument();
  });

  it('#2818: does not render a "N d ago" filed-time column', () => {
    const items = [
      item({
        issueNumber: 2801,
        title: 'first',
        createdAt: '2026-04-16T11:00:00Z', // 2 d ago from the fixed `now`
      }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByText(/\bago\b/i)).not.toBeInTheDocument();
  });

  it('#2818: renders long titles in full without ellipsis truncation', () => {
    const longTitle =
      'fix(admin-dispatcher): (title unavailable) everywhere + 20562-day-ago timestamps + strip wasteful columns';
    const items = [item({ issueNumber: 2818, title: longTitle })];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    const titleEl = screen.getByText(longTitle);
    // Title cell uses `break-words` (soft wrap) rather than `truncate`.
    expect(titleEl.className).toMatch(/break-words/);
    expect(titleEl.className).not.toMatch(/\btruncate\b/);
  });

  // --- #2886 — panel header renders `{shown} / {total}` so the operator
  //             can see queue depth even when the displayed list is capped
  //             server-side at 10.
  it('#2886: ready panel header renders `{shown} / {total}` when total provided', () => {
    const items = [
      item({ issueNumber: 2801, title: 'first' }),
      item({ issueNumber: 2802, title: 'second' }),
    ];
    renderWithTooltip(
      <QueuePanel
        queueReady={items}
        queueBlocked={[]}
        queueReadyTotal={82}
        queueBlockedTotal={0}
        nowMs={now}
      />,
    );
    expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('2 / 82');
    // Legacy "N shown" label must NOT render on the ready panel when a
    // total is supplied — the whole point of #2886 is to replace that
    // ambiguous label.
    expect(screen.getByTestId('queue-ready-count')).not.toHaveTextContent(/shown/);
  });

  it('#2886: blocked panel header renders `{shown} / {total}` when total provided', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [blocker(2500), blocker(2600)] }),
    ];
    renderWithTooltip(
      <QueuePanel
        queueReady={[]}
        queueBlocked={blocked}
        queueBlockedTotal={15}
        nowMs={now}
      />,
    );
    expect(screen.getByTestId('queue-blocked-count')).toHaveTextContent('1 / 15');
  });

  it('#2886: header renders `0 / N` when the list is empty but total > 0 (AC3)', () => {
    // AC3 — blocked panel with nothing visible but total non-zero. In
    // practice this shouldn't happen for the ready panel (cap-10 with
    // total>0 always yields at least 1 shown), but the blocked-panel
    // empty-with-total-nonzero case is explicitly called out in the
    // issue. The component MUST still render `0 / N`, never "0 shown".
    renderWithTooltip(
      <QueuePanel
        queueReady={[]}
        queueBlocked={[]}
        queueReadyTotal={82}
        queueBlockedTotal={5}
        nowMs={now}
      />,
    );
    expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('0 / 82');
    expect(screen.getByTestId('queue-blocked-count')).toHaveTextContent('0 / 5');
  });

  it('#2886: falls back to `{shown} shown` when total prop is omitted (back-compat)', () => {
    // Existing callers that haven't wired up `queueDepth` / `blockedDepth`
    // must keep working. Dropping the denominator entirely when we don't
    // have a total is strictly safer than guessing — total === shown
    // would be a lie when the tail is capped.
    const items = [item({ issueNumber: 2801, title: 'first' })];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.getByTestId('queue-ready-count')).toHaveTextContent('1 shown');
    expect(screen.getByTestId('queue-blocked-count')).toHaveTextContent('0 shown');
  });

  // --- #2967 — Magic Move `view-transition-name` on ready-panel rows.
  //             The blocked-panel rows intentionally do NOT get a name
  //             because they are not part of the Queue → Active flow
  //             and would collide with the Active outer on transitions
  //             between ready and blocked labels.
  it('#2967: ready panel rows carry view-transition-name: issue-<N>', () => {
    const items = [
      item({ issueNumber: 2801, title: 'first' }),
      item({ issueNumber: 2802, title: 'second' }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    const row1 = screen.getByTestId('queue-row-2801');
    const row2 = screen.getByTestId('queue-row-2802');
    expect((row1 as HTMLElement).style.viewTransitionName).toBe('issue-2801');
    expect((row2 as HTMLElement).style.viewTransitionName).toBe('issue-2802');
  });

  it('#2967: blocked panel rows do NOT carry a view-transition-name', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue' }),
    ];
    renderWithTooltip(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    // Blocked rows are intentionally not animated — they don't show up
    // in the Queue → Active → Completed flow, and giving them the same
    // `issue-<N>` name as a ready row of the same issue number would
    // cause a DOM collision the next time an issue flips between the
    // two label states. We assert via the absence of any
    // `queue-row-<N>` test id — the animated-row testid is only set on
    // ready rows.
    expect(screen.queryByTestId('queue-row-2500')).toBeNull();
  });

  // --- #3206 — view-transition gate while a full-list dialog is open.
  //             The cockpit's `dispatcherState` poll fires
  //             `document.startViewTransition` every 2s; the browser
  //             paints `::view-transition-*` pseudo-elements on a
  //             compositor layer above the regular z-index stack, so
  //             any row carrying a `viewTransitionName` would render
  //             over the open dialog. The fix is to suppress the name
  //             on every row whenever `dialogOpen` is true.
  it('#3206: ready rows DROP view-transition-name when dialogOpen=true', () => {
    const items = [
      item({ issueNumber: 3151, title: 'first' }),
      item({ issueNumber: 3152, title: 'second' }),
    ];
    renderWithTooltip(<QueueReadyPanel items={items} total={50} dialogOpen />);
    // Rows still render — only the animation hook is suppressed.
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(screen.getByText('second')).toBeInTheDocument();
    // Non-animated rows omit the testid (per QueueRow's own contract);
    // assert absence of the animated-row testid.
    expect(screen.queryByTestId('queue-row-3151')).toBeNull();
    expect(screen.queryByTestId('queue-row-3152')).toBeNull();
  });

  it('#3206: ready rows KEEP view-transition-name when dialogOpen=false (default)', () => {
    const items = [item({ issueNumber: 3151, title: 'first' })];
    renderWithTooltip(<QueueReadyPanel items={items} total={50} />);
    const row = screen.getByTestId('queue-row-3151');
    expect((row as HTMLElement).style.viewTransitionName).toBe('issue-3151');
  });

  it('#3206: dialogOpen=true also strips view-transition-name when explicit', () => {
    const items = [item({ issueNumber: 3151, title: 'first' })];
    renderWithTooltip(<QueueReadyPanel items={items} total={50} dialogOpen={true} />);
    // No queue-row-<N> testid → no view-transition-name in style.
    expect(screen.queryByTestId('queue-row-3151')).toBeNull();
  });

  // --- #3001 — cooldown badge on queue-ready rows.
  it('#3001: renders cooldown badge when cooldownSecondsRemaining > 0', () => {
    const items = [
      item({ issueNumber: 3001, title: 'cooled down issue', cooldownSecondsRemaining: 1800 }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    const badge = screen.getByTestId('cooldown-badge-3001');
    expect(badge).toBeInTheDocument();
    // 1800s → 30m
    expect(badge).toHaveTextContent('30m cooldown');
  });

  it('#3001: does NOT render cooldown badge when cooldownSecondsRemaining is null', () => {
    const items = [
      item({ issueNumber: 3002, title: 'fresh issue', cooldownSecondsRemaining: null }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByTestId('cooldown-badge-3002')).toBeNull();
  });

  it('#3001: does NOT render cooldown badge when cooldownSecondsRemaining is 0', () => {
    const items = [
      item({ issueNumber: 3003, title: 'expired cooldown', cooldownSecondsRemaining: 0 }),
    ];
    renderWithTooltip(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByTestId('cooldown-badge-3003')).toBeNull();
  });
});

// --- #3159 — clickable count opens the full-list dialog. The count
//             label becomes a `<button>` when `onCountClick` is provided,
//             and falls back to a static `<span>` otherwise (back-compat).
describe('QueuePanel — #3159 clickable count', () => {
  const items = [
    item({ issueNumber: 2801, title: 'first' }),
    item({ issueNumber: 2802, title: 'second' }),
  ];

  it('ready: count renders as a span when onCountClick is omitted (back-compat)', () => {
    renderWithTooltip(<QueueReadyPanel items={items} total={50} />);
    const count = screen.getByTestId('queue-ready-count');
    expect(count.tagName).toBe('SPAN');
  });

  it('ready: count renders as a button and fires onCountClick when provided', () => {
    const onCountClick = vi.fn();
    renderWithTooltip(
      <QueueReadyPanel
        items={items}
        total={50}
        onCountClick={onCountClick}
      />,
    );
    const count = screen.getByTestId('queue-ready-count');
    expect(count.tagName).toBe('BUTTON');
    expect(count).toHaveTextContent('2 / 50');
    fireEvent.click(count);
    expect(onCountClick).toHaveBeenCalledTimes(1);
  });

  it('blocked: count renders as a span when onCountClick is omitted (back-compat)', () => {
    renderWithTooltip(<QueueBlockedPanel items={[]} total={5} />);
    const count = screen.getByTestId('queue-blocked-count');
    expect(count.tagName).toBe('SPAN');
  });

  it('blocked: count renders as a button and fires onCountClick when provided', () => {
    const onCountClick = vi.fn();
    renderWithTooltip(
      <QueueBlockedPanel
        items={items}
        total={50}
        onCountClick={onCountClick}
      />,
    );
    const count = screen.getByTestId('queue-blocked-count');
    expect(count.tagName).toBe('BUTTON');
    fireEvent.click(count);
    expect(onCountClick).toHaveBeenCalledTimes(1);
  });

  it('button has accessible aria-label that names the bucket', () => {
    const onCountClick = vi.fn();
    renderWithTooltip(
      <QueueReadyPanel
        items={items}
        total={50}
        onCountClick={onCountClick}
      />,
    );
    const count = screen.getByTestId('queue-ready-count');
    expect(count.getAttribute('aria-label')).toMatch(/agent-ready/i);
  });
});

// --- #2989 — native title tooltip on IssueLink for blocked rows.
// Replaces the #2812 Radix Tooltip approach. The tooltip is now a native
// browser `title` attribute on the `#NNNN` IssueLink.
describe('QueuePanel — #2989 blocker native title tooltip', () => {
  it('AC5: IssueLink for a blocked row carries a title starting with "Blocked by:"', () => {
    const blockedItem: QueueItem = {
      issueNumber: 9001,
      title: 'blocked issue with two blockers',
      priority: 'p2',
      labels: ['priority/p2', 'status/blocked'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [
        blocker(2500, 'The first blocker'),
        blocker(2600, 'The second blocker'),
      ],
      cooldownSecondsRemaining: null,
    };
    render(
      <TooltipProvider>
        <QueueRow item={blockedItem} />
      </TooltipProvider>,
    );
    const link = screen.getByTestId('issue-link-9001');
    const titleAttr = link.getAttribute('title');
    expect(titleAttr).toBeTruthy();
    expect(titleAttr).toMatch(/^Blocked by:/);
  });

  it('AC5: tooltip includes both blocker numbers and titles', () => {
    const blockedItem: QueueItem = {
      issueNumber: 9001,
      title: 'blocked issue',
      priority: 'p2',
      labels: ['priority/p2', 'status/blocked'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [
        blocker(2500, 'The first blocker'),
        blocker(2600, 'The second blocker'),
      ],
      cooldownSecondsRemaining: null,
    };
    render(
      <TooltipProvider>
        <QueueRow item={blockedItem} />
      </TooltipProvider>,
    );
    const link = screen.getByTestId('issue-link-9001');
    const titleAttr = link.getAttribute('title') ?? '';
    expect(titleAttr).toContain('2500');
    expect(titleAttr).toContain('The first blocker');
    expect(titleAttr).toContain('2600');
    expect(titleAttr).toContain('The second blocker');
  });

  it('AC6: tooltip falls back to #N alone when a blocker title is null', () => {
    const blockedItem: QueueItem = {
      issueNumber: 9003,
      title: 'blocked issue with null title',
      priority: 'p2',
      labels: ['priority/p2', 'status/blocked'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [blocker(42, null)],
      cooldownSecondsRemaining: null,
    };
    render(
      <TooltipProvider>
        <QueueRow item={blockedItem} />
      </TooltipProvider>,
    );
    const link = screen.getByTestId('issue-link-9003');
    const titleAttr = link.getAttribute('title') ?? '';
    // For null title the line must be "#42" only (no extra text after it).
    expect(titleAttr).toContain('#42');
    // Must NOT contain stray "null" string.
    expect(titleAttr).not.toContain('null');
  });

  it('AC7: ready row IssueLink has no title attribute (no blockers)', () => {
    const readyItem: QueueItem = {
      issueNumber: 9002,
      title: 'ready issue no blockers',
      priority: 'p2',
      labels: ['priority/p2', 'agent/ready'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [],
      cooldownSecondsRemaining: null,
    };
    render(
      <TooltipProvider>
        <QueueRow item={readyItem} />
      </TooltipProvider>,
    );
    const link = screen.getByTestId('issue-link-9002');
    // Ready rows have no blocker tooltip.
    expect(link.getAttribute('title')).toBeNull();
  });

  it('no Radix blocker-tooltip-trigger on blocked rows (replaced by native title)', () => {
    const blockedItem: QueueItem = {
      issueNumber: 9004,
      title: 'blocked issue',
      priority: 'p2',
      labels: ['priority/p2', 'status/blocked'],
      createdAt: '2026-04-18T11:00:00Z',
      blockedBy: [blocker(42, 'Some title')],
      cooldownSecondsRemaining: null,
    };
    render(
      <TooltipProvider>
        <QueueRow item={blockedItem} />
      </TooltipProvider>,
    );
    // The old Radix trigger testid must be gone — replaced by native title.
    expect(screen.queryByTestId('blocker-tooltip-trigger-9004')).toBeNull();
  });
});

describe('formatCooldown', () => {
  it('formats seconds less than 60 as Xs', () => {
    expect(formatCooldown(3)).toBe('3s');
    expect(formatCooldown(42)).toBe('42s');
    expect(formatCooldown(59)).toBe('59s');
  });

  it('formats seconds >= 60 as Nm (floor division)', () => {
    expect(formatCooldown(60)).toBe('1m');
    expect(formatCooldown(300)).toBe('5m');
    expect(formatCooldown(3540)).toBe('59m');
    expect(formatCooldown(3600)).toBe('60m');
  });
});

// ---------------------------------------------------------------------------
// formatBlockerTooltip — pure helper (issue #2989)
// ---------------------------------------------------------------------------
describe('formatBlockerTooltip', () => {
  it('returns a string starting with "Blocked by:"', () => {
    const refs: BlockerRef[] = [blocker(42, 'Do the thing')];
    expect(formatBlockerTooltip(refs)).toMatch(/^Blocked by:/);
  });

  it('renders each blocker on its own line with "#N title"', () => {
    const refs: BlockerRef[] = [
      blocker(42, 'First blocker'),
      blocker(100, 'Second blocker'),
    ];
    const result = formatBlockerTooltip(refs);
    const lines = result.split('\n');
    // First line is "Blocked by:"
    expect(lines[0]).toBe('Blocked by:');
    expect(lines).toContainEqual(expect.stringContaining('#42'));
    expect(lines).toContainEqual(expect.stringContaining('First blocker'));
    expect(lines).toContainEqual(expect.stringContaining('#100'));
    expect(lines).toContainEqual(expect.stringContaining('Second blocker'));
  });

  it('AC6: null title renders as "#N" only with no stray "null"', () => {
    const refs: BlockerRef[] = [blocker(42, null)];
    const result = formatBlockerTooltip(refs);
    expect(result).toContain('#42');
    expect(result).not.toContain('null');
  });

  it('truncates at 6 entries and appends "(+M more)"', () => {
    const refs: BlockerRef[] = Array.from({ length: 9 }, (_, i) =>
      blocker(100 + i, `Title ${i}`),
    );
    const result = formatBlockerTooltip(refs);
    // 9 blockers → show 6, append "(+3 more)"
    expect(result).toContain('(+3 more)');
    // Entries 7-9 (numbers 106-108) must NOT appear.
    expect(result).not.toContain('#106');
    expect(result).not.toContain('#107');
    expect(result).not.toContain('#108');
  });

  it('does NOT append "(+M more)" when 6 or fewer', () => {
    const refs: BlockerRef[] = Array.from({ length: 6 }, (_, i) =>
      blocker(100 + i, `Title ${i}`),
    );
    const result = formatBlockerTooltip(refs);
    expect(result).not.toContain('more');
  });

  it('caps individual title at ~80 chars with ellipsis', () => {
    const longTitle = 'A'.repeat(90);
    const refs: BlockerRef[] = [blocker(42, longTitle)];
    const result = formatBlockerTooltip(refs);
    // Title must be truncated; ellipsis appended.
    expect(result).toContain('\u2026');
    // The line with the blocker must not contain the full 90-char title.
    const lines = result.split('\n');
    const blockerLine = lines.find((l) => l.includes('#42')) ?? '';
    // Line format: "  #42 <title>" — check that the title portion is capped.
    // Strip leading spaces and "#42 " to isolate the title portion.
    const titlePart = blockerLine.trimStart().replace(/^#\d+\s/, '');
    // Title should be at most 81 chars (80 + ellipsis).
    expect(titlePart.length).toBeLessThanOrEqual(81);
    // Must not contain the full 90-char title.
    expect(titlePart).not.toBe(longTitle);
  });

  it('returns just "Blocked by:" header for empty input', () => {
    expect(formatBlockerTooltip([])).toBe('Blocked by:');
  });
});
