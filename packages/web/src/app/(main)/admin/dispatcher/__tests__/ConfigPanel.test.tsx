/**
 * Tests for ConfigPanel's spawn-frozen field display logic (#2990).
 *
 * Acceptance criteria:
 *   - null spawnFrozenUntil → field hidden entirely
 *   - past spawnFrozenUntil → field hidden
 *   - ≤15 min remaining → "new agents paused for ~N min"
 *   - >15 min remaining → "new agents paused until H:MM AM/PM PT"
 *   - title tooltip contains "Resumes"
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ConfigPanel } from '../ConfigPanel';

const NOOP_COMMIT = vi.fn().mockResolvedValue(undefined);

function renderConfigPanel(spawnFrozenUntil: string | null) {
  return render(
    <ConfigPanel
      entries={[]}
      spawnFrozenUntil={spawnFrozenUntil}
      onCommitEdit={NOOP_COMMIT}
    />,
  );
}

describe('ConfigPanel — spawn frozen field (#2990)', () => {
  it('renders nothing paused-related when spawnFrozenUntil is null', () => {
    renderConfigPanel(null);
    expect(screen.queryByText(/paused/i)).toBeNull();
  });

  it('renders nothing paused-related when spawnFrozenUntil is 1 minute in the past', () => {
    const past = new Date(Date.now() - 60_000).toISOString();
    renderConfigPanel(past);
    expect(screen.queryByText(/paused/i)).toBeNull();
  });

  it('renders nothing paused-related when spawnFrozenUntil is exactly now', () => {
    // Exactly now counts as "past" (not strictly future).
    const now = new Date(Date.now()).toISOString();
    renderConfigPanel(now);
    expect(screen.queryByText(/paused/i)).toBeNull();
  });

  it('renders "new agents paused for ~7 min" when 7 minutes remain', () => {
    const future = new Date(Date.now() + 7 * 60_000).toISOString();
    renderConfigPanel(future);
    expect(screen.getByText(/new agents paused for ~7 min/i)).toBeInTheDocument();
  });

  it('renders "new agents paused for ~1 min" when 30 seconds remain (floor at 1)', () => {
    const future = new Date(Date.now() + 30_000).toISOString();
    renderConfigPanel(future);
    expect(screen.getByText(/new agents paused for ~1 min/i)).toBeInTheDocument();
  });

  it('renders "new agents paused for ~15 min" when exactly 15 minutes remain', () => {
    // 15 min is still the "short" branch (≤15 min).
    const future = new Date(Date.now() + 15 * 60_000).toISOString();
    renderConfigPanel(future);
    expect(screen.getByText(/new agents paused for ~15 min/i)).toBeInTheDocument();
  });

  it('renders "new agents paused until ... PT" when 20 minutes remain', () => {
    const future = new Date(Date.now() + 20 * 60_000).toISOString();
    renderConfigPanel(future);
    // The "until" variant uses Pacific Time clock.
    expect(screen.getByText(/new agents paused until .+ PT/i)).toBeInTheDocument();
    // Does NOT say "for ~N min".
    expect(screen.queryByText(/for ~\d+ min/i)).toBeNull();
  });

  it('title tooltip on the freeze label contains "Resumes"', () => {
    const future = new Date(Date.now() + 7 * 60_000).toISOString();
    renderConfigPanel(future);
    const label = screen.getByText(/new agents paused/i).closest('[title]') as HTMLElement | null;
    expect(label).not.toBeNull();
    expect(label!.getAttribute('title')).toContain('Resumes');
  });

  it('title tooltip on the "until" variant also contains "Resumes"', () => {
    const future = new Date(Date.now() + 20 * 60_000).toISOString();
    renderConfigPanel(future);
    const label = screen.getByText(/new agents paused until/i).closest('[title]') as HTMLElement | null;
    expect(label).not.toBeNull();
    expect(label!.getAttribute('title')).toContain('Resumes');
  });

  it('freeze label is not rendered (structurally absent) when inactive', () => {
    renderConfigPanel(null);
    // The config strip is still present for the editable fields.
    expect(screen.getByTestId('config-strip')).toBeInTheDocument();
    // But no separator or freeze text.
    expect(screen.queryByText(/spawn frozen/i)).toBeNull();
  });
});
