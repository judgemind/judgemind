import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueuePanel } from '../QueuePanel';
import type { QueueItem } from '@/lib/dispatcher-queries';

const now = Date.parse('2026-04-18T12:00:00Z');

function item(overrides: Partial<QueueItem>): QueueItem {
  return {
    issueNumber: 1,
    title: 'Example issue',
    priority: 'p1',
    labels: ['priority/p1', 'agent/ready'],
    createdAt: '2026-04-18T11:00:00Z',
    blockedBy: [],
    ...overrides,
  };
}

describe('QueuePanel', () => {
  it('renders empty states for both panels when no items', () => {
    render(<QueuePanel queueReady={[]} queueBlocked={[]} nowMs={now} />);
    expect(screen.getByText(/queue empty/i)).toBeInTheDocument();
    expect(screen.getByText(/no blocked issues/i)).toBeInTheDocument();
  });

  it('renders agent-ready rows with hot-linked issue numbers, priority badges, and full titles', () => {
    const items = [
      item({ issueNumber: 2801, title: 'wire dispatcherControl through to daemon' }),
      item({ issueNumber: 2802, title: 'recently completed panel', priority: 'p2' }),
    ];
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
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
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [2500, 2600] }),
    ];
    render(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    expect(screen.getByTestId('issue-link-2500')).toBeInTheDocument();
    expect(screen.getByText('blocked issue')).toBeInTheDocument();
  });

  // --- #2818 — density pass: rank / blocked-count / filed-time columns removed.
  it('#2818: strips the #<rank> column from the ready side', () => {
    const items = [
      item({ issueNumber: 2801, title: 'first' }),
      item({ issueNumber: 2802, title: 'second' }),
    ];
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByText('#1')).not.toBeInTheDocument();
    expect(screen.queryByText('#2')).not.toBeInTheDocument();
    expect(screen.queryAllByTestId('queue-row-slot')).toHaveLength(0);
  });

  it('#2818: strips the [N] blocked-by count column from the blocked side', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [2500, 2600] }),
    ];
    render(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
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
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    expect(screen.queryByText(/\bago\b/i)).not.toBeInTheDocument();
  });

  it('#2818: renders long titles in full without ellipsis truncation', () => {
    const longTitle =
      'fix(admin-dispatcher): (title unavailable) everywhere + 20562-day-ago timestamps + strip wasteful columns';
    const items = [item({ issueNumber: 2818, title: longTitle })];
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
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
    render(
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
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [2500, 2600] }),
    ];
    render(
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
    render(
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
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
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
    render(<QueuePanel queueReady={items} queueBlocked={[]} nowMs={now} />);
    const row1 = screen.getByTestId('queue-row-2801');
    const row2 = screen.getByTestId('queue-row-2802');
    expect((row1 as HTMLElement).style.viewTransitionName).toBe('issue-2801');
    expect((row2 as HTMLElement).style.viewTransitionName).toBe('issue-2802');
  });

  it('#2967: blocked panel rows do NOT carry a view-transition-name', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue' }),
    ];
    render(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    // Blocked rows are intentionally not animated — they don't show up
    // in the Queue → Active → Completed flow, and giving them the same
    // `issue-<N>` name as a ready row of the same issue number would
    // cause a DOM collision the next time an issue flips between the
    // two label states. We assert via the absence of any
    // `queue-row-<N>` test id — the animated-row testid is only set on
    // ready rows.
    expect(screen.queryByTestId('queue-row-2500')).toBeNull();
  });
});
