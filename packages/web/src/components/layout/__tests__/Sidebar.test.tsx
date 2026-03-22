import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const mockPathname = vi.fn().mockReturnValue('/');

vi.mock('next/navigation', () => ({
  usePathname: () => mockPathname(),
}));

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

  it('renders the Research section with Cases and Judges links', () => {
    render(<Sidebar />);
    expect(screen.getByText('Research')).toBeInTheDocument();

    const casesLink = screen.getByText('Cases');
    expect(casesLink.closest('a')).toHaveAttribute('href', '/cases');

    const judgesLink = screen.getByText('Judges');
    expect(judgesLink.closest('a')).toHaveAttribute('href', '/judges');
  });

  it('renders navigation links with correct hrefs', () => {
    render(<Sidebar />);

    const searchLink = screen.getByText('Search Rulings');
    expect(searchLink.closest('a')).toHaveAttribute('href', '/search');

    const rulingsLink = screen.getByText('Latest Rulings');
    expect(rulingsLink.closest('a')).toHaveAttribute('href', '/rulings');
  });

  it('has aria-label on nav element', () => {
    render(<Sidebar />);
    expect(screen.getByRole('navigation', { name: 'Sidebar' })).toBeInTheDocument();
  });

  it('renders section labels as headings', () => {
    render(<Sidebar />);
    const headings = screen.getAllByRole('heading', { level: 2 });
    const headingTexts = headings.map((h) => h.textContent);
    expect(headingTexts).toContain('Explore');
    expect(headingTexts).toContain('Research');
  });

  it('active sidebar link has left border accent', () => {
    mockPathname.mockReturnValue('/judges');
    render(<Sidebar />);
    const judgesLink = screen.getByText('Judges').closest('a');
    expect(judgesLink).toBeTruthy();
    expect(judgesLink?.className).toContain('border-l-2');
    expect(judgesLink?.className).toContain('border-primary');
  });

  it('non-active sidebar link does not have left border accent', () => {
    mockPathname.mockReturnValue('/judges');
    render(<Sidebar />);
    const casesLink = screen.getByText('Cases').closest('a');
    expect(casesLink).toBeTruthy();
    expect(casesLink?.className).not.toContain('border-l-2');
  });
});
