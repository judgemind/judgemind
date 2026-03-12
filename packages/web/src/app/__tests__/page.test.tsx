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

import HomePage from '../page';

describe('HomePage', () => {
  it('renders the heading', () => {
    render(<HomePage />);
    expect(
      screen.getByText('Free legal research for everyone'),
    ).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<HomePage />);
    expect(
      screen.getByText(/Judgemind captures California tentative rulings/),
    ).toBeInTheDocument();
  });

  it('renders a Search rulings link', () => {
    render(<HomePage />);
    const link = screen.getByText('Search rulings').closest('a');
    expect(link).toHaveAttribute('href', '/search');
  });

  it('renders a Latest rulings link', () => {
    render(<HomePage />);
    const link = screen.getByText('Latest rulings').closest('a');
    expect(link).toHaveAttribute('href', '/rulings');
  });
});
