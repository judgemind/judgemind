import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  INFRA_PREEMPTED_CATEGORIES,
  INFRA_PREEMPTED_GLYPH,
  INFRA_PREEMPTED_SUMMARIES,
  IssueLink,
  OutcomePill,
  PriorityBadge,
  PRLink,
} from '../ui-primitives';

describe('IssueLink', () => {
  it('renders an anchor to the github issue that opens in a new tab', () => {
    render(<IssueLink number={2805} />);
    const link = screen.getByTestId('issue-link-2805');
    expect(link).toHaveAttribute('href', 'https://github.com/judgemind/judgemind/issues/2805');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(link.textContent).toBe('#2805');
  });

  // Regression guard for #2816: `text-accent` in tailwind.config.ts maps
  // to shadcn's hover-surface gray, not brand amber.  The correct tokens
  // are `text-brand-accent` (light mode, amber-700) paired with
  // `dark:text-brand-accent-light` (dark mode, amber-600).  Both must be
  // present so link chrome is visible on both themes.
  it('renders with the brand-accent token pair (light + dark)', () => {
    render(<IssueLink number={2816} />);
    const link = screen.getByTestId('issue-link-2816');
    expect(link.className).toContain('text-brand-accent');
    expect(link.className).toContain('dark:text-brand-accent-light');
    // Bare `text-accent` must not appear — it would trigger the #2816
    // readability regression where links render as low-contrast gray.
    expect(link.className).not.toMatch(/(^|\s)text-accent(\s|$)/);
  });
});

describe('PRLink', () => {
  it('renders an anchor to the github PR', () => {
    render(<PRLink number={2740} />);
    const link = screen.getByTestId('pr-link-2740');
    expect(link).toHaveAttribute('href', 'https://github.com/judgemind/judgemind/pull/2740');
    expect(link.textContent).toBe('PR #2740');
  });

  // Regression guard for #2816 — see note on IssueLink above.
  it('renders with the brand-accent token pair (light + dark)', () => {
    render(<PRLink number={2811} />);
    const link = screen.getByTestId('pr-link-2811');
    expect(link.className).toContain('text-brand-accent');
    expect(link.className).toContain('dark:text-brand-accent-light');
    expect(link.className).not.toMatch(/(^|\s)text-accent(\s|$)/);
  });
});

