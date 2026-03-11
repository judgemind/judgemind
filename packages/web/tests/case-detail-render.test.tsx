import { describe, it, expect, vi, beforeEach } from 'vitest';
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

import { CaseDetail } from '../src/app/cases/[id]/CaseDetail';

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
      judges: [
        { id: 'j1', canonicalName: 'Smith, John', department: '5' },
      ],
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

  it('renders case metadata: court and county', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByText('Los Angeles Superior Court'),
    ).toBeInTheDocument();
    expect(screen.getByText('Los Angeles')).toBeInTheDocument();
  });

  it('renders filed date', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Jun 15, 2025')).toBeInTheDocument();
  });

  it('renders parties grouped by role', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Plaintiffs')).toBeInTheDocument();
    expect(screen.getByText('Alice Smith')).toBeInTheDocument();
    expect(screen.getByText('Defendants')).toBeInTheDocument();
    expect(screen.getByText('Bob Jones')).toBeInTheDocument();
  });

  it('renders judges section', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Judges')).toBeInTheDocument();
    expect(screen.getByText('Smith, John')).toBeInTheDocument();
  });

  it('hides parties section when no parties', () => {
    mockCaseQueryResult.data = makeCaseData({ parties: [] });
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

  it('renders ruling text', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByText('The motion for summary judgment is granted.'),
    ).toBeInTheDocument();
  });

  it('shows Show more button for long ruling text and toggles on click', () => {
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
    render(<CaseDetail caseId="case-1" />);
    const showMoreBtn = screen.getByText('Show more');
    expect(showMoreBtn).toBeInTheDocument();

    fireEvent.click(showMoreBtn);
    expect(screen.getByText('Show less')).toBeInTheDocument();
  });

  it('renders download link when documentId is present', () => {
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

  it('renders load more button for rulings', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [{ cursor: 'c1', node: makeRulingNode() }],
        pageInfo: { hasNextPage: true, endCursor: 'cursor-1' },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(
      screen.getByRole('button', { name: 'Load more' }),
    ).toBeInTheDocument();
  });

  it('renders download link for document without ruling text', () => {
    mockCaseQueryResult.data = makeCaseData();
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [
          {
            cursor: 'c1',
            node: makeRulingNode({
              rulingText: null,
              documentId: 'doc-uuid-2',
              documentFormat: 'html',
            }),
          },
        ],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    expect(screen.getByText('Download original')).toBeInTheDocument();
    expect(screen.getByText('HTML')).toBeInTheDocument();
  });

  it('uses em-dash for court when null', () => {
    mockCaseQueryResult.data = makeCaseData({ court: null });
    mockRulingsQueryResult.data = {
      rulings: {
        edges: [],
        pageInfo: { hasNextPage: false, endCursor: null },
      },
    };
    render(<CaseDetail caseId="case-1" />);
    // Two em-dashes: one for court, one for county
    const dashes = screen.getAllByText('\u2014');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });
});
