import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { Wordmark } from '../Wordmark';

/** Helper: returns the outer wrapper span (parent of the "judge" text span). */
function getWrapper() {
  return screen.getByText('judge').parentElement!;
}

describe('Wordmark', () => {
  it('renders "judge" and "mind" as separate spans', () => {
    render(<Wordmark />);
    expect(screen.getByText('judge')).toBeInTheDocument();
    expect(screen.getByText('mind')).toBeInTheDocument();
  });

  it('renders "mind" with brand accent color class', () => {
    render(<Wordmark />);
    const mindSpan = screen.getByText('mind');
    expect(mindSpan.className).toContain('text-brand-accent');
    expect(mindSpan.className).toContain('dark:text-brand-accent-light');
  });

  it('renders "judge" in default foreground color (no brand-accent class)', () => {
    render(<Wordmark />);
    const judgeSpan = screen.getByText('judge');
    expect(judgeSpan.className).not.toContain('text-brand-accent');
  });

  it('applies sm size by default', () => {
    render(<Wordmark />);
    expect(getWrapper().className).toContain('text-lg');
  });

  it('applies md size', () => {
    render(<Wordmark size="md" />);
    expect(getWrapper().className).toContain('text-xl');
  });

  it('applies lg size', () => {
    render(<Wordmark size="lg" />);
    expect(getWrapper().className).toContain('text-2xl');
  });

  it('uses font-semibold by default (sm size)', () => {
    render(<Wordmark />);
    expect(getWrapper().className).toContain('font-semibold');
  });

  it('uses font-bold for lg size', () => {
    render(<Wordmark size="lg" />);
    expect(getWrapper().className).toContain('font-bold');
  });

  it('accepts and applies additional className', () => {
    render(<Wordmark className="ml-4" />);
    expect(getWrapper().className).toContain('ml-4');
  });

  it('renders as inline element (span)', () => {
    render(<Wordmark />);
    const wrapper = getWrapper();
    expect(wrapper.tagName).toBe('SPAN');
  });

  it('passes additional props via rest spread', () => {
    render(<Wordmark data-testid="wordmark" />);
    expect(screen.getByTestId('wordmark')).toBeInTheDocument();
  });
});
