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

  it('renders the Research section heading', () => {
    render(<Sidebar />);
    expect(screen.getByText('Research')).toBeInTheDocument();
  });

  it('renders navigation links with correct hrefs', () => {
    render(<Sidebar />);

    const searchLink = screen.getByText('Search Rulings');
    expect(searchLink.closest('a')).toHaveAttribute('href', '/search');

    const rulingsLink = screen.getByText('Latest Rulings');
    expect(rulingsLink.closest('a')).toHaveAttribute('href', '/rulings');

    const casesLink = screen.getByText('Cases');
    expect(casesLink.closest('a')).toHaveAttribute('href', '/cases');

    const judgesLink = screen.getByText('Judges');
    expect(judgesLink.closest('a')).toHaveAttribute('href', '/judges');
  });
});
