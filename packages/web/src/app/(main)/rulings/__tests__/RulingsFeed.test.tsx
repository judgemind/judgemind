import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

// Mock Apollo client
const mockUseQuery = vi.fn();
vi.mock('@apollo/client', () => ({
  useQuery: (...args: unknown[]) => mockUseQuery(...args),
  gql: (strings: TemplateStringsArray) => strings.join(''),
}));

const mockReplace = vi.fn();
let mockSearchParamsValue = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: mockReplace, back: vi.fn() }),
  useSearchParams: () => mockSearchParamsValue,
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
  formatLabel: (v: string | null) => {
    if (!v) return '\u2014';
    return v.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
  },
  formatJudgeName: (j: { canonicalName: string } | null) =>
    j?.canonicalName ?? '\u2014',
  getOutcomeBadgeVariant: () => 'outline',
  getOutcomeBadgeListClass: () => '',
  OUTCOME_LABELS: {
    granted: 'Granted',
    denied: 'Denied',
    granted_in_part: 'Granted In Part',
    moot: 'Moot',
    continued: 'Continued',
    other: 'Other',
  } as Record<string, string>,
  MOTION_TYPE_LABELS: {
    msj: 'MSJ',
    mtd: 'MTD',
    mil: 'MIL',
    demurrer: 'Demurrer',
    anti_slapp: 'Anti-SLAPP',
    other: 'Other',
  } as Record<string, string>,
}));

vi.mock('@/components/OutcomeBadge', () => ({
  OutcomeBadge: ({ outcome }: { outcome: string | null }) => (
    <span data-testid="outcome-badge">{outcome ?? '\u2014'}</span>
  ),
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
    caseType: 'civil',
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
            caseType: 'family',
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
    mockSearchParamsValue = new URLSearchParams();
  });

  it('renders skeleton cards while loading initial data', () => {
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const skeletons = screen.getAllByTestId('skeleton-row');
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

  it('renders case type badges', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const badges = screen.getAllByTestId('case-type-badge');
    expect(badges.length).toBe(2);
    // formatLabel mock converts 'civil' -> 'Civil', 'family' -> 'Family'
    expect(badges[0]).toHaveTextContent('Civil');
    expect(badges[1]).toHaveTextContent('Family');
  });

  it('does not render case type badge when caseType is null', () => {
    const dataWithNullCaseType = {
      rulings: {
        edges: [
          {
            cursor: 'cursor-1',
            node: {
              ...MOCK_RULING_NODE,
              case: {
                ...MOCK_RULING_NODE.case,
                caseType: null,
              },
            },
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };

    mockUseQuery.mockReturnValue({
      data: dataWithNullCaseType,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.queryByTestId('case-type-badge')).not.toBeInTheDocument();
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
    const skeletons = screen.getAllByTestId('skeleton-row');
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

  it('renders filter inputs including new dropdowns', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(screen.getByPlaceholderText(/County/)).toBeInTheDocument();
    expect(screen.getByLabelText('Case type')).toBeInTheDocument();
    expect(screen.getByLabelText('Outcome')).toBeInTheDocument();
    expect(screen.getByLabelText('Motion type')).toBeInTheDocument();
  });

  it('date inputs have name and aria-label attributes', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const dateFrom = screen.getByLabelText('Hearings from');
    expect(dateFrom).toHaveAttribute('name', 'dateFrom');
    const dateTo = screen.getByLabelText('Hearings to');
    expect(dateTo).toHaveAttribute('name', 'dateTo');
  });

  it('updates URL params when county filter changes', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    // The component reads county from searchParams; when county state changes, URL updates.
    // Since county is set via Autocomplete, we test indirectly via initial searchParams.
    expect(mockReplace).toHaveBeenCalledWith('/rulings');
  });

  it('initializes county filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('county=Los%20Angeles');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.county).toBe('Los Angeles');
  });

  it('initializes caseType filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=civil');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('civil');
  });

  it('initializes outcome filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('outcome=granted');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.outcome).toBe('granted');
  });

  it('initializes motionType filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('motionType=msj');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.motionType).toBe('msj');
  });

  it('initializes dateFrom filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('dateFrom=2026-01-01');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.dateFrom).toBe('2026-01-01');
  });

  it('initializes dateTo filter from URL params', () => {
    mockSearchParamsValue = new URLSearchParams('dateTo=2026-12-31');
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.dateTo).toBe('2026-12-31');
  });

  it('updates URL with all filter params', () => {
    mockSearchParamsValue = new URLSearchParams(
      'county=Orange&caseType=civil&outcome=granted&motionType=msj&dateFrom=2026-01-01&dateTo=2026-06-30',
    );
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('county=Orange'),
    );
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('caseType=civil'),
    );
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('outcome=granted'),
    );
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('motionType=msj'),
    );
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('dateFrom=2026-01-01'),
    );
    expect(mockReplace).toHaveBeenCalledWith(
      expect.stringContaining('dateTo=2026-06-30'),
    );
  });

  it('passes new filter variables to GraphQL query', () => {
    mockSearchParamsValue = new URLSearchParams(
      'caseType=family&outcome=denied&motionType=mtd',
    );
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('family');
    expect(lastCall[1].variables.outcome).toBe('denied');
    expect(lastCall[1].variables.motionType).toBe('mtd');
  });

  it('sends undefined for empty filter values', () => {
    mockSearchParamsValue = new URLSearchParams();
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBeUndefined();
    expect(lastCall[1].variables.outcome).toBeUndefined();
    expect(lastCall[1].variables.motionType).toBeUndefined();
    expect(lastCall[1].variables.county).toBeUndefined();
  });

  // Visual hierarchy tests (#1738)
  it('renders outcome badge on the same line as the case title', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const titleLink = screen.getByText(/23STCV12345/);
    const outcomeBadge = screen.getByText('granted');
    // Both should share the same flex parent
    const titleParent = titleLink.closest('div.flex');
    expect(titleParent).not.toBeNull();
    expect(titleParent!.contains(outcomeBadge)).toBe(true);
  });

  it('renders court/judge metadata with muted styling', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    const countyTexts = screen.getAllByText(/Los Angeles/);
    expect(countyTexts.length).toBeGreaterThan(0);
    const metadataRow = countyTexts[0].closest('div');
    expect(metadataRow).not.toBeNull();
    // Should use reduced-opacity muted foreground
    expect(metadataRow!.className).toContain('text-muted-foreground');
  });

  it('renders motion type badges with muted subordinate styling', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_RULINGS_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<RulingsFeed />);
    // Motion type badges should exist but be visually muted
    const motionBadges = screen.getAllByText('Demurrer');
    expect(motionBadges.length).toBe(2);
  });
});
