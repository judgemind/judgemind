import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import GlobalError from '../global-error';

describe('GlobalError (root-level error boundary)', () => {
  const mockReset = vi.fn();
  const mockError = Object.assign(new Error('Critical failure'), {
    digest: 'xyz',
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the error heading', () => {
    render(<GlobalError error={mockError} reset={mockReset} />);
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
  });

  it('renders a critical error description', () => {
    render(<GlobalError error={mockError} reset={mockReset} />);
    expect(
      screen.getByText('A critical error occurred. Please try again.'),
    ).toBeInTheDocument();
  });

  it('renders a Try again button that calls reset', () => {
    render(<GlobalError error={mockError} reset={mockReset} />);
    const button = screen.getByText('Try again');
    expect(button).toBeInTheDocument();
    fireEvent.click(button);
    expect(mockReset).toHaveBeenCalledOnce();
  });

  it('applies semantic token classes to the body', () => {
    const { container } = render(
      <GlobalError error={mockError} reset={mockReset} />,
    );
    const renderedBody = container.querySelector('body');
    expect(renderedBody).not.toBeNull();
    expect(renderedBody!.className).toContain('bg-background');
    expect(renderedBody!.className).toContain('text-foreground');
  });

  it('applies semantic token classes to the card border', () => {
    const { container } = render(
      <GlobalError error={mockError} reset={mockReset} />,
    );
    const card = container.querySelector('.rounded-lg');
    expect(card?.className).toContain('border-border');
  });

  it('applies semantic token classes to the heading', () => {
    render(<GlobalError error={mockError} reset={mockReset} />);
    const heading = screen.getByText('Something went wrong');
    expect(heading.className).toContain('text-foreground');
  });

  it('applies semantic token classes to the description', () => {
    render(<GlobalError error={mockError} reset={mockReset} />);
    const desc = screen.getByText(
      'A critical error occurred. Please try again.',
    );
    expect(desc.className).toContain('text-muted-foreground');
  });

  it('includes theme detection script in head', () => {
    const { container } = render(
      <GlobalError error={mockError} reset={mockReset} />,
    );
    const scripts = container.querySelectorAll('script');
    const themeScript = Array.from(scripts).find((s) =>
      s.innerHTML.includes('prefers-color-scheme'),
    );
    expect(themeScript).toBeDefined();
  });
});
