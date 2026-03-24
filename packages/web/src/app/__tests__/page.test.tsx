import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const mockUseQuery = vi.fn();

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

vi.mock('@/lib/display-helpers', () => ({
  formatDate: (d: string) => d,
  formatOutcome: (o: string | null) => o ?? '\u2014',
  formatMotionType: (m: string | null) => m ?? '\u2014',
  formatJudgeName: (j: { canonicalName: string } | null) =>
    j?.canonicalName ?? '\u2014',
  getOutcomeBadgeVariant: () => 'outline',
  getOutcomeBadgeListClass: () => '',
}));

vi.mock('@/components/OutcomeBadge', () => ({
  OutcomeBadge: ({ outcome }: { outcome: string | null }) => (
    <span data-testid="outcome-badge">{outcome ?? '\u2014'}</span>
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

const MOCK_RULINGS = {
  rulings: {
    edges: [
      {
        node: {
          id: 'ruling-1',
          hearingDate: '2026-03-10',
          outcome: 'granted',
          motionType: 'Demurrer',
          department: '42',
          case: {
            id: 'case-1',
            caseNumber: '23STCV12345',
            caseTitle: 'Smith v. Jones',
            court: {
              county: 'Los Angeles',
              courtName: 'Superior Court of California',
            },
          },
          judge: {
            canonicalName: 'Smith, John A.',
          },
        },
      },
    ],
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
    const searchLink = screen.getByText('Search rulings').closest('a');
    expect(searchLink).toHaveAttribute('href', '/search');
    // The link should NOT have bg-brand-600 inline styles
    expect(searchLink?.className).not.toContain('bg-brand-600');

    const rulingsLink = screen.getByText('Latest rulings').closest('a');
    expect(rulingsLink).toHaveAttribute('href', '/rulings');
  });

  it('renders the stats bar when data is loaded', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return { data: MOCK_RULINGS, loading: false, error: null };
    });
    render(<HomePage />);
    expect(screen.getByTestId('stats-bar')).toBeInTheDocument();
    expect(screen.getByText('Counties covered')).toBeInTheDocument();
    expect(screen.getByText('Rulings captured')).toBeInTheDocument();
    expect(screen.getByText('Judges tracked')).toBeInTheDocument();
  });

  it('renders stat values formatted correctly', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return { data: MOCK_RULINGS, loading: false, error: null };
    });
    render(<HomePage />);
    // 12 counties should show as "12"
    expect(screen.getByText('12')).toBeInTheDocument();
    // 5432 rulings should show as "5.4k"
    expect(screen.getByText('5.4k')).toBeInTheDocument();
    // 87 judges should show as "87"
    expect(screen.getByText('87')).toBeInTheDocument();
  });

  it('renders recent rulings section', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return { data: MOCK_RULINGS, loading: false, error: null };
    });
    render(<HomePage />);
    expect(screen.getByTestId('recent-rulings')).toBeInTheDocument();
    expect(screen.getByText('Recent rulings')).toBeInTheDocument();
    expect(screen.getByText('View all')).toBeInTheDocument();
  });

  it('renders ruling cards with case info', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return { data: MOCK_RULINGS, loading: false, error: null };
    });
    render(<HomePage />);
    expect(screen.getByText(/23STCV12345/)).toBeInTheDocument();
    expect(screen.getByText(/Smith v\. Jones/)).toBeInTheDocument();
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

  it('shows error state for recent rulings', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return { data: null, loading: false, error: new Error('Network error') };
    });
    render(<HomePage />);
    expect(screen.getByText('Could not load recent rulings.')).toBeInTheDocument();
  });

  it('shows empty state for recent rulings', () => {
    mockUseQuery.mockImplementation((query: string) => {
      if (query.includes('PlatformStats')) {
        return { data: MOCK_STATS, loading: false, error: null };
      }
      return {
        data: { rulings: { edges: [] } },
        loading: false,
        error: null,
      };
    });
    render(<HomePage />);
    expect(screen.getByText(/No rulings yet/)).toBeInTheDocument();
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
});
