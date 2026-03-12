import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock Apollo client
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

import { CasesList } from '../CasesList';

const MOCK_CASES_DATA = {
  cases: {
    edges: [
      {
        cursor: 'cursor-1',
        node: {
          id: 'case-1',
          caseNumber: '24STCV01234',
          caseTitle: 'Smith v. Jones',
          caseType: 'civil',
          caseStatus: 'active',
          court: {
            courtName: 'Superior Court of California',
            county: 'Los Angeles',
          },
        },
      },
      {
        cursor: 'cursor-2',
        node: {
          id: 'case-2',
          caseNumber: '24NNCV05678',
          caseTitle: null,
          caseType: null,
          caseStatus: 'closed',
          court: {
            courtName: 'Superior Court of California',
            county: 'San Bernardino',
          },
        },
      },
    ],
    pageInfo: {
      hasNextPage: true,
      endCursor: 'cursor-2',
    },
  },
};

describe('CasesList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders skeleton rows while loading', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<CasesList />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('renders error state', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(screen.getByText(/Failed to load cases/)).toBeInTheDocument();
  });

  it('renders empty state when no cases found', () => {
    mockUseQuery.mockReturnValue({
      data: {
        cases: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(screen.getByText(/No cases found/)).toBeInTheDocument();
  });

  it('renders case rows with case numbers and titles', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(screen.getByText('24STCV01234')).toBeInTheDocument();
    expect(screen.getByText('Smith v. Jones')).toBeInTheDocument();
    expect(screen.getByText('24NNCV05678')).toBeInTheDocument();
  });

  it('renders court info for each case', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(screen.getByText(/Los Angeles/)).toBeInTheDocument();
    expect(screen.getByText(/San Bernardino/)).toBeInTheDocument();
  });

  it('renders status badges for cases', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    // Status text appears both in the filter <select> options and as badges,
    // so use getAllByText to find all occurrences.
    const activeElements = screen.getAllByText('Active');
    expect(activeElements.length).toBeGreaterThanOrEqual(2); // select option + badge
    const closedElements = screen.getAllByText('Closed');
    expect(closedElements.length).toBeGreaterThanOrEqual(2); // select option + badge
  });

  it('renders case links pointing to detail pages', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    const link = screen.getByText('24STCV01234').closest('a');
    expect(link).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders Load more button when hasNextPage is true', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(screen.getByText('Load more')).toBeInTheDocument();
  });

  it('calls fetchMore when Load more is clicked', () => {
    const mockFetchMore = vi.fn();
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<CasesList />);
    fireEvent.click(screen.getByText('Load more'));
    expect(mockFetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-2' },
      }),
    );
  });

  it('renders filter inputs', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    expect(
      screen.getByPlaceholderText(/Case number or title/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Case status/i)).toBeInTheDocument();
  });

  it('filters cases client-side by case number', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_CASES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<CasesList />);
    const input = screen.getByPlaceholderText(/Case number or title/i);
    fireEvent.change(input, { target: { value: '24STCV' } });

    expect(screen.getByText('24STCV01234')).toBeInTheDocument();
    expect(screen.queryByText('24NNCV05678')).not.toBeInTheDocument();
  });
});
