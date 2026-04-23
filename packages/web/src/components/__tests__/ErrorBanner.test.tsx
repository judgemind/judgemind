import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { ErrorBanner, InlineError } from '../ErrorBanner';

// ---------------------------------------------------------------------------
// ErrorBanner (page-level error component)
// ---------------------------------------------------------------------------

describe('ErrorBanner', () => {
  it('renders the error message', () => {
    render(<ErrorBanner message="Failed to load search results." />);
    expect(screen.getByText('Failed to load search results.')).toBeInTheDocument();
  });

  it('renders an AlertCircle icon with aria-hidden', () => {
    const { container } = render(<ErrorBanner message="Error" />);
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
  });

  it('renders a "Try again" button', () => {
    render(<ErrorBanner message="Error" />);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('calls window.location.reload by default when "Try again" is clicked', () => {
    const reloadMock = vi.fn();
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload: reloadMock },
      writable: true,
    });

    render(<ErrorBanner message="Error" />);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(reloadMock).toHaveBeenCalled();
  });

  it('calls custom onRetry when provided', () => {
    const onRetry = vi.fn();
    render(<ErrorBanner message="Error" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('uses soft styling with text-red-700', () => {
    const { container } = render(<ErrorBanner message="Error" />);
    expect(container.querySelector('.text-red-700')).toBeInTheDocument();
  });

  it('uses bg-muted card styling', () => {
    const { container } = render(<ErrorBanner message="Error" />);
    expect(container.querySelector('.bg-muted')).toBeInTheDocument();
  });

  it('renders large icon (h-8 w-8)', () => {
    const { container } = render(<ErrorBanner message="Error" />);
    const svg = container.querySelector('svg');
    expect(svg?.classList.contains('h-8')).toBe(true);
    expect(svg?.classList.contains('w-8')).toBe(true);
  });

  it('merges additional className prop', () => {
    const { container } = render(<ErrorBanner message="Error" className="mt-4" />);
    // The outermost element should have the merged class
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('mt-4');
  });
});

// ---------------------------------------------------------------------------
// InlineError (inline viewer error component)
// ---------------------------------------------------------------------------

describe('InlineError', () => {
  it('renders the error message', () => {
    render(<InlineError message="Failed to load document." />);
    expect(screen.getByText('Failed to load document.')).toBeInTheDocument();
  });

  it('renders an AlertCircle icon with aria-hidden', () => {
    const { container } = render(<InlineError message="Error" />);
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
  });

  it('renders a "Try again" button', () => {
    render(<InlineError message="Error" />);
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('calls window.location.reload by default when "Try again" is clicked', () => {
    const reloadMock = vi.fn();
    Object.defineProperty(window, 'location', {
      value: { ...window.location, reload: reloadMock },
      writable: true,
    });

    render(<InlineError message="Error" />);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(reloadMock).toHaveBeenCalled();
  });

  it('calls custom onRetry when provided', () => {
    const onRetry = vi.fn();
    render(<InlineError message="Error" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(onRetry).toHaveBeenCalled();
  });

  it('uses soft styling with text-red-700', () => {
    const { container } = render(<InlineError message="Error" />);
    expect(container.querySelector('.text-red-700')).toBeInTheDocument();
  });

  it('renders small icon (h-4 w-4)', () => {
    const { container } = render(<InlineError message="Error" />);
    const svg = container.querySelector('svg');
    expect(svg?.classList.contains('h-4')).toBe(true);
    expect(svg?.classList.contains('w-4')).toBe(true);
  });

  it('applies data-testid when testId prop is provided', () => {
    render(<InlineError message="Error" testId="viewer-error" />);
    expect(screen.getByTestId('viewer-error')).toBeInTheDocument();
  });

  it('does not render data-testid when testId is not provided', () => {
    const { container } = render(<InlineError message="Error" />);
    expect(container.querySelector('[data-testid]')).toBeNull();
  });

  it('merges additional className prop', () => {
    const { container } = render(<InlineError message="Error" className="mt-3" />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain('mt-3');
  });

  it('does not use Card wrapper (inline layout)', () => {
    const { container } = render(<InlineError message="Error" />);
    // InlineError should not render inside a card — it's a simple div
    expect(container.querySelector('.rounded-lg.border.bg-card')).toBeNull();
  });
});
