import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  fetchMore: ReturnType<typeof vi.fn>;
};

// IntersectionObserver mock
let intersectionCallback: IntersectionObserverCallback;
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
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

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => mockQueryResult,
  };
});

import { RulingsFeed } from '../src/app/(main)/rulings/RulingsFeed';

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

    mockObserve = vi.fn();
    mockDisconnect = vi.fn();
    vi.stubGlobal(
      'IntersectionObserver',
      vi.fn((callback: IntersectionObserverCallback) => {
        intersectionCallback = callback;
        return {
          observe: mockObserve,
          disconnect: mockDisconnect,
          unobserve: vi.fn(),
        };
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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

  it('renders scroll sentinel when hasNextPage is true', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<RulingsFeed />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    mockQueryResult.fetchMore = vi.fn().mockResolvedValue({});
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<RulingsFeed />);
    intersectionCallback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    expect(mockQueryResult.fetchMore).toHaveBeenCalled();
  });

  it('does not show sentinel when hasNextPage is false', () => {
    mockQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<RulingsFeed />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
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
