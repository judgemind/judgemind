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
});
