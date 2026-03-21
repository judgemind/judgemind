import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

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

vi.mock('@/lib/display-helpers', () => ({
  formatDate: (d: string) => d,
  formatOutcome: (o: string | null) => o ?? '\u2014',
  formatMotionType: (m: string | null) => m ?? '\u2014',
  formatJudgeName: (j: { canonicalName: string } | null) =>
    j?.canonicalName ?? '\u2014',
  getOutcomeBadgeClass: () => 'bg-slate-100 text-slate-600',
}));

import { RulingsFeed } from '../RulingsFeed';

const MOCK_RULING_NODE = {
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
};

const MOCK_RULINGS_DATA = {
  rulings: {
    edges: [
      { cursor: 'cursor-1', node: MOCK_RULING_NODE },
      {
        cursor: 'cursor-2',
        node: {
          ...MOCK_RULING_NODE,
          id: 'ruling-2',
          outcome: 'denied',
          case: {
            ...MOCK_RULING_NODE.case,
            id: 'case-2',
            caseNumber: '23STCV67890',
            caseTitle: 'Doe v. Roe',
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

// IntersectionObserver mock
let intersectionCallback: IntersectionObserverCallback;
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
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

describe('RulingsFeed', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders skeleton cards while loading initial data', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const skeletons = screen.getAllByTestId('skeleton-card');
    expect(skeletons.length).toBe(8);
  });

  it('renders error state', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.getByText(/Failed to load rulings/)).toBeInTheDocument();
  });

  it('renders empty state when no rulings found', () => {
    mockUseQuery.mockReturnValue({
      data: {
        rulings: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.getByText(/No rulings found/)).toBeInTheDocument();
  });

  it('renders ruling cards with case data', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.getByText(/23STCV12345/)).toBeInTheDocument();
    expect(screen.getByText(/23STCV67890/)).toBeInTheDocument();
  });

  it('renders outcome and motion type badges', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    // Outcome badges
    expect(screen.getByText('granted')).toBeInTheDocument();
    expect(screen.getByText('denied')).toBeInTheDocument();
    // Motion type badges
    expect(screen.getAllByText('Demurrer').length).toBe(2);
  });

  it('renders sentinel element when hasNextPage is true', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockUseQuery.mockReturnValue({
      data: {
        rulings: {
          ...MOCK_RULINGS_DATA.rulings,
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('sets up IntersectionObserver on the sentinel element', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<RulingsFeed />);

    // Simulate the sentinel becoming visible
    intersectionCallback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-2' },
      }),
    );
  });

  it('does not call fetchMore when sentinel is not intersecting', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<RulingsFeed />);

    intersectionCallback(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).not.toHaveBeenCalled();
  });

  it('does not call fetchMore when already loading', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: true,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<RulingsFeed />);
    // Sentinel is not rendered when loading, so fetchMore should not be called
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
    expect(mockFetchMore).not.toHaveBeenCalled();
  });

  it('shows skeleton cards while fetching more results', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    // Should show 3 skeleton cards for loading more (not the initial 8)
    const skeletons = screen.getAllByTestId('skeleton-card');
    expect(skeletons.length).toBe(3);
  });

  it('does not render sentinel while loading more results', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    // Sentinel hidden during loading to prevent duplicate fetches
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('disconnects observer on unmount', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { unmount } = render(<RulingsFeed />);
    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('renders ruling links pointing to ruling detail pages', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const link = screen.getByText(/23STCV12345/).closest('a');
    expect(link).toHaveAttribute('href', '/rulings/ruling-1');
  });

  it('renders filter inputs', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(
      screen.getByPlaceholderText(/County/),
    ).toBeInTheDocument();
  });
});
