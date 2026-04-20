import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { RecentFailuresPanel } from '../RecentFailuresPanel';
import type { DispatcherFailure } from '@/lib/dispatcher-queries';

// Fixed `nowMs` for deterministic relative-time rendering.
const NOW_MS = Date.parse('2026-04-20T12:00:00Z');

function makeFailure(overrides: Partial<DispatcherFailure> = {}): DispatcherFailure {
  return {
    failureId: '1',
    agentId: 'agent-1',
    category: 'subprocess_crash',
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
            makeFailure({ category: 'daemon_restart_abandoned' }),
            makeFailure({
              failureId: '2',
              category: 'paused_by_killswitch',
            }),
            makeFailure({
              failureId: '3',
              category: 'subprocess_crash',
            }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      // Both infra categories + the real failure category are rendered.
      expect(screen.getByText('daemon_restart_abandoned')).toBeInTheDocument();
      expect(screen.getByText('paused_by_killswitch')).toBeInTheDocument();
      expect(screen.getByText('subprocess_crash')).toBeInTheDocument();

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
            makeFailure({ failureId: '1', category: 'daemon_restart_abandoned' }),
            makeFailure({ failureId: '2', category: 'daemon_restart_abandoned' }),
            makeFailure({ failureId: '3', category: 'daemon_restart_abandoned' }),
            makeFailure({ failureId: '4', category: 'subprocess_crash' }),
          ]}
          nowMs={NOW_MS}
        />,
      );
      // Row for daemon_restart_abandoned shows count=3; grab its row
      // and assert the count cell within.
      const daemonRestartRow = screen.getByText('daemon_restart_abandoned')
        .closest('tr')!;
      expect(within(daemonRestartRow).getByText('3')).toBeInTheDocument();
      const subprocessCrashRow = screen.getByText('subprocess_crash')
        .closest('tr')!;
      expect(within(subprocessCrashRow).getByText('1')).toBeInTheDocument();
    });
  });
});
