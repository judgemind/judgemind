import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockReplace = vi.fn();
let mockSearchParamsValue = new URLSearchParams();
let mockQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  fetchMore: ReturnType<typeof vi.fn>;
};

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: mockReplace,
    back: vi.fn(),
  }),
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

vi.mock('@apollo/client', async () => {
  const actual = await vi.importActual<typeof import('@apollo/client')>(
    '@apollo/client',
  );
  return {
    ...actual,
    useQuery: () => mockQueryResult,
  };
});

import { SearchPage } from '../src/app/(main)/search/SearchPage';

// IntersectionObserver mock
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockObserve = vi.fn();
  mockDisconnect = vi.fn();

  vi.stubGlobal(
    'IntersectionObserver',
    vi.fn((callback: IntersectionObserverCallback) => {
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSearchHit(overrides: Record<string, unknown> = {}) {
  return {
    rulingId: 'ruling-1',
    caseNumber: '25STCV12345',
    court: 'LA Superior Court',
    county: 'Los Angeles',
    state: 'CA',
    judgeName: 'Smith, John',
    hearingDate: '2026-03-01',
    excerpt: 'The motion is <em>granted</em>.',
    score: 1.5,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('SearchPage (render)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSearchParamsValue = new URLSearchParams();
    mockQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    };
  });

  it('renders the search heading and description', () => {
    render(<SearchPage />);
    expect(screen.getByText('Search Rulings')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Full-text search across California tentative rulings.',
      ),
    ).toBeInTheDocument();
  });

  it('renders the search input and button', () => {
    render(<SearchPage />);
    expect(screen.getByLabelText('Search query')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Search' }),
    ).toBeInTheDocument();
  });

  it('renders filter panel with county, judge, motion types, outcomes, and date inputs when results are present', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.getByLabelText('County')).toBeInTheDocument();
    expect(screen.getByLabelText('Judge')).toBeInTheDocument();
    expect(screen.getByText('Motion Type')).toBeInTheDocument();
    expect(screen.getByText('Outcome')).toBeInTheDocument();
    expect(screen.getByLabelText('Date from')).toBeInTheDocument();
    expect(screen.getByLabelText('Date to')).toBeInTheDocument();
  });

  it('shows empty state before first search', () => {
    render(<SearchPage />);
    expect(
      screen.getByText('Enter a search term to begin'),
    ).toBeInTheDocument();
  });

  it('shows skeleton loading state after search is submitted', () => {
    mockQueryResult.loading = true;
    mockSearchParamsValue = new URLSearchParams('q=test');
    const { container } = render(<SearchPage />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows error state when query fails', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockQueryResult.error = new Error('Network error');
    render(<SearchPage />);
    expect(
      screen.getByText('Failed to load search results.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Try again'),
    ).toBeInTheDocument();
  });

  it('shows empty results message', () => {
    mockSearchParamsValue = new URLSearchParams('q=nonexistent');
    mockQueryResult.data = {
      searchRulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 0,
      },
    };
    render(<SearchPage />);
    expect(
      screen.getByText('No results for your search'),
    ).toBeInTheDocument();
  });

  it('renders search results with case info', () => {
    mockSearchParamsValue = new URLSearchParams('q=motion');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.getByText('25STCV12345')).toBeInTheDocument();
    expect(screen.getByText(/Smith, John/)).toBeInTheDocument();
    expect(screen.getByText('1 result found')).toBeInTheDocument();
  });

  it('pluralizes result count for multiple hits', () => {
    mockSearchParamsValue = new URLSearchParams('q=motion');
    mockQueryResult.data = {
      searchRulings: {
        edges: [
          { cursor: 'c1', node: makeSearchHit() },
          {
            cursor: 'c2',
            node: makeSearchHit({ rulingId: 'ruling-2', caseNumber: '25STCV99999' }),
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 2,
      },
    };
    render(<SearchPage />);
    expect(screen.getByText('2 results found')).toBeInTheDocument();
  });

  it('shows Unknown Case for null caseNumber', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockQueryResult.data = {
      searchRulings: {
        edges: [
          {
            cursor: 'c1',
            node: makeSearchHit({ caseNumber: null }),
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.getByText('Unknown Case')).toBeInTheDocument();
  });

  it('renders sentinel element when hasNextPage (infinite scroll)', () => {
    mockSearchParamsValue = new URLSearchParams('q=motion');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
        totalHits: 50,
      },
    };
    render(<SearchPage />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    // No Load more button
    expect(screen.queryByText('Load more')).not.toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockSearchParamsValue = new URLSearchParams('q=motion');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('renders motion type filter checkboxes when results are present', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.getByText('MSJ')).toBeInTheDocument();
    expect(screen.getByText('MTD')).toBeInTheDocument();
    expect(screen.getByText('MIL')).toBeInTheDocument();
    expect(screen.getByText('Demurrer')).toBeInTheDocument();
    expect(screen.getByText('Anti-SLAPP')).toBeInTheDocument();
  });

  it('renders outcome filter checkboxes when results are present', () => {
    mockSearchParamsValue = new URLSearchParams('q=test');
    mockQueryResult.data = {
      searchRulings: {
        edges: [{ cursor: 'c1', node: makeSearchHit() }],
        pageInfo: { hasNextPage: false, endCursor: null },
        totalHits: 1,
      },
    };
    render(<SearchPage />);
    expect(screen.getByText('Granted')).toBeInTheDocument();
    expect(screen.getByText('Denied')).toBeInTheDocument();
    expect(screen.getByText('Granted In Part')).toBeInTheDocument();
    expect(screen.getByText('Moot')).toBeInTheDocument();
    expect(screen.getByText('Continued')).toBeInTheDocument();
  });

  it('submits search and updates URL', async () => {
    render(<SearchPage />);
    fireEvent.change(screen.getByLabelText('Search query'), {
      target: { value: 'summary judgment' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalled();
    });
  });
});