describe('PriorityBadge', () => {
  it('renders p1 with amber tint', () => {
    render(<PriorityBadge priority="p1" />);
    const badge = screen.getByTestId('priority-badge-p1');
    expect(badge.textContent).toBe('p1');
    expect(badge.className).toContain('amber');
  });

  it('renders p0 with red tint', () => {
    render(<PriorityBadge priority="p0" />);
    const badge = screen.getByTestId('priority-badge-p0');
    expect(badge.textContent).toBe('p0');
    expect(badge.className).toContain('red');
  });

  it('renders em-dash placeholder for null', () => {
    render(<PriorityBadge priority={null} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});

describe('OutcomePill', () => {
  it('renders succeeded with a checkmark glyph', () => {
    render(<OutcomePill status="succeeded" />);
    const pill = screen.getByTestId('outcome-pill-succeeded');
    expect(pill.getAttribute('aria-label')).toBe('succeeded');
    expect(pill.textContent).toBe('\u2713');
  });

  it('renders failed with an X glyph', () => {
    render(<OutcomePill status="failed" />);
    const pill = screen.getByTestId('outcome-pill-failed');
    expect(pill.textContent).toBe('\u2717');
  });

  it('renders crashed with a warning glyph', () => {
    render(<OutcomePill status="crashed" />);
    const pill = screen.getByTestId('outcome-pill-crashed');
    expect(pill.textContent).toBe('\u26A0');
  });

  // #2857: `plan_blocked` is the "plan correctly declined" terminal —
  // distinct from `failed` (real infrastructure break). Uses a muted
  // neutral chip (not red/amber) because it is operator-informational,
  // not alarming.
  it('renders plan_blocked with a circled-division-slash glyph', () => {
    render(<OutcomePill status="plan_blocked" />);
    const pill = screen.getByTestId('outcome-pill-plan_blocked');
    expect(pill.getAttribute('aria-label')).toBe('plan blocked');
    expect(pill.textContent).toBe('\u2298');
  });

  it('renders plan_blocked with the neutral muted chip (not red/amber)', () => {
    render(<OutcomePill status="plan_blocked" />);
    const pill = screen.getByTestId('outcome-pill-plan_blocked');
    // Neutral stone treatment — signals "informational, not alarming".
    expect(pill.className).toContain('bg-muted');
    // Must NOT use the failure/warning colour tokens.
    expect(pill.className).not.toMatch(/bg-red/);
    expect(pill.className).not.toMatch(/bg-amber/);
  });

  // #2856: `needs_review` is the "ralph did real work but summary
  // flagged unmet AC" terminal — the daemon opened a DRAFT PR for
  // operator review. Uses a yellow chip (not neutral like
  // plan_blocked) because it DOES need operator action, and not
  // amber (reserved for crashed) so the operator can scan the panel
  // and separate "review my draft PR" from "something crashed".
  it('renders needs_review with a half-circle glyph', () => {
    render(<OutcomePill status="needs_review" />);
    const pill = screen.getByTestId('outcome-pill-needs_review');
    expect(pill.getAttribute('aria-label')).toBe('needs review');
    expect(pill.textContent).toBe('\u25D0');
  });

  it('renders needs_review with a yellow chip (actionable, not alarming)', () => {
    render(<OutcomePill status="needs_review" />);
    const pill = screen.getByTestId('outcome-pill-needs_review');
    // Yellow — distinct from amber (`crashed`) and from the neutral
    // muted chip (`plan_blocked`). Operator sees it and knows "there
    // is a draft PR to review" without mistaking it for a crash.
    expect(pill.className).toContain('bg-yellow');
    // Must NOT collapse onto the red (failure) palette.
    expect(pill.className).not.toMatch(/bg-red/);
    // Must NOT reuse the amber crashed-chip palette — the whole point
    // of the yellow/amber separation is to keep these two actionable
    // states visually distinct.
    expect(pill.className).not.toMatch(/bg-amber/);
    // Must NOT reuse plan_blocked's neutral muted surface — that chip
    // is for informational-only outcomes.
    expect(pill.className).not.toMatch(/bg-muted/);
  });

  it('renders a fallback for unknown status', () => {
    render(<OutcomePill status="bogus" />);
    expect(screen.getByText('?')).toBeInTheDocument();
  });

  // #2900: failureSummary prop drives the hover tooltip for failure
  // terminals so operators can triage without opening the agent detail
  // page. Null / undefined / whitespace falls back to the built-in
  // status-label tooltip to preserve the existing UX for succeeded /
  // needs_review / pre-migration-33 rows.
  describe('failureSummary prop (#2900)', () => {
    it('uses failureSummary as the title tooltip when present', () => {
      const summary =
        'ralph crashed at ralph-reviewer iteration 3 (subprocess_turn_limit): reviewer exceeded max turns';
      render(<OutcomePill status="crashed" failureSummary={summary} />);
      const pill = screen.getByTestId('outcome-pill-crashed');
      expect(pill.getAttribute('title')).toBe(summary);
      // aria-label stays on the short status string so screen readers
      // announce "crashed" without reading the full summary mid-row.
      expect(pill.getAttribute('aria-label')).toBe('crashed');
    });

    it('falls back to the status label when failureSummary is null', () => {
      render(<OutcomePill status="failed" failureSummary={null} />);
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.getAttribute('title')).toBe('failed');
    });

    it('falls back to the status label when failureSummary is undefined', () => {
      render(<OutcomePill status="failed" />);
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.getAttribute('title')).toBe('failed');
    });

    it('falls back to the status label when failureSummary is an empty string', () => {
      render(<OutcomePill status="failed" failureSummary="" />);
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.getAttribute('title')).toBe('failed');
    });

    it('falls back to the status label when failureSummary is whitespace-only', () => {
      render(<OutcomePill status="failed" failureSummary="   " />);
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.getAttribute('title')).toBe('failed');
    });

    it('trims surrounding whitespace in the rendered tooltip', () => {
      render(
        <OutcomePill
          status="failed"
          failureSummary="  plan phase returned go=false: scope is ambiguous  "
        />,
      );
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.getAttribute('title')).toBe(
        'plan phase returned go=false: scope is ambiguous',
      );
    });

    it('respects the plan_blocked failure-summary path', () => {
      // plan_blocked is a correct-outcome terminal but still gets a
      // failure_summary (the block_reason). Verify the pill shows the
      // summary on hover without losing its neutral chip styling.
      const summary = 'plan phase returned go=false: acceptance criteria missing';
      render(
        <OutcomePill status="plan_blocked" failureSummary={summary} />,
      );
      const pill = screen.getByTestId('outcome-pill-plan_blocked');
      expect(pill.getAttribute('title')).toBe(summary);
      expect(pill.className).toContain('bg-muted');
    });

    it('applies the tooltip to the unknown-status fallback chip', () => {
      render(
        <OutcomePill
          status="something-new"
          failureSummary="future terminal we haven't seen yet"
        />,
      );
      const pill = screen.getByText('?');
      expect(pill.getAttribute('title')).toBe(
        "future terminal we haven't seen yet",
      );
    });
  });

  // #2947: two `dispatcher.failures.category` values represent
  // infra preemption rather than real failure — `daemon_restart_abandoned`
  // (dispatcher cycled mid-agent; retry on next tick) and
  // `paused_by_killswitch` (operator hit the global kill switch). The
  // daemon writes exactly two canonical `failure_summary` strings for
  // these — "dispatcher restarted" and "manually stopped" — and the pill
  // re-skins to amber ↺ to signal "will auto-resume, not an action item".
  describe('infra-preempted override (#2947)', () => {
    it('renders ↺ in amber for "dispatcher restarted" (daemon_restart_abandoned)', () => {
      render(
        <OutcomePill status="failed" failureSummary="dispatcher restarted" />,
      );
      const pill = screen.getByTestId('outcome-pill-infra_preempted');
      expect(pill.textContent).toBe(INFRA_PREEMPTED_GLYPH);
      expect(pill.textContent).toBe('\u21BA');
      expect(pill.className).toContain('bg-amber');
      expect(pill.className).not.toMatch(/bg-red/);
      // aria-label is a neutral descriptor; operator-friendly.
      expect(pill.getAttribute('aria-label')).toBe('infra preempted');
      expect(pill.getAttribute('title')).toBe('dispatcher restarted');
    });

    it('renders ↺ in amber for "manually stopped" (paused_by_killswitch)', () => {
      render(
        <OutcomePill status="failed" failureSummary="manually stopped" />,
      );
      const pill = screen.getByTestId('outcome-pill-infra_preempted');
      expect(pill.textContent).toBe('\u21BA');
      expect(pill.className).toContain('bg-amber');
      expect(pill.className).not.toMatch(/bg-red/);
      expect(pill.getAttribute('title')).toBe('manually stopped');
    });

    it('trims whitespace before matching (defensive against upstream drift)', () => {
      render(
        <OutcomePill
          status="failed"
          failureSummary="  manually stopped  "
        />,
      );
      const pill = screen.getByTestId('outcome-pill-infra_preempted');
      expect(pill.textContent).toBe('\u21BA');
      expect(pill.getAttribute('title')).toBe('manually stopped');
    });

    it('falls back to red ✗ for a non-infra failureSummary', () => {
      render(
        <OutcomePill
          status="failed"
          failureSummary="subprocess crashed (turn limit)"
        />,
      );
      // Regular failed path — red ✗, not amber ↺.
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.textContent).toBe('\u2717');
      expect(pill.className).toContain('bg-red');
    });

    it('does not apply the override when failureSummary is null', () => {
      render(<OutcomePill status="failed" failureSummary={null} />);
      const pill = screen.getByTestId('outcome-pill-failed');
      expect(pill.textContent).toBe('\u2717');
      expect(pill.className).toContain('bg-red');
    });

    it('overrides crashed → ↺ too, because the category can land on either status', () => {
      // If a future code path writes `status='crashed'` for an
      // infra-preempted row (today the daemon uses `failed`, but we
      // intentionally don't couple to that), the pill should still
      // re-skin to ↺ amber rather than keep the `⚠ crashed` look.
      render(
        <OutcomePill status="crashed" failureSummary="dispatcher restarted" />,
      );
      const pill = screen.getByTestId('outcome-pill-infra_preempted');
      expect(pill.textContent).toBe('\u21BA');
      expect(pill.className).toContain('bg-amber');
    });

    it('exports exactly the two canonical summary strings', () => {
      // Guard rail: the set must stay in lockstep with the daemon's
      // `_NO_TAIL_CATEGORY_SUMMARIES` (see
      // `scripts/dispatcher/daemon.py`). Extending this set requires
      // coordinating with the daemon; shrinking it causes silent
      // regressions on admin-page rows.
      expect(INFRA_PREEMPTED_SUMMARIES.size).toBe(2);
      expect(INFRA_PREEMPTED_SUMMARIES.has('dispatcher restarted')).toBe(true);
      expect(INFRA_PREEMPTED_SUMMARIES.has('manually stopped')).toBe(true);
    });

    it('exports exactly the two canonical category strings', () => {
      // Same guard rail for `dispatcher.failures.category` values,
      // used by `RecentFailuresPanel` which keys off category
      // directly.
      expect(INFRA_PREEMPTED_CATEGORIES.size).toBe(2);
      expect(INFRA_PREEMPTED_CATEGORIES.has('daemon_restart_abandoned')).toBe(
        true,
      );
      expect(INFRA_PREEMPTED_CATEGORIES.has('paused_by_killswitch')).toBe(true);
    });
  });
});
