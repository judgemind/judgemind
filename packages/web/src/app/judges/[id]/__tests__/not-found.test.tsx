import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('next/link', () => ({
  default: ({
    children,
    href,
    ...props
  }: {
    children: React.ReactNode;
    href: string;
    [key: string]: unknown;
  }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));

import JudgeNotFound from '../not-found';

describe('JudgeNotFound', () => {
  it('renders the heading', () => {
    render(<JudgeNotFound />);
    expect(screen.getByText('Judge Not Found')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<JudgeNotFound />);
    expect(
      screen.getByText(/does not exist or has not been captured/),
    ).toBeInTheDocument();
  });

  it('renders a link back to home', () => {
    render(<JudgeNotFound />);
    const link = screen.getByText('Back to Home').closest('a');
    expect(link).toHaveAttribute('href', '/');
  });
});
