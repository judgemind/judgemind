import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// Mock Apollo client
const mockUseQuery = vi.fn();
vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
}));

let mockSearchParamsValue = new URLSearchParams();
vi.mock('next/navigation', () => ({
  useSearchParams: () => mockSearchParamsValue,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
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

// Mock lucide-react icons
vi.mock('lucide-react', () => ({
  AlertCircle: ({ className }: { className?: string }) => (
    <span data-testid="alert-circle-icon" className={className} />
  ),
  ArrowLeft: ({ className }: { className?: string }) => (
    <span data-testid="arrow-left-icon" className={className} />
  ),
  BarChart3: ({ className }: { className?: string }) => (
    <span data-testid="bar-chart-icon" className={className} />
  ),
}));

import { JudgeComparison } from '../JudgeComparison';

const MOCK_ANALYTICS_DATA = {
  compareJudges: [
    {
      judgeId: 'judge-1',
      totalRulings: 50,
      rulingsByOutcome: [
        { outcome: 'granted', count: 30 },
        { outcome: 'denied', count: 15 },
        { outcome: 'granted_in_part', count: 5 },
      ],
      rulingsByMotionType: [
        {
          motionType: 'msj',
          total: 30,
          granted: 20,
          denied: 8,
          grantedInPart: 2,
          other: 0,
          grantRate: 0.667,
        },
        {
          motionType: 'demurrer',
          total: 20,
          granted: 10,
          denied: 7,
          grantedInPart: 3,
          other: 0,
          grantRate: 0.5,
        },
      ],
      earliestRuling: '2025-01-15',
      latestRuling: '2026-03-01',
    },
    {
      judgeId: 'judge-2',
      totalRulings: 30,
      rulingsByOutcome: [
        { outcome: 'granted', count: 10 },
        { outcome: 'denied', count: 18 },
        { outcome: 'granted_in_part', count: 2 },
      ],
      rulingsByMotionType: [
        {
          motionType: 'msj',
          total: 20,
          granted: 5,
          denied: 13,
          grantedInPart: 2,
          other: 0,
          grantRate: 0.25,
        },
      ],
      earliestRuling: '2025-06-01',
      latestRuling: '2026-02-15',
    },
  ],
};

const MOCK_JUDGE_INFO = {
  j0: {
    id: 'judge-1',
    canonicalName: 'Smith, John A.',
    department: '42',
    court: { county: 'Los Angeles', courtName: 'Superior Court of California' },
  },
  j1: {
    id: 'judge-2',
    canonicalName: 'Johnson, Robert M.',
    department: null,
    court: { county: 'Orange', courtName: 'Superior Court of California' },
  },
};

describe('JudgeComparison', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsValue = new URLSearchParams('ids=judge-1,judge-2');
  });

  it('shows message when fewer than 2 judge IDs provided', () => {
    mockSearchParamsValue = new URLSearchParams('ids=judge-1');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: undefined,
    });

    render(<JudgeComparison />);
    expect(screen.getByText(/Select at least 2 judges to compare/)).toBeInTheDocument();
  });

  it('shows message when no IDs provided', () => {
    mockSearchParamsValue = new URLSearchParams('');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: undefined,
    });

    render(<JudgeComparison />);
    expect(screen.getByText(/Select at least 2 judges to compare/)).toBeInTheDocument();
  });

  it('shows skeleton while loading', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: true,
      error: undefined,
    });

    render(<JudgeComparison />);
    expect(screen.getByTestId('comparison-skeleton')).toBeInTheDocument();
  });

  it('shows error banner on error', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: undefined,
        loading: false,
        error: new Error('Network error'),
      })
      .mockReturnValueOnce({
        data: undefined,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    expect(screen.getByText(/Failed to load judge comparison data/)).toBeInTheDocument();
  });

  it('renders judge names as column headers with links', () => {
    // First call: analytics query, second call: judge info query
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    // Judge names appear in both overview and motion type tables
    const smithLinks = screen.getAllByText('Smith, John A.');
    expect(smithLinks.length).toBeGreaterThanOrEqual(1);
    const johnsonLinks = screen.getAllByText('Johnson, Robert M.');
    expect(johnsonLinks.length).toBeGreaterThanOrEqual(1);
  });

  it('renders county information for each judge', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
    expect(screen.getByText('Orange')).toBeInTheDocument();
  });

  it('renders overall grant rates', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    // Smith: 30/(30+15+5) = 60%, Johnson: 10/(10+18+2) = 33%
    expect(screen.getByText('60%')).toBeInTheDocument();
    expect(screen.getByText('33%')).toBeInTheDocument();
  });

  it('renders total rulings count', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    expect(screen.getByText('50')).toBeInTheDocument();
    expect(screen.getByText('30')).toBeInTheDocument();
  });

  it('renders motion type comparison section', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    expect(screen.getByText('Grant Rate by Motion Type')).toBeInTheDocument();
    expect(screen.getByText('MSJ')).toBeInTheDocument();
    expect(screen.getByText('Demurrer')).toBeInTheDocument();
  });

  it('renders grant rates with count for motion types', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    // MSJ: Smith 67%, Johnson 25%
    expect(screen.getByText('67%')).toBeInTheDocument();
    expect(screen.getByText('25%')).toBeInTheDocument();
    // Counts in parentheses — (20) appears for both MSJ (Johnson) and Demurrer (Smith)
    expect(screen.getByText('(30)')).toBeInTheDocument();
    expect(screen.getAllByText('(20)').length).toBeGreaterThanOrEqual(1);
  });

  it('renders em-dash for motion types a judge does not have', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    // Johnson has no demurrer rulings — should show em-dash
    const demurrerRow = screen.getByText('Demurrer').closest('tr');
    expect(demurrerRow?.textContent).toContain('\u2014');
  });

  it('renders back to judges link', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    const backLinks = screen.getAllByText(/Back to judges/);
    expect(backLinks.length).toBeGreaterThanOrEqual(1);
  });

  it('links judge names to their profile pages', () => {
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    const smithLinks = screen.getAllByText('Smith, John A.');
    const link = smithLinks[0].closest('a');
    expect(link).toHaveAttribute('href', '/judges/judge-1');
  });

  it('deduplicates judge IDs in URL params', () => {
    mockSearchParamsValue = new URLSearchParams('ids=judge-1,judge-1,judge-2');
    mockUseQuery
      .mockReturnValueOnce({
        data: MOCK_ANALYTICS_DATA,
        loading: false,
        error: undefined,
      })
      .mockReturnValueOnce({
        data: MOCK_JUDGE_INFO,
        loading: false,
        error: undefined,
      });

    render(<JudgeComparison />);
    // Smith appears in both overview and motion type table headers (2 tables x 1 column each = 2)
    // If dedup were broken, it would be 2 tables x 2 columns = 4
    const smithLinks = screen.getAllByText('Smith, John A.');
    expect(smithLinks).toHaveLength(2); // one per table header
  });
});
