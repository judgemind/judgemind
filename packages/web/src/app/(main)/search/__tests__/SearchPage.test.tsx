import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import {
  buildSearchParams,
  parseSearchParams,
  MOTION_TYPES,
  OUTCOMES,
  CASE_TYPES,
} from '../SearchPage';
import { MOTION_TYPE_LABELS, OUTCOME_LABELS, CASE_TYPE_LABELS } from '@/lib/display-helpers';

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
      caseTypes: [],
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
      caseTypes: [],
    });
    expect(params.get('outcome')).toBe('granted,denied');
  });

  it('includes caseTypes when non-empty', () => {
    const params = buildSearchParams({
      q: '',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
      caseTypes: ['civil', 'family'],
    });
    expect(params.get('caseType')).toBe('civil,family');
  });

  it('omits motionTypes, outcomes, and caseTypes when empty', () => {
    const params = buildSearchParams({
      q: 'test',
      county: '',
      judgeName: '',
      dateFrom: '',
      dateTo: '',
      motionTypes: [],
      outcomes: [],
      caseTypes: [],
    });
    expect(params.has('motion')).toBe(false);
    expect(params.has('outcome')).toBe(false);
    expect(params.has('caseType')).toBe(false);
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
      caseTypes: ['civil'],
    });
    expect(params.get('q')).toBe('summary judgment');
    expect(params.get('county')).toBe('Los Angeles');
    expect(params.get('judge')).toBe('Smith');
    expect(params.get('dateFrom')).toBe('2026-01-01');
    expect(params.get('dateTo')).toBe('2026-03-01');
    expect(params.get('motion')).toBe('msj');
    expect(params.get('outcome')).toBe('granted');
    expect(params.get('caseType')).toBe('civil');
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

  it('parses caseTypes from comma-separated string', () => {
    const params = new URLSearchParams('caseType=civil,family');
    const state = parseSearchParams(params);
    expect(state.caseTypes).toEqual(['civil', 'family']);
  });

  it('returns empty arrays when motion, outcome, and caseType are absent', () => {
    const params = new URLSearchParams('q=test');
    const state = parseSearchParams(params);
    expect(state.motionTypes).toEqual([]);
    expect(state.outcomes).toEqual([]);
    expect(state.caseTypes).toEqual([]);
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
      caseTypes: ['civil'],
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

describe('CASE_TYPES', () => {
  it('has labels for every case type', () => {
    for (const ct of CASE_TYPES) {
      expect(CASE_TYPE_LABELS[ct]).toBeDefined();
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
    Briefcase: ({ className }: { className?: string }) => (
      <span data-testid="briefcase-icon" className={className} />
    ),
    AlertCircle: ({ className }: { className?: string }) => (
      <span data-testid="alert-circle-icon" className={className} />
    ),
  };
});

vi.mock('@/lib/display-helpers', async () => {
  const actual = await vi.importActual('@/lib/display-helpers');
  return {
    ...actual,
    formatDate: (d: string) => d,
  };
});

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

vi.mock('@/lib/filter-options', async () => {
  const actual = await vi.importActual('@/lib/filter-options');
  return {
    ...actual,
    useCountyOptions: () => ['Los Angeles', 'Orange'],
    useJudgeNameOptions: () => ['Smith, John'],
  };
});

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

// ---------------------------------------------------------------------------
// SearchPage component — filter visibility (#1739)
// ---------------------------------------------------------------------------

describe('SearchPage component — filter visibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('hides filter sidebar when no search has been performed (empty state)', () => {
    mockSearchParamsValue = new URLSearchParams('');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    // Filter sidebar should not be present
    expect(screen.queryByLabelText('Search filters')).not.toBeInTheDocument();
    // But the empty state message should be visible and centered
    expect(screen.getByText('Enter a search term to begin')).toBeInTheDocument();
  });

  it('hides filter sidebar when search returns no results', () => {
    mockSearchParamsValue = new URLSearchParams('q=nonexistent');
    mockUseQuery.mockReturnValue({
      data: {
        searchRulings: {
          edges: [],
          pageInfo: { hasNextPage: false, endCursor: null },
          totalHits: 0,
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.queryByLabelText('Search filters')).not.toBeInTheDocument();
    expect(screen.getByText('No results for your search')).toBeInTheDocument();
  });

  it('hides mobile filter button when no results are present', () => {
    mockSearchParamsValue = new URLSearchParams('');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.queryByLabelText('Open filters')).not.toBeInTheDocument();
  });

  it('shows filter sidebar when search results are present', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.getByLabelText('Search filters')).toBeInTheDocument();
  });

  it('shows mobile filter button when search results are present', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.getByLabelText('Open filters')).toBeInTheDocument();
  });

  it('hides filter sidebar on error state', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.queryByLabelText('Search filters')).not.toBeInTheDocument();
  });

  it('hides filter sidebar when error occurs with stale data (fetchMore failure)', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: new Error('fetchMore failed'),
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    // Error state should take priority over stale results
    expect(screen.queryByLabelText('Search filters')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Open filters')).not.toBeInTheDocument();
    expect(screen.getByText('Failed to load search results.')).toBeInTheDocument();
    expect(screen.getByText('Try again')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// SearchPage component — error state styling (#1744)
// ---------------------------------------------------------------------------

describe('SearchPage component — error state styling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uses soft warm-toned error styling, not destructive red banner', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    const { container } = render(<SearchPage />);

    // Should NOT use destructive (alarming) styling
    expect(container.querySelector('.border-destructive')).not.toBeInTheDocument();
    expect(container.querySelector('.text-destructive')).not.toBeInTheDocument();

    // Should use soft semantic styling (bg-muted, border)
    expect(container.querySelector('.bg-muted')).toBeInTheDocument();
  });

  it('renders a warning icon in the error state', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    expect(screen.getByTestId('alert-circle-icon')).toBeInTheDocument();
  });

  it('renders a Try again action in the error state', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: undefined,
      loading: false,
      error: new Error('Network error'),
      fetchMore: vi.fn(),
    });

    render(<SearchPage />);
    const tryAgainButton = screen.getByText('Try again');
    expect(tryAgainButton).toBeInTheDocument();
    expect(tryAgainButton.tagName).toBe('BUTTON');
  });

  // Hover state tests (#1743)
  it('renders hover highlight on search result rows using muted design token', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockUseQuery.mockReturnValue({
      data: MOCK_SEARCH_DATA,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });

    const { container } = render(<SearchPage />);
    const rows = container.querySelectorAll('.hover\\:bg-muted\\/50');
    expect(rows.length).toBeGreaterThanOrEqual(2);
  });
});
