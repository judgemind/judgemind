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

import CaseNotFound from '../not-found';

describe('CaseNotFound', () => {
  it('renders the heading', () => {
    render(<CaseNotFound />);
    expect(screen.getByText('Case Not Found')).toBeInTheDocument();
  });

  it('renders the description', () => {
    render(<CaseNotFound />);
    expect(
      screen.getByText(/does not exist or has not been captured/),
    ).toBeInTheDocument();
  });

  it('renders a link back to cases', () => {
    render(<CaseNotFound />);
    const link = screen.getByText('Back to Cases').closest('a');
    expect(link).toHaveAttribute('href', '/cases');
  });
});
