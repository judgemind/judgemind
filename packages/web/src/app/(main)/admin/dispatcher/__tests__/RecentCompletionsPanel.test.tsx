import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RecentCompletionsPanel } from '../RecentCompletionsPanel';
import type { RecentCompletion } from '@/lib/dispatcher-queries';

const now = Date.parse('2026-04-18T12:00:00Z');

function completion(overrides: Partial<RecentCompletion>): RecentCompletion {
  return {
    agentId: 'agent-1',
    issueNumber: 2805,
    issueTitle: 'wire dispatcher admin dashboard',
    status: 'succeeded',
    endedAt: '2026-04-18T11:55:00Z',
    prNumber: 2811,
    ...overrides,
  };
}

describe('RecentCompletionsPanel', () => {
  it('renders the empty state when completions is empty', () => {
    render(<RecentCompletionsPanel completions={[]} nowMs={now} />);
    expect(screen.getByText(/no completed agents yet/i)).toBeInTheDocument();
  });

  it('renders outcome glyph + issue link + PR link + title', () => {
    const items = [completion({})];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.getByTestId('outcome-pill-succeeded')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-2805')).toBeInTheDocument();
    expect(screen.getByTestId('pr-link-2811')).toBeInTheDocument();
    expect(screen.getByText('wire dispatcher admin dashboard')).toBeInTheDocument();
  });

  // --- #2818 — density pass: "(no PR)" filler and "N min ago" column removed.
  it('#2818: renders nothing in place of a missing PR (no "(no PR)" text)', () => {
    const items = [completion({ prNumber: null, agentId: 'agent-2' })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.queryByText(/\(no PR\)/i)).not.toBeInTheDocument();
    // No PR link rendered for this row.
    expect(screen.queryByTestId('pr-link-null')).not.toBeInTheDocument();
  });

  it('#2818: does not render a "N min ago" completion-time column', () => {
    const items = [
      completion({
        endedAt: '2026-04-18T11:55:00Z', // 5 min ago from the fixed `now`
        agentId: 'agent-3',
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.queryByText(/\bago\b/i)).not.toBeInTheDocument();
  });

  it('#2818: still shows the "(title unavailable)" placeholder when issueTitle is null', () => {
    const items = [completion({ issueTitle: null, agentId: 'agent-4' })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.getByText(/\(title unavailable\)/i)).toBeInTheDocument();
  });

  it('#2818: renders long titles in full without ellipsis truncation', () => {
    const longTitle =
      'fix(admin-dispatcher): (title unavailable) everywhere + 20562-day-ago timestamps + strip wasteful columns';
    const items = [completion({ issueTitle: longTitle, agentId: 'agent-5' })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const titleEl = screen.getByText(longTitle);
    expect(titleEl.className).toMatch(/break-words/);
    expect(titleEl.className).not.toMatch(/\btruncate\b/);
  });
});
