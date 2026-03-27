import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Radix UI Select uses pointer events and pointer capture, which jsdom does not support.
// Stub the missing methods so Radix doesn't throw in tests.
beforeEach(() => {
  Element.prototype.hasPointerCapture = vi.fn().mockReturnValue(false);
  Element.prototype.setPointerCapture = vi.fn();
  Element.prototype.releasePointerCapture = vi.fn();
  // Radix Select also uses scrollIntoView
  Element.prototype.scrollIntoView = vi.fn();
});

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
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [key: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

vi.mock('@/lib/display-helpers', () => ({
  formatDate: (d: string) => d,
  formatLabel: (v: string | null) =>
    v ? v.charAt(0).toUpperCase() + v.slice(1).replace(/_/g, ' ') : '\u2014',
  formatMotionType: (m: string | null) => m ?? 'Not classified',
  getOutcomeBadgeVariant: () => 'outline',
  getOutcomeBadgeListClass: () => '',
}));

vi.mock('@/lib/filter-options', () => ({
  useCountyOptions: () => ['Los Angeles', 'Orange', 'San Bernardino'],
}));

vi.mock('@/components/OutcomeBadge', () => ({
  OutcomeBadge: ({ outcome }: { outcome: string | null }) => (
    <span data-testid="outcome-badge">{outcome ?? '\u2014'}</span>
  ),
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

import { CasesList } from '../CasesList';

const MOCK_CASE_NODE = {
  id: 'case-1',
  caseNumber: '24STCV01234',
  caseTitle: 'Smith v. Jones',
  caseType: 'civil',
  caseStatus: 'active',
  court: { courtName: 'Superior Court of California', county: 'Los Angeles' },
  latestRuling: {
    hearingDate: '2026-03-10',
    outcome: 'granted',
    motionType: 'msj',
  },
};

const MOCK_CASES_DATA = {
  cases: {
    edges: [
      { cursor: 'cursor-1', node: MOCK_CASE_NODE },
      {
        cursor: 'cursor-2',
        node: {
          ...MOCK_CASE_NODE,
          id: 'case-2',
          caseNumber: '24NNCV05678',
          caseTitle: null,
          caseType: null,
          caseStatus: 'closed',
          court: { courtName: 'Superior Court of California', county: 'San Bernardino' },
          latestRuling: {
            hearingDate: '2026-03-08',
            outcome: 'denied',
            motionType: 'mtd',
          },
        },
      },
      {
        cursor: 'cursor-3',
        node: {
          ...MOCK_CASE_NODE,
          id: 'case-3',
          caseNumber: '24STCV99999',
          caseTitle: 'No Ruling Case',
          caseType: 'family',
          caseStatus: 'active',
          court: { courtName: 'Superior Court of California', county: 'Orange' },
          latestRuling: null,
        },
      },
    ],
    pageInfo: { hasNextPage: true, endCursor: 'cursor-3' },
  },
};

describe('CasesList', () => {
  beforeEach(() => { vi.clearAllMocks(); mockSearchParamsValue = new URLSearchParams(); });

  it('renders skeleton rows while loading', () => {
    mockUseQuery.mockReturnValue({ data: undefined, loading: true, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const skeletons = screen.getAllByTestId('skeleton-row');
    expect(skeletons.length).toBe(8);
  });

  it('renders error state', () => {
    mockUseQuery.mockReturnValue({ data: undefined, loading: false, error: new Error('Network error'), fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/Failed to load cases/)).toBeInTheDocument();
  });

  it('renders empty state when no cases found', () => {
    mockUseQuery.mockReturnValue({ data: { cases: { edges: [], pageInfo: { hasNextPage: false, endCursor: null } } }, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/No cases found/)).toBeInTheDocument();
  });

  it('renders case rows with case numbers and titles', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/24STCV01234/)).toBeInTheDocument();
    expect(screen.getByText(/Smith v. Jones/)).toBeInTheDocument();
    expect(screen.getByText(/24NNCV05678/)).toBeInTheDocument();
  });

  it('renders latest ruling outcome badges', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const badges = screen.getAllByTestId('outcome-badge');
    expect(badges.length).toBe(2); // case-1 and case-2 have rulings; case-3 does not
    expect(badges[0]).toHaveTextContent('granted');
    expect(badges[1]).toHaveTextContent('denied');
  });

  it('renders latest ruling hearing dates', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('2026-03-10')).toBeInTheDocument();
    expect(screen.getByText('2026-03-08')).toBeInTheDocument();
  });

  it('renders latest ruling motion types', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('msj')).toBeInTheDocument();
    expect(screen.getByText('mtd')).toBeInTheDocument();
  });

  it('renders em-dash for cases without a latest ruling date', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // Case-3 has no ruling, should show em-dash
    const emDashes = screen.getAllByText('\u2014');
    expect(emDashes.length).toBeGreaterThan(0);
  });

  it('renders county metadata in row', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
    expect(screen.getByText('San Bernardino')).toBeInTheDocument();
  });

  it('renders case links pointing to detail pages', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText(/24STCV01234/).closest('a')).toHaveAttribute('href', '/cases/case-1');
  });

  it('renders sentinel element when hasNextPage is true (infinite scroll)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockUseQuery.mockReturnValue({
      data: {
        cases: {
          ...MOCK_CASES_DATA.cases,
          pageInfo: { hasNextPage: false, endCursor: null },
        },
      },
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    });
    render(<CasesList />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('sets up IntersectionObserver on the sentinel element', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(IntersectionObserver).toHaveBeenCalledWith(
      expect.any(Function),
      { rootMargin: '200px' },
    );
    expect(mockObserve).toHaveBeenCalled();
  });

  it('calls fetchMore when sentinel becomes visible', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: mockFetchMore });
    render(<CasesList />);

    intersectionCallback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: { after: 'cursor-3' },
      }),
    );
  });

  it('does not call fetchMore when sentinel is not intersecting', () => {
    const mockFetchMore = vi.fn().mockResolvedValue({});
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: mockFetchMore });
    render(<CasesList />);

    intersectionCallback(
      [{ isIntersecting: false } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );

    expect(mockFetchMore).not.toHaveBeenCalled();
  });

  it('does not render sentinel while loading more results', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: true, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('disconnects observer on unmount', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { unmount } = render(<CasesList />);
    unmount();
    expect(mockDisconnect).toHaveBeenCalled();
  });

  it('does not render a "Load more" button (uses infinite scroll instead)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('renders filter inputs including county, date range, and case type', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByPlaceholderText(/Case number or title/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Case type/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/County/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Rulings from/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Rulings to/)).toBeInTheDocument();
  });

  it('filter inputs have aria-label attributes', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const caseFilter = screen.getByLabelText('Case number or title');
    expect(caseFilter).toHaveAttribute('name', 'caseFilter');
    const typeTrigger = screen.getByLabelText('Case type');
    expect(typeTrigger.tagName.toLowerCase()).toBe('button');
    const dateFrom = screen.getByLabelText('Rulings from');
    expect(dateFrom).toHaveAttribute('name', 'dateFrom');
    const dateTo = screen.getByLabelText('Rulings to');
    expect(dateTo).toHaveAttribute('name', 'dateTo');
  });

  it('filters cases client-side by case number', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByPlaceholderText(/Case number or title/i), { target: { value: '24STCV01234' } });
    expect(screen.getByText(/24STCV01234/)).toBeInTheDocument();
    expect(screen.queryByText(/24NNCV05678/)).not.toBeInTheDocument();
  });

  it('renders shadcn Select trigger showing default placeholder text', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByText('All types')).toBeInTheDocument();
  });

  it('does not use raw select elements (uses shadcn Select)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelectorAll('select').length).toBe(0);
  });

  it('initializes case type filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=probate');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBe('probate');
    expect(screen.getByText('Probate')).toBeInTheDocument();
  });

  it('initializes county filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('county=Los%20Angeles');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.county).toBe('Los Angeles');
  });

  it('initializes dateFrom filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('dateFrom=2026-01-01');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.dateFrom).toBe('2026-01-01');
  });

  it('initializes dateTo filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('dateTo=2026-12-31');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.dateTo).toBe('2026-12-31');
  });

  it('updates URL when case type filter is set via URL params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=family');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('updates URL with all filter params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=civil&county=Orange&dateFrom=2026-01-01&dateTo=2026-06-30');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=civil'));
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('county=Orange'));
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('dateFrom=2026-01-01'));
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('dateTo=2026-06-30'));
  });

  it('does not pass caseType when "All types" is selected', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBeUndefined();
  });

  it('does not pass county when empty', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.county).toBeUndefined();
  });

  it('does not pass dateFrom when empty', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.dateFrom).toBeUndefined();
  });

  it('uses borderless row layout (not table)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    // Should NOT have table elements
    expect(container.querySelector('table')).not.toBeInTheDocument();
    expect(container.querySelector('thead')).not.toBeInTheDocument();
    // Should use divide-y rows (borderless row pattern)
    expect(container.querySelector('.divide-y')).toBeInTheDocument();
  });

  it('search placeholder shows actual ellipsis character, not unicode escape', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const input = screen.getByPlaceholderText(/Case number or title/i);
    expect(input.getAttribute('placeholder')).toContain('\u2026');
    expect(input.getAttribute('placeholder')).not.toContain('\\u');
  });

  it('updates query variables when case type filter is changed via user interaction', async () => {
    const user = userEvent.setup();
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);

    await user.click(screen.getByLabelText(/Case type/i));
    const familyOption = await screen.findByRole('option', { name: 'Family' });
    await user.click(familyOption);

    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('family');
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('shows skeleton cards while fetching more results', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: true, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const skeletons = screen.getAllByTestId('skeleton-row');
    expect(skeletons.length).toBe(3);
  });
});
