import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  fetchMore: ReturnType<typeof vi.fn>;
};

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

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => mockQueryResult,
  };
});

import { RulingsFeed } from '../src/app/rulings/RulingsFeed';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeRulingNode(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ruling-1',
    hearingDate: '2026-03-01',
    outcome: 'granted',
    motionType: 'msj',
    department: 'Dept. 5',
    case: {
      id: 'case-1',
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones',
      court: { county: 'Los Angeles', courtName: 'LA Superior Court' },
    },
    judge: { canonicalName: 'Smith, John' },
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RulingsFeed (render)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    };
  });

  it('shows skeleton rows while loading', () => {
    mockQueryResult.loading = true;
    const { container } = render(<RulingsFeed />);
    // Skeleton rows have the animate-pulse class
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows error message when query fails', () => {
    mockQueryResult.error = new Error('Network error');
    render(<RulingsFeed />);
    expect(
      screen.getByText('Failed to load rulings. Please try again.'),
    ).toBeInTheDocument();
  });

  it('shows empty state when no rulings found', () => {
    mockQueryResult.data = {
      rulings: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } },
    };
    render(<RulingsFeed />);
    expect(
      screen.getByText(/No rulings found/),
    ).toBeInTheDocument();
  });

  it('renders ruling rows with case, judge, motion, and outcome', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(screen.getByText('25STCV12345 — Smith v. Jones')).toBeInTheDocument();
    expect(screen.getByText('Smith, John')).toBeInTheDocument();
    expect(screen.getByText('MSJ')).toBeInTheDocument();
    expect(screen.getByText('Granted')).toBeInTheDocument();
  });

  it('shows em-dash for ruling with no case', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [
          { cursor: 'c1', node: makeRulingNode({ case: null }) },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(screen.getByText('\u2014')).toBeInTheDocument();
  });

  it('renders the load more button when hasNextPage is true', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<RulingsFeed />);
    expect(
      screen.getByRole('button', { name: 'Load more' }),
    ).toBeInTheDocument();
  });

  it('calls fetchMore when load more is clicked', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<RulingsFeed />);
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    expect(mockQueryResult.fetchMore).toHaveBeenCalled();
  });

  it('does not show load more when hasNextPage is false', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(
      screen.queryByRole('button', { name: 'Load more' }),
    ).not.toBeInTheDocument();
  });

  it('renders filter inputs', () => {
    render(<RulingsFeed />);
    expect(
      screen.getByPlaceholderText('County (e.g. Los Angeles)'),
    ).toBeInTheDocument();
    expect(screen.getByTitle('Hearings from')).toBeInTheDocument();
    expect(screen.getByTitle('Hearings to')).toBeInTheDocument();
  });

  it('renders "Unknown judge" for null judge', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [
          { cursor: 'c1', node: makeRulingNode({ judge: null }) },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(screen.getByText('Unknown judge')).toBeInTheDocument();
  });

  it('renders "Not classified" for null outcome', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [
          { cursor: 'c1', node: makeRulingNode({ outcome: null }) },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(screen.getByText('Not classified')).toBeInTheDocument();
  });
});
