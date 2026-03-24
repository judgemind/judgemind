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

const MOCK_CASES_DATA = {
  cases: {
    edges: [
      { cursor: 'cursor-1', node: { id: 'case-1', caseNumber: '24STCV01234', caseTitle: 'Smith v. Jones', caseType: 'civil', caseStatus: 'active', court: { courtName: 'Superior Court of California', county: 'Los Angeles' } } },
      { cursor: 'cursor-2', node: { id: 'case-2', caseNumber: '24NNCV05678', caseTitle: null, caseType: null, caseStatus: 'closed', court: { courtName: 'Superior Court of California', county: 'San Bernardino' } } },
    ],
    pageInfo: { hasNextPage: true, endCursor: 'cursor-2' },
  },
};

describe('CasesList', () => {
  beforeEach(() => { vi.clearAllMocks(); mockSearchParamsValue = new URLSearchParams(); });

  it('renders skeleton rows while loading', () => {
    mockUseQuery.mockReturnValue({ data: undefined, loading: true, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
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

  it('renders case rows with county prefixed to case numbers and titles', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // County should be prefixed to case number
    expect(screen.getByText(/Los Angeles .+ 24STCV01234/)).toBeInTheDocument();
    expect(screen.getByText('Smith v. Jones')).toBeInTheDocument();
    expect(screen.getByText(/San Bernardino .+ 24NNCV05678/)).toBeInTheDocument();
  });

  it('renders county inline with case number (not in separate column)', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    // Only 2 column headers: Case and Type (no Court or Status columns)
    const headers = container.querySelectorAll('th');
    expect(headers.length).toBe(2);
    expect(headers[0].textContent).toBe('Case');
    expect(headers[1].textContent).toBe('Type');
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
    // Sentinel hidden during loading to prevent duplicate fetches
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

  it('does not have a Status column', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    const headers = Array.from(container.querySelectorAll('th')).map((h) => h.textContent);
    expect(headers).not.toContain('Status');
  });

  it('does not have a Court column', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    const headers = Array.from(container.querySelectorAll('th')).map((h) => h.textContent);
    expect(headers).not.toContain('Court');
  });

  it('renders filter inputs with case type Select trigger', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(screen.getByPlaceholderText(/Case number or title/i)).toBeInTheDocument();
    // shadcn Select renders as button triggers with aria-label
    expect(screen.getByLabelText(/Case type/i)).toBeInTheDocument();
  });

  it('filter inputs have aria-label attributes', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const caseFilter = screen.getByLabelText('Case number or title');
    expect(caseFilter).toHaveAttribute('name', 'caseFilter');
    // shadcn Select triggers are buttons with aria-label
    const typeTrigger = screen.getByLabelText('Case type');
    expect(typeTrigger.tagName.toLowerCase()).toBe('button');
  });

  it('filters cases client-side by case number', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    fireEvent.change(screen.getByPlaceholderText(/Case number or title/i), { target: { value: '24STCV' } });
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
    // No native <select> elements should exist
    expect(container.querySelectorAll('select').length).toBe(0);
  });

  it('initializes case type filter from URL params and passes to query', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=probate');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    // Verify the query is called with the correct caseType from URL
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBe('probate');
    // Verify the Select trigger shows the selected value (Probate)
    expect(screen.getByText('Probate')).toBeInTheDocument();
  });

  it('updates URL when case type filter is set via URL params', () => {
    mockSearchParamsValue = new URLSearchParams('caseType=family');
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('does not pass caseType when "All types" is selected', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    expect(mockUseQuery.mock.calls[0][1].variables.caseType).toBeUndefined();
  });

  it('uses shadcn Table component structure', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    expect(container.querySelector('table')).toBeInTheDocument();
    expect(container.querySelector('thead')).toBeInTheDocument();
    expect(container.querySelector('tbody')).toBeInTheDocument();
  });

  it('uses table-fixed to prevent horizontal overflow', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    const table = container.querySelector('table');
    expect(table?.className).toContain('table-fixed');
  });

  it('wraps table in overflow-hidden container', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    const { container } = render(<CasesList />);
    const tableWrapper = container.querySelector('table')?.closest('.overflow-hidden');
    expect(tableWrapper).toBeInTheDocument();
  });

  it('updates query variables when case type filter is changed via user interaction', async () => {
    const user = userEvent.setup();
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);

    // Click the case type select trigger to open the dropdown
    await user.click(screen.getByLabelText(/Case type/i));

    // Click on the 'Family' option
    const familyOption = await screen.findByRole('option', { name: 'Family' });
    await user.click(familyOption);

    // Assert query was re-run with new caseType variable
    const lastCall = mockUseQuery.mock.calls[mockUseQuery.mock.calls.length - 1];
    expect(lastCall[1].variables.caseType).toBe('family');

    // Assert URL was updated
    expect(mockReplace).toHaveBeenCalledWith(expect.stringContaining('caseType=family'));
  });

  it('search placeholder shows actual ellipsis character, not unicode escape', () => {
    mockUseQuery.mockReturnValue({ data: MOCK_CASES_DATA, loading: false, error: undefined, fetchMore: vi.fn() });
    render(<CasesList />);
    const input = screen.getByPlaceholderText(/Case number or title/i);
    // The placeholder should contain the actual ellipsis character (U+2026)
    expect(input.getAttribute('placeholder')).toContain('\u2026');
    // And should NOT contain a literal backslash-u escape
    expect(input.getAttribute('placeholder')).not.toContain('\\u');
  });
});
