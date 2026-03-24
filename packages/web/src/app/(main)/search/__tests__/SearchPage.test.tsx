import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  buildSearchParams,
  parseSearchParams,
  MOTION_TYPES,
  MOTION_TYPE_LABELS,
  OUTCOMES,
  OUTCOME_LABELS,
} from '../SearchPage';

// ---------------------------------------------------------------------------
// buildSearchParams — URL encoding of filter state (#1105)
// ---------------------------------------------------------------------------

describe('buildSearchParams', () => {
  it('includes motionTypes when non-empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: ['demurrer', 'msj'],
      outcomes: [],
    });
    expect(params.get('motion')).toBe('demurrer,msj');
  });

  it('includes outcomes when non-empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: ['granted', 'denied'],
    });
    expect(params.get('outcome')).toBe('granted,denied');
  });

  it('omits motionTypes and outcomes when empty', () => {
    const params = buildSearchParams({
      q: 'test',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
    });
    expect(params.has('motion')).toBe(false);
    expect(params.has('outcome')).toBe(false);
  });

  it('includes all filter fields when populated', () => {
    const params = buildSearchParams({
      q: 'summary judgment',
      county: 'Los Angeles',
      judgeName: 'Smith',
      dateFrom: '2026-01-01',
      dateTo: '2026-03-01',
      motionTypes: ['msj'],
      outcomes: ['granted'],
    });
    expect(params.get('q')).toBe('summary judgment');
    expect(params.get('county')).toBe('Los Angeles');
    expect(params.get('judge')).toBe('Smith');
    expect(params.get('dateFrom')).toBe('2026-01-01');
    expect(params.get('dateTo')).toBe('2026-03-01');
    expect(params.get('motion')).toBe('msj');
    expect(params.get('outcome')).toBe('granted');
  });
});

// ---------------------------------------------------------------------------
// parseSearchParams — URL decoding of filter state (#1105)
// ---------------------------------------------------------------------------

describe('parseSearchParams', () => {
  it('parses motionTypes from comma-separated string', () => {
    const params = new URLSearchParams('motion=demurrer,msj');
    const state = parseSearchParams(params);
    expect(state.motionTypes).toEqual(['demurrer', 'msj']);
  });

  it('parses outcomes from comma-separated string', () => {
    const params = new URLSearchParams('outcome=granted,denied');
    const state = parseSearchParams(params);
    expect(state.outcomes).toEqual(['granted', 'denied']);
  });

  it('returns empty arrays when motion and outcome are absent', () => {
    const params = new URLSearchParams('q=test');
    const state = parseSearchParams(params);
    expect(state.motionTypes).toEqual([]);
    expect(state.outcomes).toEqual([]);
  });

  it('round-trips through buildSearchParams', () => {
    const original = {
      q: 'test',
      county: 'Los Angeles',
      judgeName: 'Smith',
      dateFrom: '2026-01-01',
      dateTo: '2026-03-01',
      motionTypes: ['demurrer', 'msj'],
      outcomes: ['granted'],
    };
    const params = buildSearchParams(original);
    const parsed = parseSearchParams(params);
    expect(parsed).toEqual(original);
  });
});

// ---------------------------------------------------------------------------
// Constants — motion types and outcomes (#1105)
// ---------------------------------------------------------------------------

describe('MOTION_TYPES', () => {
  it('has labels for every motion type', () => {
    for (const mt of MOTION_TYPES) {
      expect(MOTION_TYPE_LABELS[mt]).toBeDefined();
    }
  });
});

describe('OUTCOMES', () => {
  it('has labels for every outcome', () => {
    for (const oc of OUTCOMES) {
      expect(OUTCOME_LABELS[oc]).toBeDefined();
    }
  });
});

// ---------------------------------------------------------------------------
// SearchPage component — infinite scroll (#1876)
// ---------------------------------------------------------------------------

const mockUseQuery = vi.fn();
vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual('@apollo/client');
  return {
    ...actual,
    useQuery: (...args: unknown[]) => mockUseQuery(...args),
  };
});

