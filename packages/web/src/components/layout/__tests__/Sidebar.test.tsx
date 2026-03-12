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

import { Sidebar } from '../Sidebar';

describe('Sidebar', () => {
  it('renders the Explore section heading', () => {
    render(<Sidebar />);
    expect(screen.getByText('Explore')).toBeInTheDocument();
  });

  it('does not render the Research section (cases/judges pages not yet built)', () => {
    render(<Sidebar />);
    expect(screen.queryByText('Research')).not.toBeInTheDocument();
    expect(screen.queryByText('Cases')).not.toBeInTheDocument();
    expect(screen.queryByText('Judges')).not.toBeInTheDocument();
  });

  it('renders navigation links with correct hrefs', () => {
    render(<Sidebar />);

    const searchLink = screen.getByText('Search Rulings');
    expect(searchLink.closest('a')).toHaveAttribute('href', '/search');

    const rulingsLink = screen.getByText('Latest Rulings');
    expect(rulingsLink.closest('a')).toHaveAttribute('href', '/rulings');
  });
});
