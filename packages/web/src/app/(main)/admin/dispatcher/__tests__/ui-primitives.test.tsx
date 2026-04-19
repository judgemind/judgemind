import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { IssueLink, OutcomePill, PriorityBadge, PRLink } from '../ui-primitives';

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

  it('renders a fallback for unknown status', () => {
    render(<OutcomePill status="bogus" />);
    expect(screen.getByText('?')).toBeInTheDocument();
  });
});
