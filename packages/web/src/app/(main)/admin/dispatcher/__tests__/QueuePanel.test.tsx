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

  it('renders agent-ready rows with hot-linked issue numbers', () => {
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
    // Slot numbers #1, #2.
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText('#2')).toBeInTheDocument();
    // Priority badges present.
    expect(screen.getByTestId('priority-badge-p1')).toBeInTheDocument();
    expect(screen.getByTestId('priority-badge-p2')).toBeInTheDocument();
  });

  it('renders blocked rows with [N] blocker count and tooltip', () => {
    const blocked = [
      item({ issueNumber: 2500, title: 'blocked issue', blockedBy: [2500, 2600] }),
    ];
    render(<QueuePanel queueReady={[]} queueBlocked={blocked} nowMs={now} />);
    expect(screen.getByText('[2]')).toBeInTheDocument();
    // Tooltip exposes the raw blocker list.
    const slot = screen.getByText('[2]');
    expect(slot.getAttribute('title')).toBe('#2500, #2600');
  });
});
