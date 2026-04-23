import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const mockUseQuery = vi.fn();
const mockPush = vi.fn();

vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
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

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn(), back: vi.fn() }),
}));

vi.mock('lucide-react', () => ({
  Search: ({ className }: { className?: string }) => (
    <span data-testid="search-icon" className={className} />
  ),
}));

import HomePage from '../(main)/page';

const MOCK_STATS = {
  platformStats: {
    countiesCount: 12,
    rulingsCount: 5432,
    judgesCount: 87,
  },
};

describe('HomePage', () => {
  it('renders the heading', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    expect(
      screen.getByText('Free legal research for everyone'),
    ).toBeInTheDocument();
  });

  it('renders the description with wordmark', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    expect(
      screen.getByText(/captures California tentative rulings/),
    ).toBeInTheDocument();
    // Wordmark renders "judge" and "mind" as separate spans
    expect(screen.getByText('judge')).toBeInTheDocument();
    expect(screen.getByText('mind')).toBeInTheDocument();
  });

  it('renders CTA buttons using Button component (no inline bg-brand-600)', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    const searchLink = screen.getByText('Advanced search').closest('a');
    expect(searchLink).toHaveAttribute('href', '/search');
    // The link should NOT have bg-brand-600 inline styles
    expect(searchLink?.className).not.toContain('bg-brand-600');

    const rulingsLink = screen.getByText('Latest rulings').closest('a');
    expect(rulingsLink).toHaveAttribute('href', '/rulings');
  });

  it('renders the stats bar when data is loaded', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_STATS, loading: false, error: null });
    render(<HomePage />);
    expect(screen.getByTestId('stats-bar')).toBeInTheDocument();
    expect(screen.getByText('Counties covered')).toBeInTheDocument();
    expect(screen.getByText('Rulings captured')).toBeInTheDocument();
    expect(screen.getByText('Judges tracked')).toBeInTheDocument();
  });

  it('renders stat values formatted correctly', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_STATS, loading: false, error: null });
    render(<HomePage />);
    // 12 counties should show as "12"
    expect(screen.getByText('12')).toBeInTheDocument();
    // 5432 rulings should show as "5.4k"
    expect(screen.getByText('5.4k')).toBeInTheDocument();
    // 87 judges should show as "87"
    expect(screen.getByText('87')).toBeInTheDocument();
  });

  it('renders the how it works section', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    expect(screen.getByTestId('how-it-works')).toBeInTheDocument();
    expect(screen.getByText('How it works')).toBeInTheDocument();
    expect(screen.getByText('We capture tentative rulings daily')).toBeInTheDocument();
    expect(screen.getByText('Search by keyword, judge, or case')).toBeInTheDocument();
    expect(screen.getByText('Track judge analytics over time')).toBeInTheDocument();
  });

  it('shows skeleton loading state for stats', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    // Stats bar should still be present with skeletons
    expect(screen.getByTestId('stats-bar')).toBeInTheDocument();
  });

  it('does not render a recent rulings section', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_STATS, loading: false, error: null });
    render(<HomePage />);
    expect(screen.queryByTestId('recent-rulings')).not.toBeInTheDocument();
    expect(screen.queryByText('Recent rulings')).not.toBeInTheDocument();
  });

  it('does not contain bg-brand-600 in the rendered output', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    const { container } = render(<HomePage />);
    // Check that no element has bg-brand-600 class
    const allElements = container.querySelectorAll('*');
    for (const el of allElements) {
      expect(el.className).not.toContain('bg-brand-600');
    }
  });

  it('renders a search bar in the hero area', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    const searchForm = screen.getByTestId('hero-search');
    expect(searchForm).toBeInTheDocument();
    const searchInput = screen.getByLabelText('Search rulings');
    expect(searchInput).toBeInTheDocument();
    expect(searchInput).toHaveAttribute('type', 'search');
    expect(searchInput).toHaveAttribute('name', 'q');
  });

  it('navigates to /search?q=<query> on form submission', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    mockPush.mockClear();
    render(<HomePage />);
    const searchInput = screen.getByLabelText('Search rulings');
    fireEvent.change(searchInput, { target: { value: 'summary judgment' } });
    const form = screen.getByTestId('hero-search');
    fireEvent.submit(form);
    expect(mockPush).toHaveBeenCalledWith('/search?q=summary+judgment');
  });

  it('does not navigate on empty search submission', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    mockPush.mockClear();
    render(<HomePage />);
    const form = screen.getByTestId('hero-search');
    fireEvent.submit(form);
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('renders the search bar as the most prominent interactive element', () => {
    mockUseQuery.mockReturnValue({ data: null, loading: true, error: null });
    render(<HomePage />);
    const searchInput = screen.getByLabelText('Search rulings');
    // The search input should have h-12 class making it taller than standard inputs
    expect(searchInput.className).toContain('h-12');
    // The hero search form should appear before the CTA buttons in the DOM
    const heroSearch = screen.getByTestId('hero-search');
    const advancedSearchLink = screen.getByText('Advanced search').closest('a');
    expect(heroSearch.compareDocumentPosition(advancedSearchLink!)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });
});
