import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import ErrorPage from '../error';

describe('ErrorPage (route-level error boundary)', () => {
  const mockReset = vi.fn();
  const mockError = Object.assign(new Error('Test error'), { digest: 'abc' });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('renders the error heading', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
  });

  it('renders the error description', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    expect(
      screen.getByText('An unexpected error occurred. Please try again.'),
    ).toBeInTheDocument();
  });

  it('renders a Try again button that calls reset', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    const button = screen.getByText('Try again');
    expect(button).toBeInTheDocument();
    fireEvent.click(button);
    expect(mockReset).toHaveBeenCalledOnce();
  });

  it('applies dark mode classes to the container border', () => {
    const { container } = render(
      <ErrorPage error={mockError} reset={mockReset} />,
    );
    const card = container.querySelector('.rounded-lg');
    expect(card?.className).toContain('dark:border-slate-700');
  });

  it('applies dark mode classes to the heading', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    const heading = screen.getByText('Something went wrong');
    expect(heading.className).toContain('dark:text-slate-100');
  });

  it('applies dark mode classes to the description', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    const desc = screen.getByText(
      'An unexpected error occurred. Please try again.',
    );
    expect(desc.className).toContain('dark:text-slate-400');
  });

  it('logs the error to console', () => {
    render(<ErrorPage error={mockError} reset={mockReset} />);
    expect(console.error).toHaveBeenCalledWith(
      'Application error:',
      mockError,
    );
  });
});
