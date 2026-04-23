import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { OutcomeBadge } from '../OutcomeBadge';

describe('OutcomeBadge', () => {
  it('renders the formatted outcome label for granted', () => {
    render(<OutcomeBadge outcome="granted" />);
    expect(screen.getByText('Granted')).toBeInTheDocument();
  });

  it('renders the formatted outcome label for denied', () => {
    render(<OutcomeBadge outcome="denied" />);
    expect(screen.getByText('Denied')).toBeInTheDocument();
  });

  it('renders the formatted outcome label for granted_in_part', () => {
    render(<OutcomeBadge outcome="granted_in_part" />);
    expect(screen.getByText('Granted In Part')).toBeInTheDocument();
  });

  it('renders the formatted outcome label for denied_in_part', () => {
    render(<OutcomeBadge outcome="denied_in_part" />);
    expect(screen.getByText('Denied In Part')).toBeInTheDocument();
  });

  it('renders the formatted outcome label for moot', () => {
    render(<OutcomeBadge outcome="moot" />);
    expect(screen.getByText('Moot')).toBeInTheDocument();
  });

  it('renders the formatted outcome label for continued', () => {
    render(<OutcomeBadge outcome="continued" />);
    expect(screen.getByText('Continued')).toBeInTheDocument();
  });

  it('renders "Not classified" for null outcome', () => {
    render(<OutcomeBadge outcome={null} />);
    expect(screen.getByText('Not classified')).toBeInTheDocument();
  });

  // Color tests: verify semantic coloring per BRAND.md
  it('applies green tint classes for granted outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="granted" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-green-700');
    expect(badge.className).toContain('border-green-300');
  });

  it('applies green tint classes for granted_in_part outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="granted_in_part" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-green-700');
    expect(badge.className).toContain('border-green-300');
  });

  it('applies red tint classes for denied outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="denied" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-red-700');
    expect(badge.className).toContain('border-red-300');
  });

  it('applies red tint classes for denied_in_part outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="denied_in_part" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-red-700');
    expect(badge.className).toContain('border-red-300');
  });

  it('applies stone tint classes for moot outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="moot" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-stone-500');
    expect(badge.className).toContain('border-stone-300');
  });

  it('applies stone tint classes for continued outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="continued" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-stone-500');
    expect(badge.className).toContain('border-stone-300');
  });

  it('applies stone tint classes for off_calendar outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="off_calendar" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-stone-500');
    expect(badge.className).toContain('border-stone-300');
  });

  it('applies stone tint classes for submitted outcomes (#1941)', () => {
    const { container } = render(<OutcomeBadge outcome="submitted" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-stone-500');
    expect(badge.className).toContain('border-stone-300');
  });

  it('applies stone tint classes for other outcomes', () => {
    const { container } = render(<OutcomeBadge outcome="other" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('text-stone-500');
    expect(badge.className).toContain('border-stone-300');
  });

  // Structural tests
  it('renders as a pill badge (rounded-full)', () => {
    const { container } = render(<OutcomeBadge outcome="granted" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('rounded-full');
  });

  it('uses outline variant (has border)', () => {
    const { container } = render(<OutcomeBadge outcome="granted" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('border');
  });

  it('merges additional className prop', () => {
    const { container } = render(<OutcomeBadge outcome="granted" className="ml-2" />);
    const badge = container.firstChild as HTMLElement;
    expect(badge.className).toContain('ml-2');
    expect(badge.className).toContain('text-green-700');
  });
});
