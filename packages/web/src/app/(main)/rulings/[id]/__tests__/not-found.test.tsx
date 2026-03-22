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

import RulingNotFound from '../not-found';

describe('RulingNotFound', () => {
  it('renders the heading', () => {
    render(<RulingNotFound />);
    expect(screen.getByText('Ruling Not Found')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<RulingNotFound />);
    expect(
      screen.getByText(/does not exist or has not been captured/),
    ).toBeInTheDocument();
  });

  it('renders a link back to rulings', () => {
    render(<RulingNotFound />);
    const link = screen.getByText('Back to Rulings').closest('a');
    expect(link).toHaveAttribute('href', '/rulings');
  });
});
