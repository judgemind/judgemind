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

// Mock lucide-react icons
vi.mock('lucide-react', () => ({
  Search: ({ className }: { className?: string }) => (
    <span data-testid="search-icon" className={className} />
  ),
}));

import { JudgesList } from '../JudgesList';

const MOCK_JUDGES_DATA = {
  judges: {
    edges: [
      {
        cursor: 'cursor-1',
        node: {
          id: 'judge-1',
          canonicalName: 'Smith, John A.',
          department: '42',
          isActive: true,
          court: {
            courtName: 'Superior Court of California',
            county: 'Los Angeles',
          },
        },
      },
      {
        cursor: 'cursor-2',
        node: {
          id: 'judge-2',
          canonicalName: 'Johnson, Robert M.',
          department: null,
          isActive: false,
          court: {
            courtName: 'Superior Court of California',
            county: 'Orange',
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

describe('JudgesList', () => {
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

    const { container } = render(<JudgesList />);
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

    render(<JudgesList />);
    expect(screen.getByText(/Failed to load judges/)).toBeInTheDocument();
  });

  it('renders empty state when no judges found', () => {
    mockUseQuery.mockReturnValue({
      data: {
        judges: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText(/No judges found/)).toBeInTheDocument();
  });

  it('renders judge names', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Smith, John A.')).toBeInTheDocument();
    expect(screen.getByText('Johnson, Robert M.')).toBeInTheDocument();
  });

  it('renders court info for each judge', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText(/Los Angeles/)).toBeInTheDocument();
    expect(screen.getByText(/Orange/)).toBeInTheDocument();
  });

  it('renders active/inactive badges', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Inactive')).toBeInTheDocument();
  });

  it('renders department info when available', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText(/Dept\. 42/)).toBeInTheDocument();
  });

  it('renders judge links pointing to detail pages', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const link = screen.getByText('Smith, John A.').closest('a');
    expect(link).toHaveAttribute('href', '/judges/judge-1');
  });

  it('renders Load more button when hasNextPage is true', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Load more')).toBeInTheDocument();
  });

  it('calls fetchMore when Load more is clicked', () => {
    const mockFetchMore = vi.fn();
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<JudgesList />);
    fireEvent.click(screen.getByText('Load more'));
    expect(mockFetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-2' },
      }),
    );
  });

  it('renders filter input for name search', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(
      screen.getByLabelText(/Judge name/i),
    ).toBeInTheDocument();
  });

  it('filters judges client-side by name', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    const input = screen.getByLabelText(/Judge name/i);
    fireEvent.change(input, { target: { value: 'Smith' } });

    expect(screen.getByText('Smith, John A.')).toBeInTheDocument();
    expect(screen.queryByText('Johnson, Robert M.')).not.toBeInTheDocument();
  });

  it('uses shadcn Table component', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<JudgesList />);
    expect(container.querySelector('table')).toBeInTheDocument();
  });

  it('renders table column headers', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_JUDGES_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<JudgesList />);
    expect(screen.getByText('Judge')).toBeInTheDocument();
    expect(screen.getByText('County')).toBeInTheDocument();
    expect(screen.getByText('Dept.')).toBeInTheDocument();
    expect(screen.getByText('Status')).toBeInTheDocument();
  });
});
