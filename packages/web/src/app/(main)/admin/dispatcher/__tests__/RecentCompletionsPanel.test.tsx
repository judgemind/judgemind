import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  RecentCompletionsPanel,
  formatCostFootnote,
  formatTokenCount,
  formatRelativeTime,
  formatPacificDatetime,
} from '../RecentCompletionsPanel';
import type { RecentCompletion } from '@/lib/dispatcher-queries';

const now = Date.parse('2026-04-18T12:00:00Z');

function completion(overrides: Partial<RecentCompletion>): RecentCompletion {
  return {
    agentId: 'agent-1',
    issueNumber: 2805,
    issueTitle: 'wire dispatcher admin dashboard',
    priority: 'p2',
    status: 'succeeded',
    endedAt: '2026-04-18T11:55:00Z',
    prNumber: 2811,
    totalTokens: null,
    totalCostUsd: null,
    failureSummary: null,
    ...overrides,
  };
}

describe('RecentCompletionsPanel', () => {
  it('renders the empty state when completions is empty', () => {
    render(<RecentCompletionsPanel completions={[]} nowMs={now} />);
    expect(screen.getByText(/no completed agents yet/i)).toBeInTheDocument();
  });

  it('renders outcome glyph + issue link + priority badge + PR link + title', () => {
    const items = [completion({})];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.getByTestId('outcome-pill-succeeded')).toBeInTheDocument();
    expect(screen.getByTestId('issue-link-2805')).toBeInTheDocument();
    // #2899: unified `(issue, priority, [pr,] title)` prefix — priority
    // badge sits between the issue link and the PR link.
    expect(screen.getByTestId('priority-badge-p2')).toBeInTheDocument();
    expect(screen.getByTestId('pr-link-2811')).toBeInTheDocument();
    expect(screen.getByText('wire dispatcher admin dashboard')).toBeInTheDocument();
  });

  it('#2899: renders the em-dash priority placeholder when priority is null', () => {
    const items = [completion({ priority: null, agentId: 'agent-pnull' })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    // PriorityBadge's em-dash placeholder.
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  // --- #2818 — density pass: "(no PR)" filler and "N min ago" column removed.
  it('#2818: renders nothing in place of a missing PR (no "(no PR)" text)', () => {
    const items = [completion({ prNumber: null, agentId: 'agent-2' })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    expect(screen.queryByText(/\(no PR\)/i)).not.toBeInTheDocument();
    // No PR link rendered for this row.
    expect(screen.queryByTestId('pr-link-null')).not.toBeInTheDocument();
  });

  // #2932 re-introduces the relative-time cell that #2818 stripped,
  // with a better abbreviated format and a Pacific-time hover tooltip.
  it('#2932: renders the abbreviated relative-time cell (5m ago) after the cost footnote', () => {
    const items = [
      completion({
        endedAt: '2026-04-18T11:55:00Z', // 5 min ago from the fixed `now`
        agentId: 'agent-3',
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const cell = screen.getByTestId('completion-row-relative-time');
    expect(cell.textContent).toBe('5m ago');
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

  // --- #2900 — failure_summary tooltip on the outcome glyph.
  //
  // The panel keeps its density-first layout (#2818) — no always-
  // visible second line. The summary surfaces on hover via the
  // OutcomePill's `title` attribute, so operators can triage failures
  // without opening the agent detail page.
  it('#2900: wires failure_summary into the OutcomePill tooltip for failure rows', () => {
    const summary =
      'ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns';
    const items = [
      completion({
        agentId: 'agent-fail',
        issueNumber: 2900,
        status: 'crashed',
        prNumber: null,
        failureSummary: summary,
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const pill = screen.getByTestId('outcome-pill-crashed');
    expect(pill.getAttribute('title')).toBe(summary);
  });

  it('#2900: leaves the default status-label tooltip for succeeded rows (no summary)', () => {
    const items = [completion({ agentId: 'agent-ok', failureSummary: null })];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const pill = screen.getByTestId('outcome-pill-succeeded');
    // Default tooltip is the human status label ("succeeded").
    expect(pill.getAttribute('title')).toBe('succeeded');
  });

  it('#2900: does NOT render the summary as a second visible line on the row', () => {
    const summary =
      'ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns';
    const items = [
      completion({
        agentId: 'agent-hidden',
        issueNumber: 2900,
        status: 'crashed',
        prNumber: null,
        failureSummary: summary,
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    // The summary must not appear as visible text on the row — the
    // density-first constraint (#2818) is that the panel keeps its
    // one-line-per-row layout. The tooltip is the *only* surface.
    expect(screen.queryByText(summary)).not.toBeInTheDocument();
  });

  // --- #2932 — relative-time cell (re-introduced post-#2818 density pass).
  it('#2932: places the relative-time cell AFTER the cost footnote in DOM order', () => {
    const items = [
      completion({
        agentId: 'agent-order',
        endedAt: '2026-04-18T11:55:00Z', // 5m ago
        totalCostUsd: 0.42,
        totalTokens: 12000,
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const cost = screen.getByTestId('completion-row-cost');
    const timestamp = screen.getByTestId('completion-row-relative-time');
    // `compareDocumentPosition`: 0x04 = DOCUMENT_POSITION_FOLLOWING.
    expect(
      cost.compareDocumentPosition(timestamp) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('#2932: sets the native `title` attribute on the cell to a Pacific-time datetime', () => {
    const items = [
      completion({
        agentId: 'agent-pacific',
        endedAt: '2026-04-18T11:55:00Z',
      }),
    ];
    render(<RecentCompletionsPanel completions={items} nowMs={now} />);
    const cell = screen.getByTestId('completion-row-relative-time');
    const title = cell.getAttribute('title');
    expect(title).not.toBeNull();
    // Format: "YYYY-MM-DD HH:MM:SS PDT" or "PST" — Pacific time always
    // abbreviates to one of these two. April is PDT.
    expect(title).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} P[DS]T$/,
    );
    expect(title).toContain('PDT');
  });

  it('#2932: falls back to Date.now() when nowMs prop is omitted', () => {
    // Render without `nowMs`. The cell still renders (with some age
    // computed against real wall-clock time). We only assert the cell
    // is present and has the expected testid — the exact text depends
    // on wall-clock time at test runtime so we don't pin it.
    const items = [completion({ agentId: 'agent-realtime' })];
    render(<RecentCompletionsPanel completions={items} />);
    const cell = screen.getByTestId('completion-row-relative-time');
    expect(cell).toBeInTheDocument();
    // Must have a non-empty title attribute (Pacific datetime).
    expect(cell.getAttribute('title')).toMatch(/P[DS]T$/);
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

describe('formatRelativeTime (#2932)', () => {
  // Anchor "now" at a fixed instant so the age math is deterministic.
  const nowMs = Date.parse('2026-04-20T12:00:00Z');

  function at(isoOffset: string): number {
    return Date.parse(isoOffset);
  }

  it('renders "Just now" for < 60 seconds (30s case)', () => {
    const thirtySecondsAgo = nowMs - 30 * 1000;
    expect(formatRelativeTime(thirtySecondsAgo, nowMs)).toBe('Just now');
  });

  it('renders "5m ago" for 5 minutes', () => {
    const fiveMinutesAgo = nowMs - 5 * 60 * 1000;
    expect(formatRelativeTime(fiveMinutesAgo, nowMs)).toBe('5m ago');
  });

  it('renders "47m ago" for 47 minutes (edge of the hour bucket)', () => {
    const fortySevenMinutesAgo = nowMs - 47 * 60 * 1000;
    expect(formatRelativeTime(fortySevenMinutesAgo, nowMs)).toBe('47m ago');
  });

  it('renders "1h ago" for exactly 1 hour', () => {
    const oneHourAgo = nowMs - 60 * 60 * 1000;
    expect(formatRelativeTime(oneHourAgo, nowMs)).toBe('1h ago');
  });

  it('renders "23h ago" for 23 hours (edge of the day bucket)', () => {
    const twentyThreeHoursAgo = nowMs - 23 * 60 * 60 * 1000;
    expect(formatRelativeTime(twentyThreeHoursAgo, nowMs)).toBe('23h ago');
  });

  it('renders "1 day ago" (SINGULAR) for exactly 1 day', () => {
    const oneDayAgo = nowMs - 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(oneDayAgo, nowMs)).toBe('1 day ago');
  });

  it('renders "2 days ago" for 2 days', () => {
    const twoDaysAgo = nowMs - 2 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(twoDaysAgo, nowMs)).toBe('2 days ago');
  });

  it('renders "11 days ago" for 11 days', () => {
    const elevenDaysAgo = nowMs - 11 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(elevenDaysAgo, nowMs)).toBe('11 days ago');
  });

  it('renders "1 month ago" for 40 days (≥ 30-day bucket, singular)', () => {
    const fortyDaysAgo = nowMs - 40 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(fortyDaysAgo, nowMs)).toBe('1 month ago');
  });

  it('renders "3 months ago" for 100 days', () => {
    const hundredDaysAgo = nowMs - 100 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(hundredDaysAgo, nowMs)).toBe('3 months ago');
  });

  it('renders "1 year ago" for 400 days (singular)', () => {
    const fourHundredDaysAgo = nowMs - 400 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(fourHundredDaysAgo, nowMs)).toBe('1 year ago');
  });

  it('renders "N years ago" for multi-year ages', () => {
    const threeYearsAgo = nowMs - 3 * 365 * 24 * 60 * 60 * 1000;
    expect(formatRelativeTime(threeYearsAgo, nowMs)).toBe('3 years ago');
  });

  it('renders "Just now" for future timestamps (clock skew)', () => {
    const fiveSecondsInTheFuture = nowMs + 5 * 1000;
    expect(formatRelativeTime(fiveSecondsInTheFuture, nowMs)).toBe('Just now');
  });

  it('boundary: 59 seconds → "Just now", 60 seconds → "1m ago"', () => {
    expect(formatRelativeTime(nowMs - 59 * 1000, nowMs)).toBe('Just now');
    expect(formatRelativeTime(nowMs - 60 * 1000, nowMs)).toBe('1m ago');
  });

  it('boundary: 59 minutes → "59m ago", 60 minutes → "1h ago"', () => {
    expect(formatRelativeTime(nowMs - 59 * 60 * 1000, nowMs)).toBe('59m ago');
    expect(formatRelativeTime(nowMs - 60 * 60 * 1000, nowMs)).toBe('1h ago');
  });

  it('boundary: 29 days → "29 days ago", 30 days → "1 month ago"', () => {
    expect(formatRelativeTime(nowMs - 29 * 24 * 60 * 60 * 1000, nowMs)).toBe(
      '29 days ago',
    );
    expect(formatRelativeTime(nowMs - 30 * 24 * 60 * 60 * 1000, nowMs)).toBe(
      '1 month ago',
    );
  });

  // Keep this cheap — `at` is only used when the test needs an
  // absolute ISO-8601 date rather than an offset.
  it('works with ISO-parsed endedAt values (smoke test)', () => {
    expect(formatRelativeTime(at('2026-04-20T11:55:00Z'), nowMs)).toBe('5m ago');
  });
});

describe('formatPacificDatetime (#2932)', () => {
  it('formats an April UTC timestamp as PDT (daylight time)', () => {
    // 2026-04-20T22:36:39Z is 15:36:39 in Pacific Daylight Time (UTC-7).
    const ms = Date.parse('2026-04-20T22:36:39Z');
    expect(formatPacificDatetime(ms)).toBe('2026-04-20 15:36:39 PDT');
  });

  it('formats a January UTC timestamp as PST (standard time)', () => {
    // 2026-01-15T18:00:00Z is 10:00:00 in Pacific Standard Time (UTC-8).
    const ms = Date.parse('2026-01-15T18:00:00Z');
    expect(formatPacificDatetime(ms)).toBe('2026-01-15 10:00:00 PST');
  });

  it('emits a consistent YYYY-MM-DD HH:MM:SS ZZZ shape', () => {
    const ms = Date.parse('2026-04-20T22:36:39Z');
    expect(formatPacificDatetime(ms)).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} P[DS]T$/,
    );
  });
});
