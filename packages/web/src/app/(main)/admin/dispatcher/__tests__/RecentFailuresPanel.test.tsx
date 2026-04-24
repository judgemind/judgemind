import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { RecentFailuresPanel } from '../RecentFailuresPanel';
import type { DispatcherFailure } from '@/lib/dispatcher-queries';

// Fixed `nowMs` for deterministic relative-time rendering.
const NOW_MS = Date.parse('2026-04-20T12:00:00Z');

/**
 * Build a fixture DispatcherFailure. When `displayCategory` is not
 * supplied we default it to the raw `category` — matching the backend
 * contract (#2948): unknown categories fall through to the raw token.
 * Tests that exercise the #2948 rephrase explicitly set
 * `displayCategory` to the operator-friendly string.
 */
function makeFailure(overrides: Partial<DispatcherFailure> = {}): DispatcherFailure {
  const category = overrides.category ?? 'subprocess_crash';
  return {
    failureId: '1',
    agentId: 'agent-1',
    category,
    displayCategory: overrides.displayCategory ?? category,
    detectedBy: 'scheduler',
    details: {},
    ts: new Date(NOW_MS - 5 * 60 * 1000).toISOString(),
    issueNumber: 1234,
    ...overrides,
  };
}

describe('RecentFailuresPanel', () => {
  it('renders the empty state when there are no failures', () => {
    render(<RecentFailuresPanel failures={[]} nowMs={NOW_MS} />);
    expect(
      screen.getByText(/no failures in the last 24 hours/i),
    ).toBeInTheDocument();
  });

  // #2947: every failure category gets a leading glyph so operators can
  // distinguish real failures from infra preemption at a glance.
  describe('glyph column (#2947)', () => {
    it('renders red ✗ for ordinary failure categories', () => {
      render(
        <RecentFailuresPanel
          failures={[makeFailure({ category: 'subprocess_crash' })]}
          nowMs={NOW_MS}
        />,
      );
      const glyph = screen.getByTestId('failure-glyph-failed');
      expect(glyph.textContent).toBe('\u2717');
      expect(glyph.className).toContain('bg-red');
      expect(glyph.className).not.toMatch(/bg-amber/);
    });

    it('renders gray ↺ for daemon_restart_abandoned', () => {
      // Issue #2953 recoloured the infra-preempt chip from amber to
      // gray (bg-muted) — infra churn is neutral, not a warning.
      render(
        <RecentFailuresPanel
          failures={[makeFailure({ category: 'daemon_restart_abandoned' })]}
          nowMs={NOW_MS}
        />,
      );
      const glyph = screen.getByTestId('failure-glyph-infra_preempted');
      expect(glyph.textContent).toBe('\u21BA');
      expect(glyph.className).toContain('bg-muted');
      expect(glyph.className).not.toMatch(/bg-amber/);
      expect(glyph.className).not.toMatch(/bg-red/);
      // aria-label is a neutral descriptor, not "failed".
      expect(glyph.getAttribute('aria-label')).toBe('infra preempted');
    });

    it('renders gray ↺ for paused_by_killswitch', () => {
      render(
        <RecentFailuresPanel
          failures={[makeFailure({ category: 'paused_by_killswitch' })]}
          nowMs={NOW_MS}
        />,
      );
      const glyph = screen.getByTestId('failure-glyph-infra_preempted');
      expect(glyph.textContent).toBe('\u21BA');
      expect(glyph.className).toContain('bg-muted');
      expect(glyph.className).not.toMatch(/bg-amber/);
    });

    it('keeps infra-preempted rows IN the table (operator wants the signal)', () => {
      // Per the #2947 issue body: "Per operator direction, infra-preempted
      // outcomes should still appear in the Recent Failures (last 24h)
      // roll-up — they're useful signal about dispatcher churn even if
      // they don't represent code bugs. Don't filter them out."
      render(
        <RecentFailuresPanel
          failures={[
            makeFailure({
              category: 'daemon_restart_abandoned',
              displayCategory: 'dispatcher restarted',
            }),
            makeFailure({
              failureId: '2',
              category: 'paused_by_killswitch',
              displayCategory: 'manually stopped',
            }),
            makeFailure({
              failureId: '3',
              category: 'subprocess_crash',
              displayCategory: 'subprocess crashed',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      // All three category rows render their operator-friendly
      // displayCategory (#2948). The raw tokens are available via the
      // pill's data-category attribute for debugging.
      expect(screen.getByText('dispatcher restarted')).toBeInTheDocument();
      expect(screen.getByText('manually stopped')).toBeInTheDocument();
      expect(screen.getByText('subprocess crashed')).toBeInTheDocument();

      // Two amber ↺ glyphs and one red ✗ glyph.
      expect(screen.getAllByTestId('failure-glyph-infra_preempted')).toHaveLength(
        2,
      );
      expect(screen.getAllByTestId('failure-glyph-failed')).toHaveLength(1);
    });

    it('groups by category so the count column aggregates per category', () => {
      render(
        <RecentFailuresPanel
          failures={[
            makeFailure({
              failureId: '1',
              category: 'daemon_restart_abandoned',
              displayCategory: 'dispatcher restarted',
            }),
            makeFailure({
              failureId: '2',
              category: 'daemon_restart_abandoned',
              displayCategory: 'dispatcher restarted',
            }),
            makeFailure({
              failureId: '3',
              category: 'daemon_restart_abandoned',
              displayCategory: 'dispatcher restarted',
            }),
            makeFailure({
              failureId: '4',
              category: 'subprocess_crash',
              displayCategory: 'subprocess crashed',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      // Row for daemon_restart_abandoned shows count=3; grab its row
      // via the pill's data-category attribute (the raw token is still
      // available for querying even though the visible text is the
      // rephrased string).
      const daemonRestartPill = screen.getByTestId(
        'failure-category-pill-daemon_restart_abandoned',
      );
      const daemonRestartRow = daemonRestartPill.closest('tr')!;
      expect(within(daemonRestartRow).getByText('3')).toBeInTheDocument();

      const subprocessCrashPill = screen.getByTestId(
        'failure-category-pill-subprocess_crash',
      );
      const subprocessCrashRow = subprocessCrashPill.closest('tr')!;
      expect(within(subprocessCrashRow).getByText('1')).toBeInTheDocument();
    });
  });

  // #2812: §2.2 — category pill is font-mono.
  it('§2.2 — category pill is font-mono', () => {
    render(
      <RecentFailuresPanel
        failures={[makeFailure({ category: 'subprocess_crash', displayCategory: 'subprocess crashed' })]}
        nowMs={NOW_MS}
      />,
    );
    const pill = screen.getByTestId('failure-category-pill-subprocess_crash');
    expect(pill.className).toContain('font-mono');
  });

  // #2948: the Category column renders the operator-friendly
  // `displayCategory` string instead of the raw category token,
  // matching the Recently Completed tooltip rephrasing from #2935.
  describe('category rephrase (#2948)', () => {
    it('renders the rephrased display category, not the raw token', () => {
      // Acceptance criterion #1: Recent Failures table's category
      // column renders the display names, not raw tokens.
      render(
        <RecentFailuresPanel
          failures={[
            makeFailure({
              category: 'subprocess_turn_limit',
              displayCategory: 'turn limit reached',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      expect(screen.getByText('turn limit reached')).toBeInTheDocument();
      // The raw token is absent from the visible text (the pill
      // renders the rephrased string in place of the token).
      expect(
        screen.queryByText('subprocess_turn_limit'),
      ).not.toBeInTheDocument();
    });

    it('preserves the raw category token on the cell title for debugging', () => {
      // Acceptance criterion #2: the raw category string is still
      // accessible for debugging — either as a tooltip on the cell or
      // as a data attribute. Both are present.
      render(
        <RecentFailuresPanel
          failures={[
            makeFailure({
              category: 'daemon_restart_abandoned',
              displayCategory: 'dispatcher restarted',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      const pill = screen.getByTestId(
        'failure-category-pill-daemon_restart_abandoned',
      );
      expect(pill).toHaveAttribute('title', 'daemon_restart_abandoned');
      expect(pill).toHaveAttribute(
        'data-category',
        'daemon_restart_abandoned',
      );
      expect(pill).toHaveTextContent('dispatcher restarted');
    });

    it('rephrases multiple well-known categories consistently', () => {
      // Sanity-check the happy-path rephrases called out in the issue
      // body: daemon_restart_abandoned, paused_by_killswitch,
      // ci_red_after_retries.
      const failures: DispatcherFailure[] = [
        makeFailure({
          failureId: 'f1',
          category: 'daemon_restart_abandoned',
          displayCategory: 'dispatcher restarted',
          ts: new Date(NOW_MS - 5 * 60 * 1000).toISOString(),
        }),
        makeFailure({
          failureId: 'f2',
          category: 'paused_by_killswitch',
          displayCategory: 'manually stopped',
          ts: new Date(NOW_MS - 6 * 60 * 1000).toISOString(),
        }),
        makeFailure({
          failureId: 'f3',
          category: 'ci_red_after_retries',
          displayCategory: 'CI failed after retries',
          ts: new Date(NOW_MS - 7 * 60 * 1000).toISOString(),
        }),
      ];
      render(<RecentFailuresPanel failures={failures} nowMs={NOW_MS} />);
      expect(screen.getByText('dispatcher restarted')).toBeInTheDocument();
      expect(screen.getByText('manually stopped')).toBeInTheDocument();
      expect(screen.getByText('CI failed after retries')).toBeInTheDocument();
    });

    it('falls through to the raw token when displayCategory === category (unknown category)', () => {
      // Acceptance criterion #3: unknown category (future additions
      // not yet in the map) falls through to the raw token as today.
      // The backend returns `category` verbatim as `displayCategory`
      // when the token isn't in the display-name map, so the UI just
      // renders whatever `displayCategory` contains.
      render(
        <RecentFailuresPanel
          failures={[
            makeFailure({
              category: 'some_new_category',
              displayCategory: 'some_new_category',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      expect(screen.getByText('some_new_category')).toBeInTheDocument();
      const pill = screen.getByTestId(
        'failure-category-pill-some_new_category',
      );
      expect(pill).toHaveAttribute('title', 'some_new_category');
    });
  });
});
