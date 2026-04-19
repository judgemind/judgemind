import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  RecentCompletionsPanel,
  formatCostFootnote,
  formatTokenCount,
} from '../RecentCompletionsPanel';
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
    totalTokens: null,
    totalCostUsd: null,
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

  // --- #2869 — per-PR cost footnote on each row.
  it('#2869: renders "~$X.XX, Nk tok" footnote when both metering fields are populated', () => {
    const items = [
      completion({
        agentId: 'agent-cost-1',
        totalCostUsd: 0.42,
        totalTokens: 123456,
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const footnote = screen.getByTestId('completion-row-cost');
    expect(footnote.textContent).toContain('~$0.42');
    // 123456 tokens → 123k (Math.round, no decimal for >= 10k).
    expect(footnote.textContent).toContain('123k tok');
  });

  it('#2869: omits the footnote entirely when both metering fields are null (pre-migration-31)', () => {
    const items = [
      completion({
        agentId: 'agent-no-cost',
        totalCostUsd: null,
        totalTokens: null,
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    // No footnote rendered — distinguishes "no data" from "$0.00".
    expect(screen.queryByTestId('completion-row-cost')).not.toBeInTheDocument();
  });

  it('#2869: renders only the available half when the other metering field is null', () => {
    const costOnly = [
      completion({
        agentId: 'agent-cost-only',
        totalCostUsd: 0.01,
        totalTokens: null,
      }),
    ];
    const { unmount } = render(
      <RecentCompletionsPanel completions={costOnly} nowMs={now} />,
    );
    expect(screen.getByTestId('completion-row-cost').textContent).toContain(
      '~$0.0100',
    );
    expect(screen.getByTestId('completion-row-cost').textContent).not.toContain(
      'tok',
    );
    unmount();

    const tokensOnly = [
      completion({
        agentId: 'agent-tokens-only',
        totalCostUsd: null,
        totalTokens: 5000,
      }),
    ];
    render(<RecentCompletionsPanel completions={tokensOnly} nowMs={now} />);
    const footnote = screen.getByTestId('completion-row-cost');
    expect(footnote.textContent).toContain('tok');
    expect(footnote.textContent).not.toContain('$');
  });
});

describe('formatCostFootnote (#2869)', () => {
  it('returns null when both inputs are null', () => {
    expect(formatCostFootnote(null, null)).toBeNull();
  });

  it('formats cost with 2 decimals when >= $1', () => {
    expect(formatCostFootnote(1.234, null)).toBe('(~$1.23)');
    expect(formatCostFootnote(42.5, null)).toBe('(~$42.50)');
  });

  it('formats cost with 4 decimals when < $1 (so haiku cheap phases are visible)', () => {
    expect(formatCostFootnote(0.0008, null)).toBe('(~$0.0008)');
    expect(formatCostFootnote(0.1234, null)).toBe('(~$0.1234)');
  });

  it('combines cost and token count with a comma separator', () => {
    expect(formatCostFootnote(0.42, 12345)).toBe('(~$0.4200, 12k tok)');
  });
});

describe('formatTokenCount (#2869)', () => {
  it('renders a single-digit "k" with a decimal for < 10k', () => {
    expect(formatTokenCount(1500)).toBe('1.5k tok');
    expect(formatTokenCount(9876)).toBe('9.9k tok');
  });

  it('drops the decimal for >= 10k (noise for at-a-glance)', () => {
    expect(formatTokenCount(12345)).toBe('12k tok');
    expect(formatTokenCount(234567)).toBe('235k tok');
  });

  it('renders raw count for < 1k so "0k" does not imply zero cost', () => {
    expect(formatTokenCount(500)).toBe('500 tok');
    expect(formatTokenCount(0)).toBe('0 tok');
  });
});
