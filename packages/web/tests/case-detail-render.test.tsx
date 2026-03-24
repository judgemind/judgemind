import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let mockCaseQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
};
let mockRulingsQueryResult: {
  data: unknown;
  loading: boolean;
  error: Error | undefined;
  fetchMore: ReturnType<typeof vi.fn>;
};

// Track which queries were called
let queryCallCount = 0;

// IntersectionObserver mock
let mockObserve: ReturnType<typeof vi.fn>;
let mockDisconnect: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockObserve = vi.fn();
  mockDisconnect = vi.fn();

  vi.stubGlobal(
    'IntersectionObserver',
    vi.fn((_callback: IntersectionObserverCallback) => ({
      observe: mockObserve,
      disconnect: mockDisconnect,
      unobserve: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

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
    useQuery: () => {
      queryCallCount++;
      // First call is CASE_QUERY, second is CASE_RULINGS_QUERY
      if (queryCallCount % 2 === 1) return mockCaseQueryResult;
      return mockRulingsQueryResult;
    },
  };
});

import { CaseDetail } from '../src/app/(main)/cases/[id]/CaseDetail';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCaseData(overrides: Record<string, unknown> = {}) {
  return {
    case: {
      id: 'case-1',
      caseNumber: '25STCV12345',
      caseTitle: 'Smith v. Jones',
      caseType: 'civil',
      caseStatus: 'active',
      filedAt: '2025-06-15',
      court: {
        courtName: 'Los Angeles Superior Court',
        county: 'Los Angeles',
      },
      parties: [
        {
          id: 'p1',
          canonicalName: 'Alice Smith',
          partyType: null,
          role: 'plaintiff',
        },
        {
          id: 'p2',
          canonicalName: 'Bob Jones',
          partyType: null,
          role: 'defendant',
        },
      ],
      judges: [
        { id: 'j1', canonicalName: 'Smith, John' },
      ],
      ...overrides,
    },
  };
}

function makeRulingNode(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ruling-1',
    hearingDate: '2026-03-01',
    motionType: 'msj',
    outcome: 'granted',
    isTentative: true,
    department: '5',
    judge: { canonicalName: 'Smith, John' },
    rulingText: 'The motion for summary judgment is granted.',
    rulingTextHtml: null,
    documentId: null,
    documentFormat: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('CaseDetail (render)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    queryCallCount = 0;
    mockCaseQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
    };
    mockRulingsQueryResult = {
      data: undefined,
      loading: false,
      error: undefined,
      fetchMore: vi.fn(),
    };
  });

  it('shows skeleton while case is loading', () => {
    mockCaseQueryResult.loading = true;
    const { container } = render(<CaseDetail caseId="case-1" />);
    const skeletons = container.querySelectorAll('.animate-pulse');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows error message when case query fails', () => {
    mockCaseQueryResult.error = new Error('Network error');
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByText('Failed to load case details. Please try again.'),
    ).toBeInTheDocument();
  });

  it('shows not-found message when case is null', () => {
    mockCaseQueryResult.data = { case: null };
    render(<CaseDetail caseId="nonexistent" />);
    expect(screen.getByText('Case not found.')).toBeInTheDocument();
  });

  it('renders filed date in the header area', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    // Filed date appears once in the parties header area
    expect(screen.getByText('Jun 15, 2025')).toBeInTheDocument();
  });

  it('renders parties grouped by role in the header area', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    // Parties appear once in the header area (no sidebar duplication)
    expect(screen.getByText('Plaintiffs')).toBeInTheDocument();
    expect(screen.getByText('Alice Smith')).toBeInTheDocument();
    expect(screen.getByText('Defendants')).toBeInTheDocument();
    expect(screen.getByText('Bob Jones')).toBeInTheDocument();
  });

  it('does not render separate Case Details or Judges cards', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    // Case Details and Judges card headings should NOT exist
    expect(screen.queryByText('Case Details')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Judges' })).not.toBeInTheDocument();
  });

  it('hides parties section when no parties', () => {
    mockCaseQueryResult.data = makeCaseData({ parties: [], filedAt: null });
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.queryByText('Plaintiffs')).not.toBeInTheDocument();
  });

  it('renders ruling rows with hearing date, motion type, outcome', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Mar 1, 2026')).toBeInTheDocument();
    expect(screen.getByText('Granted')).toBeInTheDocument();
    expect(screen.getByText('Tentative')).toBeInTheDocument();
  });

  it('renders ruling text after expanding the ruling card', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);

    // Text is hidden before expanding
    expect(
      screen.queryByText('The motion for summary judgment is granted.'),
    ).not.toBeInTheDocument();

    // Expand the ruling card
    const rulingBtn = screen.getByLabelText(/Ruling from/);
    fireEvent.click(rulingBtn);

    // Now text is visible
    expect(
      screen.getByText('The motion for summary judgment is granted.'),
    ).toBeInTheDocument();
  });

  it('shows full text when ruling card is expanded (card-based collapse)', () => {
    const longText = 'The court considered the arguments. '.repeat(30);
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [
          { cursor: 'c1', node: makeRulingNode({ rulingText: longText }) },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    const { container } = render(<CaseDetail caseId="case-1" />);

    // Expand the ruling card
    const rulingBtn = screen.getByLabelText(/Ruling from/);
    fireEvent.click(rulingBtn);

    // Full text should be visible — no CSS truncation in card layout
    expect(screen.getByText(/The court considered the arguments/)).toBeInTheDocument();

    // No gradient overlay or max-height truncation
    expect(container.querySelector('.bg-gradient-to-t')).not.toBeInTheDocument();
    expect(container.querySelector('.max-h-40')).not.toBeInTheDocument();
  });

  it('renders download link when documentId is present and card is expanded', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [
          {
            cursor: 'c1',
            node: makeRulingNode({
              documentId: 'doc-uuid-1',
              documentFormat: 'pdf',
            }),
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);

    // Expand the ruling card to see download link
    const rulingBtn = screen.getByLabelText(/Ruling from/);
    fireEvent.click(rulingBtn);

    expect(screen.getByText('Download original')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('shows "Final" badge for non-tentative rulings', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [
          { cursor: 'c1', node: makeRulingNode({ isTentative: false }) },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Final')).toBeInTheDocument();
  });

  it('shows empty state when no rulings', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByText('No rulings captured for this case.'),
    ).toBeInTheDocument();
  });

  it('shows rulings error', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.error = new Error('Rulings query failed');
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByText('Failed to load rulings. Please try again.'),
    ).toBeInTheDocument();
  });

  it('renders infinite scroll sentinel when hasNextPage is true', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByTestId('scroll-sentinel')).toBeInTheDocument();
    // No "Load more" button — infinite scroll only
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('does not render sentinel when hasNextPage is false', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.queryByTestId('scroll-sentinel')).not.toBeInTheDocument();
  });

  it('renders download link for document without ruling text after expanding', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [
          {
            cursor: 'c1',
            node: makeRulingNode({
              rulingText: null,
              rulingTextHtml: null,
              documentId: 'doc-uuid-2',
              documentFormat: 'html',
            }),
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);

    // Expand the ruling card to see download link
    const rulingBtn = screen.getByLabelText(/Ruling from/);
    fireEvent.click(rulingBtn);

    expect(screen.getByText('Download original')).toBeInTheDocument();
    expect(screen.getByText('HTML')).toBeInTheDocument();
  });

  it('renders single-column layout (no sidebar grid)', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    const { container } = render(<CaseDetail caseId="case-1" />);
    // Should NOT have a sidebar grid layout
    const gridEl = container.querySelector('.sm\\:grid-cols-\\[1fr_280px\\]');
    expect(gridEl).not.toBeInTheDocument();
    // Should NOT have an aside element
    expect(container.querySelector('aside')).not.toBeInTheDocument();
  });
});