const mockReplace = vi.fn();
let mockSearchParamsValue = new URLSearchParams('q=test');

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

vi.mock('lucide-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('lucide-react')>();
  return {
    ...actual,
    Search: ({ className }: { className?: string }) => (
      <span data-testid="search-icon" className={className} />
    ),
    SlidersHorizontal: ({ className }: { className?: string }) => (
      <span data-testid="sliders-icon" className={className} />
    ),
    Calendar: ({ className }: { className?: string }) => (
      <span data-testid="calendar-icon" className={className} />
    ),
    Scale: ({ className }: { className?: string }) => (
      <span data-testid="scale-icon" className={className} />
    ),
    Gavel: ({ className }: { className?: string }) => (
      <span data-testid="gavel-icon" className={className} />
    ),
  };
});

vi.mock('@/lib/display-helpers', () => ({
  formatDate: (d: string) => d,
}));

vi.mock('@/lib/typography', () => ({
  PAGE_TITLE: 'text-2xl font-bold',
  SECTION_LABEL: 'text-sm font-medium',
}));

vi.mock('@/lib/sanitize-html', () => ({
  sanitizeExcerptHtml: (html: string) => html,
}));

vi.mock('@/components/OutcomeBadge', () => ({
  OutcomeBadge: ({ outcome }: { outcome: string | null }) => (
    <span data-testid="outcome-badge">{outcome ?? '\u2014'}</span>
  ),
}));

vi.mock('@/components/Autocomplete', () => ({
  Autocomplete: ({ value, onChange, placeholder, ...props }: { value: string; onChange: (v: string) => void; placeholder?: string; [key: string]: unknown }) => (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={props['aria-label'] as string}
    />
  ),
}));

vi.mock('@/lib/filter-options', () => ({
  useCountyOptions: () => ['Los Angeles', 'Orange'],
  useJudgeNameOptions: () => ['Smith, John'],
}));

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

// Must be imported after mocks are set up
import { SearchPage } from '../SearchPage';

const MOCK_SEARCH_DATA = {
  searchRulings: {
    edges: [
      {
        cursor: 'cursor-1',
        node: {
          rulingId: 'ruling-1',
          caseNumber: '24STCV01234',
          caseTitle: 'Smith v. Jones',
          court: 'Superior Court',
          county: 'Los Angeles',
          state: 'CA',
          judgeName: 'Smith, John',
          hearingDate: '2026-03-01',
          motionType: 'msj',
          outcome: 'granted',
          excerpt: 'The motion is <em>granted</em>.',
          score: 0.95,
        },
      },
      {
        cursor: 'cursor-2',
        node: {
          rulingId: 'ruling-2',
          caseNumber: '24STCV05678',
          caseTitle: 'Doe v. Roe',
          court: 'Superior Court',
          county: 'Orange',
          state: 'CA',
          judgeName: null,
          hearingDate: '2026-02-15',
          motionType: 'demurrer',
          outcome: 'denied',
          excerpt: null,
          score: 0.8,
        },
      },
    ],
    pageInfo: { hasNextPage: true, endCursor: 'cursor-2' },
    totalHits: 42,
  },
};

describe('SearchPage component — infinite scroll', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsValue = new URLSearchParams('q=test');
  });

  it('renders sentinel element when hasNextPage is true', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockUseQuery.mockReturnValue({
      data: {
        searchRulings: {
          ...MOCK_SEARCH_DATA.searchRulings,
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('sets up IntersectionObserver on the sentinel element', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<SearchPage />);

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
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: mockFetchMore,
    });

    render(<SearchPage />);

    intersectionCallback(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).not.toHaveBeenCalled();
  });

  it('does not render sentinel while loading more results', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: true,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    // Sentinel hidden during loading to prevent duplicate fetches
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('disconnects observer on unmount', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { unmount } = render(<SearchPage />);
    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('does not render a "Load more" button (uses infinite scroll instead)', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('renders search results with total hits count', () => {
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.getByText(/42 results found/)).toBeInTheDocument();
  });
});
